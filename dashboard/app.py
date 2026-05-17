#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi>=0.110",
#     "uvicorn>=0.27",
#     "croniter>=2.0",
# ]
# ///
"""
chassis dashboard — local web monitor for chassis containers.

Auto-discovers any container whose docker-compose project name ends with
"-chassis" and exposes a single auto-refreshing page at http://127.0.0.1:8765
showing, per chassis:
  - container status, uptime, restart count
  - CPU / memory usage
  - parsed cron schedule with next-fire times
  - per-agent last-run timestamp + task
  - tail of the dispatcher audit log (deny/error highlighted)

Click an audit row or agent row to drill in.

See dashboard/README.md for setup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from croniter import croniter
except ImportError:
    croniter = None


CHASSIS_SUFFIX = "-chassis"
AUDIT_LOG = "/var/log/chassis/run-tool.jsonl"
AGENTS_LOG_ROOT = "/var/log/chassis/agents"
CRON_FILE = "/etc/cron.d/chassis"
# Drill-in file reads are restricted to these prefixes inside the container.
# Anything else returns 400 — the dashboard isn't a generic shell.
ALLOWED_FILE_PREFIXES = ("/home/agent/.pi/", "/var/log/chassis/")
FILE_MAX_BYTES = 200_000

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")


def _safe_name(s: str) -> bool:
    return bool(_SAFE_NAME.match(s))


async def sh(*cmd: str, timeout: float = 5.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def docker_ps_chassis() -> list[dict]:
    rc, out, _ = await sh(
        "docker", "ps", "-a", "--format",
        '{{.Names}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}',
    )
    if rc != 0:
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, status_line, project = parts[0], parts[1], parts[2]
        if not project.endswith(CHASSIS_SUFFIX):
            continue
        rows.append({"name": name, "status_line": status_line, "project": project})
    rows.sort(key=lambda r: r["name"])
    return rows


async def docker_inspect(name: str) -> dict:
    rc, out, _ = await sh(
        "docker", "inspect", name, "--format",
        "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.Running}}",
    )
    if rc != 0:
        return {}
    parts = out.strip().split("|")
    if len(parts) < 4:
        return {}
    return {
        "status": parts[0],
        "started_at": parts[1],
        "restart_count": int(parts[2] or 0),
        "running": parts[3].lower() == "true",
    }


async def docker_stats_all() -> dict[str, dict]:
    rc, out, _ = await sh(
        "docker", "stats", "--no-stream", "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
        timeout=10.0,
    )
    res: dict[str, dict] = {}
    if rc != 0:
        return res
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        res[parts[0]] = {"cpu": parts[1], "mem": parts[2], "mem_pct": parts[3]}
    return res


async def exec_in(container: str, *cmd: str, user: str = "agent", timeout: float = 4.0) -> str:
    rc, out, _ = await sh(
        "docker", "exec", "-u", user, container, *cmd, timeout=timeout,
    )
    return out if rc == 0 else ""


async def tail_audit(container: str, n: int = 20) -> list[dict]:
    raw = await exec_in(container, "sh", "-c", f"tail -n {n} {AUDIT_LOG} 2>/dev/null")
    entries: list[dict] = []
    for line in raw.strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries


async def list_agents(container: str) -> list[str]:
    # Agent dirs live directly under /home/agent (alongside .pi and shell
    # dotfiles), so we identify them by the presence of agent.json rather
    # than listing the home dir wholesale.
    raw = await exec_in(
        container, "sh", "-c",
        f"ls {AGENTS_LOG_ROOT} 2>/dev/null; "
        "for f in /home/agent/*/agent.json; do "
        "[ -f \"$f\" ] && basename \"$(dirname \"$f\")\"; done",
    )
    seen: list[str] = []
    for a in raw.splitlines():
        a = a.strip()
        if a and a not in seen:
            seen.append(a)
    return sorted(seen)


async def agent_last_run(container: str, agent: str) -> dict | None:
    raw = await exec_in(
        container, "sh", "-c",
        f'find {AGENTS_LOG_ROOT}/{agent} -type f -name "*.jsonl" '
        f'-printf "%T@ %p\\n" 2>/dev/null | sort -n | tail -1',
    )
    line = raw.strip()
    if not line:
        return None
    parts = line.split(None, 1)
    if len(parts) < 2:
        return None
    mtime_s, path = parts
    head = await exec_in(container, "sh", "-c", f"head -1 {path} 2>/dev/null")
    rec: dict = {}
    head_lines = head.strip().splitlines()
    if head_lines:
        try:
            rec = json.loads(head_lines[0])
        except json.JSONDecodeError:
            pass
    return {
        "mtime": float(mtime_s),
        "path": path,
        "task": rec.get("task"),
        "ts": rec.get("ts"),
    }


CRON_LINE_RE = re.compile(
    r"^(?P<m>\S+)\s+(?P<h>\S+)\s+(?P<dom>\S+)\s+(?P<mon>\S+)\s+(?P<dow>\S+)\s+\S+\s+(?P<cmd>.*)$"
)


async def cron_jobs(container: str) -> list[dict]:
    raw = await exec_in(container, "cat", CRON_FILE, user="root", timeout=3.0)
    jobs: list[dict] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        first = s.split()[0]
        if "=" in first:
            continue
        m = CRON_LINE_RE.match(s)
        if not m:
            continue
        expr = f"{m['m']} {m['h']} {m['dom']} {m['mon']} {m['dow']}"
        cmd = m["cmd"]
        cmd = re.sub(r"\s*>>.*$", "", cmd)
        cmd = re.sub(r"^/usr/local/bin/run-pi\s+", "", cmd)
        next_fire = None
        if croniter:
            try:
                next_fire = croniter(expr, now).get_next(datetime).replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        jobs.append({"schedule": expr, "command": cmd, "next": next_fire})
    return jobs


async def chassis_snapshot(meta: dict, stats: dict[str, dict]) -> dict:
    name = meta["name"]
    inspect = await docker_inspect(name)
    snap: dict = {
        "name": name,
        "project": meta["project"],
        "status_line": meta["status_line"],
        "stats": stats.get(name, {}),
        **inspect,
    }
    if not inspect.get("running"):
        return snap
    audit, agents, jobs = await asyncio.gather(
        tail_audit(name, 25),
        list_agents(name),
        cron_jobs(name),
    )
    agent_runs = await asyncio.gather(*(agent_last_run(name, a) for a in agents))
    snap["agents"] = [{"name": a, "last_run": r} for a, r in zip(agents, agent_runs)]
    snap["audit"] = audit
    snap["cron"] = jobs
    return snap


# ---------- demo mode ----------------------------------------------------
# `--demo` short-circuits the three API routes and returns synthetic state.
# Timestamps regenerate per request so the dashboard always looks live —
# good for screenshots and for iterating on the UI without running real
# chassis. Touches nothing on disk; not a "seed real containers" mode.

def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# Shared runtime config for all demo chassis — surfaces in the top bar.
# (In real mode chassis_snapshot() can populate the same fields by reading
# /home/agent/.pi/agent/models.json for `model`/`source` and a future
# harness marker file for `harness`.)
DEMO_RUNTIME = {
    "harness": "pi",
    "model":   "Qwen/Qwen3.6-35B-A3B",
    "source":  "host3-5090-vLLM",
}


def _agent(name: str, *, last_task: str | None, mins_ago: float | None) -> dict:
    if mins_ago is None:
        return {"name": name, "last_run": None}
    when = datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    safe_task = (last_task or "run").replace("_", "-")
    return {"name": name, "last_run": {
        "mtime": when.timestamp(),
        "path": f"/var/log/chassis/agents/{name}/{safe_task}/{when.strftime('%Y-%m-%dT%H-%M-%S')}.jsonl",
        "task": last_task,
        "ts": _iso(when),
    }}


def demo_state() -> dict:
    """Synthetic chassis fleet emphasizing multi-agent coordination patterns —
    blackboard consensus, adversarial debate, distributed search, internal
    markets, theorem exploration. Each chassis runs several agents that
    talk to each other through shared state (blackboard / queue / kv) or
    peer RPC, not just to the outside world."""
    now = datetime.now(timezone.utc)
    def ago(mins: float) -> str: return _iso(now - timedelta(minutes=mins))
    def in_mins(mins: float) -> str: return _iso(now + timedelta(minutes=mins))

    chassis: list[dict] = []

    # 1. swarm-consensus (v1) — proposer / validator / aggregator coordinate
    # entirely through a shared in-chassis blackboard. No agent calls another
    # directly; the blackboard is the bus. A denied cross-chassis peer-rpc
    # at the bottom shows the isolation boundary against v2.
    chassis.append({
        "name": "swarm-consensus-chassis",
        "project": "swarm-consensus-chassis",
        "status_line": "Up 3 days",
        "status": "running",
        "started_at": _iso(now - timedelta(days=3)),
        "restart_count": 0,
        "running": True,
        "stats": {"cpu": "2.18%", "mem": "147.3MiB / 7.7GiB", "mem_pct": "1.87%"},
        "agents": [
            _agent("proposer",   last_task="propose", mins_ago=2),
            _agent("validator",  last_task="vote",    mins_ago=1),
            _agent("aggregator", last_task="tally",   mins_ago=4),
            _agent("manager",    last_task=None,      mins_ago=None),
        ],
        "audit": [
            {"ts": ago(1),  "tool": "blackboard-read", "args": '{"key":"round/0418/proposals"}',                                       "status": "ok",     "exit": 0, "duration_ms": 31,  "secrets_passed": []},
            {"ts": ago(1),  "tool": "vote-submit",     "args": '{"round":418,"proposal":"p2","weight":1}',                             "status": "ok",     "exit": 0, "duration_ms": 44,  "secrets_passed": []},
            {"ts": ago(2),  "tool": "queue-pop",       "args": '{"queue":"questions"}',                                                "status": "ok",     "exit": 0, "duration_ms": 38,  "secrets_passed": []},
            {"ts": ago(2),  "tool": "blackboard-post", "args": '{"key":"round/0418/proposals","items":3}',                             "status": "ok",     "exit": 0, "duration_ms": 56,  "secrets_passed": []},
            {"ts": ago(2),  "tool": "peer-broadcast",  "args": '{"to":"validator","event":"round_open","round":418}',                  "status": "ok",     "exit": 0, "duration_ms": 22,  "secrets_passed": []},
            {"ts": ago(4),  "tool": "vote-tally",      "args": '{"round":417}',                                                        "status": "ok",     "exit": 0, "duration_ms": 89,  "secrets_passed": []},
            {"ts": ago(4),  "tool": "kv-write",        "args": '{"key":"consensus/0417","value":"p1"}',                                "status": "ok",     "exit": 0, "duration_ms": 27,  "secrets_passed": []},
            {"ts": ago(7),  "tool": "peer-rpc",        "args": '{"target":"swarm-consensus-v2-chassis::aggregator","call":"sync"}',   "status": "denied", "exit": 2, "duration_ms": 0,   "reason": "args_schema violation: cross-chassis rpc not permitted", "secrets_passed": []},
        ],
        "cron": [
            {"schedule": "*/3 * * * *", "command": "proposer propose", "next": in_mins(1)},
            {"schedule": "* * * * *",   "command": "validator vote",   "next": in_mins(1)},
            {"schedule": "*/5 * * * *", "command": "aggregator tally", "next": in_mins(1)},
        ],
    })

    # 2. swarm-consensus (v2) — same blackboard pattern, plus a `reputation`
    # agent that scores past voters; tallies are weighted by historical
    # accuracy. Demonstrates iterating on a multi-agent design without
    # retiring v1 (run both on the same question stream, compare divergence).
    chassis.append({
        "name": "swarm-consensus-v2-chassis",
        "project": "swarm-consensus-v2-chassis",
        "status_line": "Up 19 hours",
        "status": "running",
        "started_at": _iso(now - timedelta(hours=19)),
        "restart_count": 0,
        "running": True,
        "stats": {"cpu": "2.71%", "mem": "163.9MiB / 7.7GiB", "mem_pct": "2.08%"},
        "agents": [
            _agent("proposer",   last_task="propose",       mins_ago=1),
            _agent("validator",  last_task="vote",          mins_ago=1),
            _agent("aggregator", last_task="weighted-tally",mins_ago=3),
            _agent("reputation", last_task="update",        mins_ago=7),
            _agent("manager",    last_task=None,            mins_ago=None),
        ],
        "audit": [
            {"ts": ago(1), "tool": "reputation-fetch",  "args": '{"voter":"v_07"}',                                                    "status": "ok",    "exit": 0, "duration_ms": 18,  "secrets_passed": []},
            {"ts": ago(1), "tool": "vote-submit",       "args": '{"round":118,"proposal":"p1","weight":0.83}',                         "status": "ok",    "exit": 0, "duration_ms": 41,  "secrets_passed": []},
            {"ts": ago(1), "tool": "blackboard-post",   "args": '{"key":"round/0118/proposals","items":3}',                            "status": "ok",    "exit": 0, "duration_ms": 52,  "secrets_passed": []},
            {"ts": ago(3), "tool": "vote-tally",        "args": '{"round":117,"weighted":true}',                                       "status": "ok",    "exit": 0, "duration_ms": 112, "secrets_passed": []},
            {"ts": ago(3), "tool": "kv-write",          "args": '{"key":"consensus/0117","value":"p2","margin":0.41}',                 "status": "ok",    "exit": 0, "duration_ms": 24,  "secrets_passed": []},
            {"ts": ago(7), "tool": "reputation-update", "args": '{"round":116,"updated":12}',                                          "status": "ok",    "exit": 0, "duration_ms": 87,  "secrets_passed": []},
            {"ts": ago(7), "tool": "blackboard-read",   "args": '{"key":"voter/v_03"}',                                                "status": "error", "exit": 1, "duration_ms": 14,  "reason": "key not found: voter/v_03 (stale validator, skipped)", "secrets_passed": []},
        ],
        "cron": [
            {"schedule": "*/3 * * * *", "command": "proposer propose",          "next": in_mins(2)},
            {"schedule": "* * * * *",   "command": "validator vote",            "next": in_mins(1)},
            {"schedule": "*/5 * * * *", "command": "aggregator weighted-tally", "next": in_mins(2)},
            {"schedule": "*/10 * * * *","command": "reputation update",         "next": in_mins(3)},
        ],
    })

    # 3. debate-arena — pro/con advocates trade arguments under an impartial
    # judge; archivist commits the verdict to a shared corpus. Adversarial
    # pair + neutral scorer is a classic structured-debate MAS pattern. The
    # denied row shows role-scoped policy: the judge can't search the web.
    chassis.append({
        "name": "debate-arena-chassis",
        "project": "debate-arena-chassis",
        "status_line": "Up 5 days",
        "status": "running",
        "started_at": _iso(now - timedelta(days=5)),
        "restart_count": 0,
        "running": True,
        "stats": {"cpu": "0.62%", "mem": "108.4MiB / 7.7GiB", "mem_pct": "1.38%"},
        "agents": [
            _agent("advocate-pro", last_task="argue",           mins_ago=12),
            _agent("advocate-con", last_task="counter",         mins_ago=11),
            _agent("judge",        last_task="score",           mins_ago=7),
            _agent("archivist",    last_task="publish-verdict", mins_ago=3),
            _agent("manager",      last_task=None,              mins_ago=None),
        ],
        "audit": [
            {"ts": ago(3),  "tool": "verdict-publish", "args": '{"topic":"t_0214","winner":"con","score":7.4}',                       "status": "ok",     "exit": 0, "duration_ms": 142, "secrets_passed": []},
            {"ts": ago(3),  "tool": "kv-write",        "args": '{"key":"corpus/0214","bytes":3812}',                                  "status": "ok",     "exit": 0, "duration_ms": 38,  "secrets_passed": []},
            {"ts": ago(7),  "tool": "blackboard-read", "args": '{"key":"topic/0214/rounds"}',                                         "status": "ok",     "exit": 0, "duration_ms": 26,  "secrets_passed": []},
            {"ts": ago(7),  "tool": "score-round",     "args": '{"topic":"t_0214","round":3,"rubric":"clarity+evidence"}',            "status": "ok",     "exit": 0, "duration_ms": 1183,"secrets_passed": []},
            {"ts": ago(7),  "tool": "web-search",      "args": '{"query":"survey of recent counterclaims for t_0214"}',               "status": "denied", "exit": 2, "duration_ms": 0,   "reason": "args_schema violation: judge role may not call web-search", "secrets_passed": []},
            {"ts": ago(11), "tool": "web-search",      "args": '{"query":"counterevidence for claim c_2"}',                           "status": "ok",     "exit": 0, "duration_ms": 1620,"secrets_passed": ["TAVILY_API_KEY"]},
            {"ts": ago(11), "tool": "peer-broadcast",  "args": '{"to":"judge","event":"round_closed","topic":"t_0214","round":3}',    "status": "ok",     "exit": 0, "duration_ms": 21,  "secrets_passed": []},
            {"ts": ago(12), "tool": "web-search",      "args": '{"query":"strongest evidence for claim c_2"}',                        "status": "ok",     "exit": 0, "duration_ms": 1554,"secrets_passed": ["TAVILY_API_KEY"]},
        ],
        "cron": [
            {"schedule": "*/15 * * * *", "command": "advocate-pro argue",        "next": in_mins(3)},
            {"schedule": "*/15 * * * *", "command": "advocate-con counter",      "next": in_mins(4)},
            {"schedule": "*/20 * * * *", "command": "judge score",               "next": in_mins(13)},
            {"schedule": "0 * * * *",    "command": "archivist publish-verdict", "next": in_mins(57)},
        ],
    })

    # 4. evolution-pool — distributed program/policy search. Tight inner
    # mutate -> evaluate loop with periodic selection and novelty-aware
    # archiving. High audit volume; most "work" here is inter-agent state
    # churn through the shared pool, not external calls.
    chassis.append({
        "name": "evolution-pool-chassis",
        "project": "evolution-pool-chassis",
        "status_line": "Up 9 days",
        "status": "running",
        "started_at": _iso(now - timedelta(days=9)),
        "restart_count": 0,
        "running": True,
        "stats": {"cpu": "8.94%", "mem": "412.6MiB / 7.7GiB", "mem_pct": "5.24%"},
        "agents": [
            _agent("mutator",   last_task="spawn",    mins_ago=1),
            _agent("evaluator", last_task="score",    mins_ago=1),
            _agent("selector",  last_task="cull",     mins_ago=2),
            _agent("archivist", last_task="preserve", mins_ago=23),
            _agent("manager",   last_task=None,       mins_ago=None),
        ],
        "audit": [
            {"ts": ago(1),  "tool": "variant-spawn", "args": '{"parent":"g_88231","ops":["xover","mut(0.04)"]}',                       "status": "ok",    "exit": 0, "duration_ms": 41,   "secrets_passed": []},
            {"ts": ago(1),  "tool": "fitness-score", "args": '{"genome":"g_88240"}',                                                   "status": "ok",    "exit": 0, "duration_ms": 612,  "secrets_passed": []},
            {"ts": ago(1),  "tool": "fitness-score", "args": '{"genome":"g_88241"}',                                                   "status": "ok",    "exit": 0, "duration_ms": 588,  "secrets_passed": []},
            {"ts": ago(2),  "tool": "fitness-score", "args": '{"genome":"g_88238"}',                                                   "status": "error", "exit": 1, "duration_ms": 5012, "reason": "eval sandbox timeout (5s) — variant looped", "secrets_passed": []},
            {"ts": ago(2),  "tool": "pool-prune",    "args": '{"keep_top":64,"by":"pareto"}',                                          "status": "ok",    "exit": 0, "duration_ms": 173,  "secrets_passed": []},
            {"ts": ago(2),  "tool": "kv-write",      "args": '{"key":"pool/gen/0431","size":64}',                                      "status": "ok",    "exit": 0, "duration_ms": 26,   "secrets_passed": []},
            {"ts": ago(23), "tool": "novelty-check", "args": '{"candidates":18,"threshold":0.31}',                                     "status": "ok",    "exit": 0, "duration_ms": 244,  "secrets_passed": []},
            {"ts": ago(23), "tool": "archive-write", "args": '{"added":4,"archive":"hall_of_fame"}',                                   "status": "ok",    "exit": 0, "duration_ms": 81,   "secrets_passed": []},
        ],
        "cron": [
            {"schedule": "* * * * *",    "command": "mutator spawn",      "next": in_mins(1)},
            {"schedule": "* * * * *",    "command": "evaluator score",    "next": in_mins(1)},
            {"schedule": "*/5 * * * *",  "command": "selector cull",      "next": in_mins(3)},
            {"schedule": "*/30 * * * *", "command": "archivist preserve", "next": in_mins(7)},
        ],
    })

    # 5. sys-analysis — distributed root-cause analysis on a target system.
    # log-scanner and trace-correlator independently survey their own signal
    # source and post findings to a shared incident blackboard; hypothesizer
    # reads accumulated findings and submits candidate root causes; probe
    # runs read-only tests to corroborate or refute. Bidirectional loop —
    # probe results feed back into the next round of hypotheses.
    chassis.append({
        "name": "sys-analysis-chassis",
        "project": "sys-analysis-chassis",
        "status_line": "Up 8 days",
        "status": "running",
        "started_at": _iso(now - timedelta(days=8)),
        "restart_count": 0,
        "running": True,
        "stats": {"cpu": "0.74%", "mem": "134.2MiB / 7.7GiB", "mem_pct": "1.70%"},
        "agents": [
            _agent("log-scanner",      last_task="scan",        mins_ago=1),
            _agent("trace-correlator", last_task="correlate",   mins_ago=4),
            _agent("probe",            last_task="test",        mins_ago=2),
            _agent("hypothesizer",     last_task="hypothesize", mins_ago=6),
            _agent("manager",          last_task=None,          mins_ago=None),
        ],
        "audit": [
            {"ts": ago(1), "tool": "log-window",         "args": '{"service":"checkout","window":"5m","matches":42}',                       "status": "ok",     "exit": 0, "duration_ms": 218,  "secrets_passed": []},
            {"ts": ago(1), "tool": "finding-post",       "args": '{"key":"incident/i_2031/findings","kind":"latency-spike","p99_ms":4180}', "status": "ok",     "exit": 0, "duration_ms": 41,   "secrets_passed": []},
            {"ts": ago(2), "tool": "hypothesis-fetch",   "args": '{"id":"h_0042"}',                                                         "status": "ok",     "exit": 0, "duration_ms": 19,   "secrets_passed": []},
            {"ts": ago(2), "tool": "probe-run",          "args": '{"cmd":"sql:select count(*) from pg_stat_activity"}',                     "status": "ok",     "exit": 0, "duration_ms": 412,  "secrets_passed": ["PGPASSWORD"]},
            {"ts": ago(2), "tool": "config-write",       "args": '{"key":"checkout/pool_max","value":48}',                                  "status": "denied", "exit": 2, "duration_ms": 0,    "reason": "args_schema violation: probe role is read-only (mutation requires operator approval)", "secrets_passed": []},
            {"ts": ago(2), "tool": "peer-broadcast",     "args": '{"to":"hypothesizer","event":"corroborated","id":"h_0042"}',              "status": "ok",     "exit": 0, "duration_ms": 23,   "secrets_passed": []},
            {"ts": ago(4), "tool": "trace-fetch",        "args": '{"service":"checkout-api","since":"15m"}',                                "status": "error",  "exit": 1, "duration_ms": 8014, "reason": "trace backend timeout (8s)", "secrets_passed": []},
            {"ts": ago(4), "tool": "finding-post",       "args": '{"key":"incident/i_2031/findings","kind":"slow-span","span":"db.query"}', "status": "ok",     "exit": 0, "duration_ms": 38,   "secrets_passed": []},
            {"ts": ago(6), "tool": "hypothesis-submit", "args": '{"id":"h_0042","claim":"checkout db pool exhausted","confidence":0.71}',  "status": "ok",     "exit": 0, "duration_ms": 92,   "secrets_passed": []},
        ],
        "cron": [
            {"schedule": "*/2 * * * *",  "command": "log-scanner scan",          "next": in_mins(1)},
            {"schedule": "*/5 * * * *",  "command": "trace-correlator correlate","next": in_mins(1)},
            {"schedule": "*/10 * * * *", "command": "hypothesizer hypothesize",  "next": in_mins(4)},
            {"schedule": "*/5 * * * *",  "command": "probe test",                "next": in_mins(3)},
        ],
    })

    # 6. theorem-search — conjecturer proposes lemmas, prover attempts them,
    # counterexample-search hunts for refutations, librarian curates the
    # accepted corpus. Three-way collaborative/adversarial loop sharing a
    # corpus blackboard.
    chassis.append({
        "name": "theorem-search-chassis",
        "project": "theorem-search-chassis",
        "status_line": "Up 12 days",
        "status": "running",
        "started_at": _iso(now - timedelta(days=12)),
        "restart_count": 1,
        "running": True,
        "stats": {"cpu": "4.11%", "mem": "287.2MiB / 7.7GiB", "mem_pct": "3.65%"},
        "agents": [
            _agent("conjecturer",           last_task="propose", mins_ago=8),
            _agent("prover",                last_task="prove",   mins_ago=14),
            _agent("counterexample-search", last_task="refute",  mins_ago=22),
            _agent("librarian",             last_task="curate",  mins_ago=90),
            _agent("manager",               last_task=None,      mins_ago=None),
        ],
        "audit": [
            {"ts": ago(8),  "tool": "corpus-read",       "args": '{"tag":"abelian-cat"}',                                              "status": "ok",    "exit": 0, "duration_ms": 88,   "secrets_passed": []},
            {"ts": ago(8),  "tool": "conjecture-submit","args": '{"id":"c_2204","statement":"forall f g : ..., ..."}',                 "status": "ok",    "exit": 0, "duration_ms": 64,   "secrets_passed": []},
            {"ts": ago(14), "tool": "conjecture-fetch", "args": '{"id":"c_2203"}',                                                    "status": "ok",    "exit": 0, "duration_ms": 19,   "secrets_passed": []},
            {"ts": ago(14), "tool": "proof-check",      "args": '{"id":"c_2203","backend":"lean"}',                                   "status": "ok",    "exit": 0, "duration_ms": 4218, "secrets_passed": []},
            {"ts": ago(14), "tool": "kv-write",         "args": '{"key":"proved/c_2203","backend":"lean"}',                           "status": "ok",    "exit": 0, "duration_ms": 22,   "secrets_passed": []},
            {"ts": ago(22), "tool": "conjecture-fetch", "args": '{"id":"c_2202"}',                                                    "status": "ok",    "exit": 0, "duration_ms": 18,   "secrets_passed": []},
            {"ts": ago(22), "tool": "smt-solve",        "args": '{"id":"c_2202","budget_s":30}',                                      "status": "error", "exit": 1, "duration_ms": 30041,"reason": "smt timeout: no counterexample within budget", "secrets_passed": []},
            {"ts": ago(90), "tool": "corpus-write",     "args": '{"accepted":3,"deferred":11}',                                       "status": "ok",    "exit": 0, "duration_ms": 207,  "secrets_passed": []},
        ],
        "cron": [
            {"schedule": "*/10 * * * *", "command": "conjecturer propose",          "next": in_mins(2)},
            {"schedule": "*/15 * * * *", "command": "prover prove",                 "next": in_mins(1)},
            {"schedule": "*/20 * * * *", "command": "counterexample-search refute", "next": in_mins(18)},
            {"schedule": "30 */2 * * *", "command": "librarian curate",             "next": in_mins(30)},
        ],
    })

    # 7. world-model — stopped. A curiosity-driven exploration prototype
    # (scout generates surprising states, modeler updates a learned
    # transition model, critic scores novelty). Retired in favor of the
    # evolution-pool design; kept so the dashboard's "down" card style
    # has a representative.
    chassis.append({
        "name": "world-model-chassis",
        "project": "world-model-chassis",
        "status_line": "Exited (0) 2 hours ago",
        "status": "exited",
        "started_at": _iso(now - timedelta(hours=10)),
        "restart_count": 2,
        "running": False,
        "stats": {},
    })

    # Stamp shared runtime config (harness, LLM) on every chassis so the top
    # bar can surface it. Done here rather than inline so the dict literals
    # above stay focused on the per-chassis state.
    for c in chassis:
        c.update(DEMO_RUNTIME)

    return {"generated_at": _iso(now), "chassis": chassis}


# Per-agent task pool for drill-in run lists. Falls back to "run" for unknown agents.
_DEMO_TASKS = {
    "proposer":              ["propose"],
    "validator":             ["vote"],
    "aggregator":            ["tally", "weighted-tally"],
    "reputation":            ["update"],
    "advocate-pro":          ["argue"],
    "advocate-con":          ["counter"],
    "judge":                 ["score"],
    "archivist":             ["publish-verdict", "preserve"],
    "mutator":               ["spawn"],
    "evaluator":             ["score"],
    "selector":              ["cull"],
    "log-scanner":           ["scan"],
    "trace-correlator":      ["correlate"],
    "hypothesizer":          ["hypothesize"],
    "probe":                 ["test"],
    "conjecturer":           ["propose"],
    "prover":                ["prove"],
    "counterexample-search": ["refute"],
    "librarian":             ["curate"],
}


def demo_runs(_chassis: str, agent: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    tasks = _DEMO_TASKS.get(agent, ["run"])
    runs: list[dict] = []
    # Cadence per agent — controls the spacing between rows in the drill-in.
    if agent in ("mutator", "evaluator", "validator"):
        step_mins = 1
    elif agent == "log-scanner":
        step_mins = 2
    elif agent == "proposer":
        step_mins = 3
    elif agent in ("aggregator", "selector", "trace-correlator", "probe"):
        step_mins = 5
    elif agent in ("reputation", "conjecturer", "hypothesizer"):
        step_mins = 10
    elif agent in ("advocate-pro", "advocate-con", "prover"):
        step_mins = 15
    elif agent in ("judge", "counterexample-search"):
        step_mins = 20
    elif agent == "archivist":
        step_mins = 30
    elif agent == "librarian":
        step_mins = 60 * 2
    else:
        step_mins = 60 * 12  # safe fallback
    for i in range(10):
        when = now - timedelta(minutes=step_mins * (i + 1) - 4)
        task = tasks[i % len(tasks)]
        runs.append({
            "mtime": when.timestamp(),
            "path": f"/var/log/chassis/agents/{agent}/{task}/{when.strftime('%Y-%m-%dT%H-%M-%S')}.jsonl",
            "ts": _iso(when),
            "agent": agent,
            "task": task,
            "session_file": f"/home/agent/.pi/sessions/{agent}-{when.strftime('%Y%m%dT%H%M%S')}.jsonl",
        })
    return runs


def demo_file(path: str) -> dict:
    if "sessions" in path:
        sample = [
            {"ts": "2026-05-17T07:00:05Z", "role": "system",    "content": "You are the proposer in a 3-role consensus chassis. Pull the next open question from the blackboard queue, generate 3 distinct candidate answers, post them to round/<n>/proposals, and notify the validator pool."},
            {"ts": "2026-05-17T07:00:05Z", "role": "user",      "content": "Run a proposal round."},
            {"ts": "2026-05-17T07:00:06Z", "role": "assistant", "content": "Pulling the next open question off the blackboard queue."},
            {"ts": "2026-05-17T07:00:06Z", "role": "tool",      "name": "queue-pop",       "content": "{\"id\":\"q_0418\",\"prompt\":\"shortest reduction of expr e_77\"}"},
            {"ts": "2026-05-17T07:00:07Z", "role": "assistant", "content": "Drafting three candidate reductions with deliberately divergent strategies so validators see a real spread."},
            {"ts": "2026-05-17T07:00:11Z", "role": "tool",      "name": "blackboard-post", "content": "{\"key\":\"round/0418/proposals\",\"items\":3}"},
            {"ts": "2026-05-17T07:00:11Z", "role": "assistant", "content": "Opening the round so validators can begin voting."},
            {"ts": "2026-05-17T07:00:11Z", "role": "tool",      "name": "peer-broadcast",  "content": "{\"to\":\"validator\",\"event\":\"round_open\",\"round\":418}"},
            {"ts": "2026-05-17T07:00:12Z", "role": "assistant", "content": "Done. Round 418 open with 3 proposals; aggregator will tally on its next tick."},
        ]
        return {"path": path, "content": "\n".join(json.dumps(s) for s in sample)}
    # run log
    return {
        "path": path,
        "content": json.dumps({"ts": "2026-05-17T07:00:04Z", "agent": "proposer", "task": "propose", "session_file": "/home/agent/.pi/sessions/proposer-20260517T070004.jsonl"}),
    }


# ---------- app -----------------------------------------------------------

app = FastAPI(title="chassis dashboard")
app.state.demo = False


@app.get("/api/state")
async def api_state():
    if app.state.demo:
        return JSONResponse(demo_state())
    chassis = await docker_ps_chassis()
    stats = await docker_stats_all()
    snapshots = await asyncio.gather(*(chassis_snapshot(m, stats) for m in chassis))
    return JSONResponse({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chassis": snapshots,
    })


@app.get("/api/runs/{container}/{agent}")
async def api_runs(container: str, agent: str, limit: int = 20):
    """Most-recent run records for one agent in one chassis. Each entry is the
    first JSON line of a run log under /var/log/chassis/agents/<agent>/."""
    if not (_safe_name(container) and _safe_name(agent)):
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if app.state.demo:
        return JSONResponse(demo_runs(container, agent)[:max(1, min(limit, 100))])
    limit = max(1, min(limit, 100))
    raw = await exec_in(
        container, "sh", "-c",
        f'find {AGENTS_LOG_ROOT}/{agent} -type f -name "*.jsonl" '
        f'-printf "%T@ %p\\n" 2>/dev/null | sort -n | tail -{limit}',
    )
    runs: list[dict] = []
    for line in raw.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        mtime_s, path = parts
        head = await exec_in(container, "sh", "-c", f"head -1 {shlex.quote(path)} 2>/dev/null")
        rec: dict = {}
        head_lines = head.strip().splitlines()
        if head_lines:
            try:
                rec = json.loads(head_lines[0])
            except json.JSONDecodeError:
                pass
        runs.append({"mtime": float(mtime_s), "path": path, **rec})
    runs.reverse()
    return JSONResponse(runs)


@app.get("/api/file/{container}")
async def api_file(container: str, path: str):
    """Read a file from inside a chassis container, capped at FILE_MAX_BYTES
    and restricted to ALLOWED_FILE_PREFIXES so the dashboard isn't a shell."""
    if not _safe_name(container):
        return JSONResponse({"error": "invalid container"}, status_code=400)
    if not any(path.startswith(p) for p in ALLOWED_FILE_PREFIXES):
        return JSONResponse({"error": "path not allowed"}, status_code=400)
    if app.state.demo:
        return JSONResponse(demo_file(path))
    content = await exec_in(
        container, "sh", "-c",
        f"tail -c {FILE_MAX_BYTES} {shlex.quote(path)} 2>/dev/null",
    )
    return JSONResponse({"path": path, "content": content})


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>chassis</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏎️</text></svg>">
<style>
  :root { color-scheme: dark; }
  body { font: 12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; background:#0d1117; color:#c9d1d9; margin:0; padding:0; }
  .topbar { display:flex; align-items:center; gap:18px; padding:8px 16px; background:#010409; border-bottom:1px solid #30363d; color:#c9d1d9; }
  .topbar .brand { display:flex; align-items:center; gap:8px; font-size:15px; font-weight:600; letter-spacing:.3px; }
  .topbar .brand .car { font-size:18px; filter:saturate(1.2); }
  .topbar .brand .name { color:#c9d1d9; }
  .topbar .config { display:flex; gap:14px; align-items:center; color:#c9d1d9; font-size:11.5px; padding-left:8px; border-left:1px solid #21262d; }
  .topbar .config .cfg { display:flex; align-items:baseline; gap:6px; }
  .topbar .config .cfg-k { color:#6e7681; text-transform:uppercase; font-size:10px; letter-spacing:.6px; }
  .topbar .stats-inline { display:flex; gap:14px; align-items:center; color:#8b949e; font-size:11.5px; flex:1; padding-left:8px; border-left:1px solid #21262d; }
  .topbar .stats-inline .item { display:flex; align-items:center; gap:5px; }
  .topbar .stats-inline .item .num { color:#c9d1d9; font-weight:600; }
  .topbar .stats-inline .item .dot { width:6px; height:6px; }
  .topbar .stats-inline .item.bad .num { color:#f85149; }
  .topbar .age { color:#6e7681; font-size:11px; }
  .page { padding:14px; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-bottom:14px; }
  .stat { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px 10px; display:flex; flex-direction:column; gap:4px; min-height:74px; }
  .stat .label { color:#6e7681; font-size:10px; text-transform:uppercase; letter-spacing:.6px; }
  .stat .value { font-size:18px; font-weight:600; color:#c9d1d9; line-height:1.15; }
  .stat .sub { color:#6e7681; font-size:10.5px; }
  .stat svg.gauge { display:block; width:100%; height:24px; margin-top:auto; }
  .bar { height:6px; background:#21262d; border-radius:3px; overflow:hidden; display:flex; }
  .bar > span { display:block; height:100%; }
  .bar .up { background:#3fb950; }
  .bar .down { background:#f85149; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,300px); gap:10px; justify-content:start; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:9px 11px; aspect-ratio:1/1; overflow-y:auto; display:flex; flex-direction:column; box-sizing:border-box; }
  .card::-webkit-scrollbar { width:6px; }
  .card::-webkit-scrollbar-thumb { background:#30363d; border-radius:3px; }
  .card.down { opacity:.55; }
  .card h2 { font-size:13px; margin:0 0 4px; display:flex; align-items:center; gap:7px; font-weight:600; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; flex:none; }
  .dot.up { background:#3fb950; }
  .dot.down { background:#f85149; }
  .meta { color:#8b949e; font-size:11px; margin-bottom:6px; }
  section { margin-top:6px; }
  .label { color:#6e7681; font-size:10px; text-transform:uppercase; letter-spacing:.6px; margin-bottom:2px; }
  .line { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .line.deny { color:#f85149; }
  .ts { color:#6e7681; display:inline-block; min-width:5.5em; }
  table { width:100%; border-collapse:collapse; font-size:11.5px; }
  td { padding:1px 6px 1px 0; vertical-align:top; }
  td.k { color:#8b949e; white-space:nowrap; }
  .empty { color:#6e7681; font-style:italic; font-size:11.5px; }
  code { background:#21262d; padding:1px 5px; border-radius:3px; font-size:11px; }
  .pill { display:inline-block; padding:1px 6px; border-radius:10px; font-size:10px; background:#21262d; color:#8b949e; }
  .clickable { cursor:pointer; }
  .clickable:hover { background:#1f2630; }
  .modal { position:fixed; inset:0; z-index:50; display:flex; align-items:center; justify-content:center; }
  .modal[hidden] { display:none; }
  .modal-backdrop { position:absolute; inset:0; background:rgba(0,0,0,.7); }
  .modal-body { position:relative; background:#161b22; border:1px solid #30363d; border-radius:8px; max-width:min(90vw,1100px); max-height:85vh; min-width:480px; display:flex; flex-direction:column; box-shadow:0 8px 32px rgba(0,0,0,.6); }
  .modal-head { display:flex; align-items:center; justify-content:space-between; padding:10px 14px; border-bottom:1px solid #30363d; gap:12px; }
  .modal-title { font-size:13px; font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .modal-close { background:none; border:none; color:#8b949e; cursor:pointer; font-size:14px; padding:0 4px; }
  .modal-close:hover { color:#c9d1d9; }
  .modal-content { padding:12px 14px; overflow:auto; flex:1; font-size:11.5px; }
  .modal-content pre { white-space:pre-wrap; word-break:break-word; margin:0; font-family:inherit; color:#c9d1d9; }
  .modal-content table { margin-top:4px; }
  .modal-content td { padding:3px 10px 3px 0; }
  .modal-content a.link { color:#58a6ff; cursor:pointer; text-decoration:underline; }
  .modal-content .back { display:inline-block; margin-bottom:8px; color:#8b949e; cursor:pointer; }
  .modal-content .back:hover { color:#c9d1d9; }
  .badges { position:fixed; bottom:10px; right:12px; display:flex; gap:6px; z-index:40; pointer-events:none; }
  .badge { padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; letter-spacing:.6px; text-transform:uppercase; border:1px solid; background:#0d1117; }
  .badge.beta { color:#d29922; border-color:#d29922; background:rgba(210,153,34,.10); }
  .badge.sample { color:#8b949e; border-color:#30363d; background:#161b22; }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand"><span class="car">🏎️</span><span class="name">chassis</span></div>
  <div class="config" id="topconfig"></div>
  <div class="stats-inline" id="topstats"></div>
  <div class="age" id="age">loading…</div>
</div>
<div class="page">
  <div class="stats" id="stats"></div>
  <div class="grid" id="grid"></div>
</div>
<div id="modal" class="modal" hidden>
  <div class="modal-backdrop"></div>
  <div class="modal-body">
    <div class="modal-head"><span class="modal-title"></span><button class="modal-close" type="button">✕</button></div>
    <div class="modal-content"></div>
  </div>
</div>
<script>
function escape(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function parseNum(s){if(s==null)return 0;const m=String(s).match(/-?[\d.]+/);return m?parseFloat(m[0]):0;}
// Grafana retro-LCD bar gauge: the track is composed of N small vertical
// segments. Segments left of the value are "lit" using the gradient color
// at their position (green near 0%, amber near 55%, red near 100%); segments
// to the right are dim. Snapping the fill to a segment boundary keeps the
// look crisp — no half-lit cells.
const GAUGE_W = 120, GAUGE_H = 24, GAUGE_BAR_Y = 5, GAUGE_BAR_H = 14;
const GAUGE_SEGMENTS = 44;
const GAUGE_SEG_GAP  = 0.5;
const GAUGE_SEG_W    = (GAUGE_W - GAUGE_SEG_GAP * (GAUGE_SEGMENTS - 1)) / GAUGE_SEGMENTS;
const GAUGE_DIM      = "#21262d";

// Three-stop gradient: green (0%) → amber (55%) → red (100%). Linear interp
// in straight RGB — fine for this short range and matches Grafana's "Continuous
// Green Yellow Red" feel without dragging in HSL math.
const GAUGE_STOPS = [
  [0.00, [0x3f, 0xb9, 0x50]],
  [0.55, [0xd2, 0x99, 0x22]],
  [1.00, [0xf8, 0x51, 0x49]],
];
function gaugeColor(t){
  for (let i = 0; i < GAUGE_STOPS.length - 1; i++){
    const [t1, c1] = GAUGE_STOPS[i], [t2, c2] = GAUGE_STOPS[i+1];
    if (t <= t2){
      const f = (t - t1) / (t2 - t1);
      const r = Math.round(c1[0] + (c2[0]-c1[0])*f);
      const g = Math.round(c1[1] + (c2[1]-c1[1])*f);
      const b = Math.round(c1[2] + (c2[2]-c1[2])*f);
      return `#${((r<<16)|(g<<8)|b).toString(16).padStart(6,"0")}`;
    }
  }
  return GAUGE_DIM;
}

function gauge(val, max){
  const pct = max > 0 ? Math.max(0, Math.min(val/max, 1)) : 0;
  const filled = Math.round(pct * GAUGE_SEGMENTS);
  let rects = "";
  for (let i = 0; i < GAUGE_SEGMENTS; i++){
    const x = i * (GAUGE_SEG_W + GAUGE_SEG_GAP);
    const fill = i < filled ? gaugeColor((i + 0.5) / GAUGE_SEGMENTS) : GAUGE_DIM;
    rects += `<rect x="${x.toFixed(2)}" y="${GAUGE_BAR_Y}" width="${GAUGE_SEG_W.toFixed(2)}" height="${GAUGE_BAR_H}" rx="1" fill="${fill}"/>`;
  }
  return `<svg class="gauge" viewBox="0 0 ${GAUGE_W} ${GAUGE_H}" preserveAspectRatio="none">${rects}</svg>`;
}
function renderTopStats(data){
  const cs=data.chassis||[];
  const up=cs.filter(c=>c.running).length, total=cs.length;
  const agents=cs.reduce((a,c)=>a+((c.agents||[]).length),0);
  const hourAgo=Date.now()-3600*1000;
  let calls=0, bad=0;
  for(const c of cs) for(const e of (c.audit||[])){
    const t=Date.parse(e.ts); if(!(t&&t>=hourAgo)) continue;
    calls++;
    if(e.status==="denied"||(e.exit&&e.exit!==0)) bad++;
  }
  const allUp=total>0&&up===total;

  // Runtime config — surface from the first chassis that reports any of the
  // known fields. When chassis disagree the operator should look at the
  // individual cards, so the top bar keeps a single representative value
  // rather than averaging or "mixed".
  const CFG_FIELDS = ["harness", "model", "source"];
  const cfgSrc = cs.find(c => CFG_FIELDS.some(k => c[k])) || {};
  const cfgItems = CFG_FIELDS
    .filter(k => cfgSrc[k])
    .map(k => `<div class="cfg"><span class="cfg-k">${k}</span><span>${escape(cfgSrc[k])}</span></div>`);
  document.getElementById("topconfig").innerHTML = cfgItems.join("");

  document.getElementById("topstats").innerHTML=`
    <div class="item"><span class="dot ${allUp?"up":"down"}"></span><span class="num">${up}/${total}</span> chassis</div>
    <div class="item"><span class="num">${agents}</span> agents</div>
    <div class="item ${bad>0?"bad":""}"><span class="num">${calls}</span> calls/hr${bad?` <span class="num">· ${bad} bad</span>`:""}</div>`;
}
// Gauge scales: chosen so a typical-load chassis sits ~40-60%, leaving room
// for the gradient to communicate "headroom" vs "approaching limit."
const GAUGE_MAX_CPU   = 400;   // 4 fully-saturated cores
const GAUGE_MAX_MEM   = 100;   // % of host
const GAUGE_MAX_TOOLS = 50;    // calls/hr is "busy"
function renderStats(data){
  const cs=data.chassis||[];
  const cpuTotal=cs.reduce((a,c)=>a+parseNum(c.stats&&c.stats.cpu),0);
  const memTotal=cs.reduce((a,c)=>a+parseNum(c.stats&&c.stats.mem_pct),0);
  const hourAgo=Date.now()-3600*1000;
  const toolsHour=cs.reduce((a,c)=>a+((c.audit||[]).filter(e=>{const t=Date.parse(e.ts);return t&&t>=hourAgo;}).length),0);
  const denyHour=cs.reduce((a,c)=>a+((c.audit||[]).filter(e=>{const t=Date.parse(e.ts);if(!(t&&t>=hourAgo))return false;return e.status==="denied"||(e.exit&&e.exit!==0);}).length),0);
  const el=document.getElementById("stats");
  el.innerHTML=`
    <div class="stat">
      <div class="label">cpu total</div>
      <div class="value">${cpuTotal.toFixed(1)}<span class="sub">%</span></div>
      ${gauge(cpuTotal, GAUGE_MAX_CPU)}
    </div>
    <div class="stat">
      <div class="label">memory total</div>
      <div class="value">${memTotal.toFixed(1)}<span class="sub">% host</span></div>
      ${gauge(memTotal, GAUGE_MAX_MEM)}
    </div>
    <div class="stat">
      <div class="label">tool calls (1h)</div>
      <div class="value">${toolsHour}${denyHour?` <span class="sub">· ${denyHour} bad</span>`:""}</div>
      ${gauge(toolsHour, GAUGE_MAX_TOOLS)}
    </div>`;
}
function relTime(iso){
  if(!iso) return "—";
  const d=new Date(iso); if(isNaN(d)) return iso;
  const diff=(Date.now()-d.getTime())/1000;
  const a=Math.abs(diff), sfx=diff>=0?"ago":"";
  let v;
  if(a<60) v=Math.floor(a)+"s";
  else if(a<3600) v=Math.floor(a/60)+"m";
  else if(a<86400) v=Math.floor(a/3600)+"h";
  else v=Math.floor(a/86400)+"d";
  return diff>=0?`${v} ago`:`in ${v}`;
}
function renderAudit(audit){
  if(!audit||!audit.length) return '<div class="empty">no audit entries</div>';
  return audit.slice(0,4).map(e=>{
    const bad=e.status==="denied"||(e.exit&&e.exit!==0);
    const note=bad?` ✗ ${escape(e.reason||"exit "+e.exit)}`:"";
    return `<div class="line clickable ${bad?"deny":""}" data-audit="${escape(JSON.stringify(e))}"><span class="ts">${escape(relTime(e.ts))}</span> ${escape(e.tool)}${note}</div>`;
  }).join("");
}
function renderAgents(chassis,agents){
  if(!agents||!agents.length) return '<div class="empty">no agents</div>';
  return '<table>'+agents.map(a=>{
    const r=a.last_run;
    const when=r?escape(relTime(new Date(r.mtime*1000).toISOString())):'<span class="empty">never</span>';
    const task=r?(r.task?`<code>${escape(r.task)}</code>`:'<span class="empty">interactive</span>'):'';
    return `<tr class="clickable" data-chassis="${escape(chassis)}" data-agent="${escape(a.name)}"><td class="k">${escape(a.name)}</td><td>${task}</td><td>${when}</td></tr>`;
  }).join("")+'</table>';
}
function renderCron(jobs){
  if(!jobs||!jobs.length) return '<div class="empty">no cron jobs</div>';
  return '<table>'+jobs.map(j=>{
    const next=j.next?escape(relTime(j.next)):'<span class="empty">—</span>';
    return `<tr><td class="k">${escape(j.schedule)}</td><td>${escape(j.command)}</td><td>${next}</td></tr>`;
  }).join("")+'</table>';
}
function renderChassis(c){
  const up=c.running;
  const stats=c.stats||{};
  const statBits=up&&stats.cpu?` · cpu ${escape(stats.cpu)} · mem ${escape(stats.mem_pct||stats.mem||"")}`:"";
  return `
    <div class="card ${up?"":"down"}">
      <h2><span class="dot ${up?"up":"down"}"></span>${escape(c.name)} <span class="pill">${escape(c.status||"unknown")}</span></h2>
      <div class="meta">${escape(c.status_line||"")} · restarts ${c.restart_count??0}${statBits}</div>
      ${up?`
        <section><div class="label">agents</div>${renderAgents(c.name,c.agents)}</section>
        <section><div class="label">cron</div>${renderCron(c.cron)}</section>
        <section><div class="label">recent tools</div>${renderAudit(c.audit)}</section>
      `:""}
    </div>`;
}
let lastN=0;
function layoutCards(n){
  const grid=document.getElementById("grid");
  if(!grid) return;
  if(!n){ grid.style.gridTemplateColumns=""; return; }
  const gap=10, minSize=180, maxSize=420;
  const W=grid.clientWidth;
  const top=grid.getBoundingClientRect().top;
  const H=Math.max(window.innerHeight-top-16,200);
  let best={size:0,cols:1};
  for(let cols=1;cols<=n;cols++){
    const rows=Math.ceil(n/cols);
    const s=Math.min((W-gap*(cols-1))/cols,(H-gap*(rows-1))/rows);
    if(s>best.size) best={size:s,cols};
  }
  const size=Math.max(minSize,Math.min(maxSize,Math.floor(best.size)));
  grid.style.gridTemplateColumns=`repeat(${best.cols}, ${size}px)`;
}
// --- modal ---------------------------------------------------------------
function openModal(title,html){
  document.querySelector('.modal-title').textContent=title;
  document.querySelector('.modal-content').innerHTML=html;
  document.getElementById('modal').hidden=false;
}
function closeModal(){ document.getElementById('modal').hidden=true; }
function showAudit(e){
  const bad=e.status==="denied"||(e.exit&&e.exit!==0);
  openModal(`${e.tool} · ${bad?(e.status==="denied"?"denied":"exit "+e.exit):"ok"}`,`<pre>${escape(JSON.stringify(e,null,2))}</pre>`);
}
async function showAgent(chassis,agent){
  openModal(`${chassis} · ${agent}`,'<div class="empty">loading…</div>');
  try{
    const r=await fetch(`/api/runs/${encodeURIComponent(chassis)}/${encodeURIComponent(agent)}?limit=20`);
    const runs=await r.json();
    if(!runs.length){ document.querySelector('.modal-content').innerHTML='<div class="empty">no runs recorded</div>'; return; }
    const rows=runs.map(r=>{
      const when=escape(relTime(new Date(r.mtime*1000).toISOString()));
      const task=r.task?`<code>${escape(r.task)}</code>`:'<span class="empty">interactive</span>';
      const sess=r.session_file?`<a class="link" data-session="${escape(r.session_file)}" data-chassis="${escape(chassis)}">session</a>`:'<span class="empty">—</span>';
      const log=`<a class="link" data-file="${escape(r.path)}" data-chassis="${escape(chassis)}">run log</a>`;
      return `<tr><td class="k">${when}</td><td>${task}</td><td>${log}</td><td>${sess}</td></tr>`;
    }).join("");
    document.querySelector('.modal-content').innerHTML=`<table><thead><tr><td class="k">when</td><td class="k">task</td><td class="k">run log</td><td class="k">pi session</td></tr></thead>${rows}</table>`;
  }catch(err){
    document.querySelector('.modal-content').innerHTML=`<div class="empty">fetch error: ${escape(err.message)}</div>`;
  }
}
async function showFile(chassis,path,title){
  const backHTML=`<a class="back" id="modal-back">← back</a>`;
  openModal(title,backHTML+'<div class="empty">loading…</div>');
  try{
    const r=await fetch(`/api/file/${encodeURIComponent(chassis)}?path=${encodeURIComponent(path)}`);
    const j=await r.json();
    if(j.error){ document.querySelector('.modal-content').innerHTML=backHTML+`<div class="empty">${escape(j.error)}</div>`; return; }
    document.querySelector('.modal-content').innerHTML=backHTML+`<div style="color:#6e7681;margin-bottom:6px">${escape(path)}</div><pre>${escape(j.content||"(empty)")}</pre>`;
  }catch(err){
    document.querySelector('.modal-content').innerHTML=backHTML+`<div class="empty">fetch error: ${escape(err.message)}</div>`;
  }
}
document.addEventListener('click',e=>{
  if(e.target.matches('.modal-backdrop, .modal-close')){ closeModal(); return; }
  if(e.target.id==='modal-back' || e.target.classList.contains('back')){
    if(lastAgentCtx){ showAgent(lastAgentCtx.chassis,lastAgentCtx.agent); }
    return;
  }
  const sess=e.target.closest('[data-session]');
  if(sess){ showFile(sess.dataset.chassis,sess.dataset.session,'pi session'); return; }
  const file=e.target.closest('[data-file]');
  if(file){ showFile(file.dataset.chassis,file.dataset.file,'run log'); return; }
  const audit=e.target.closest('[data-audit]');
  if(audit){ try{ showAudit(JSON.parse(audit.dataset.audit)); }catch(_){} return; }
  const agentRow=e.target.closest('[data-agent]');
  if(agentRow){ lastAgentCtx={chassis:agentRow.dataset.chassis,agent:agentRow.dataset.agent}; showAgent(lastAgentCtx.chassis,lastAgentCtx.agent); }
});
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });
let lastAgentCtx=null;

async function tick(){
  try{
    const r=await fetch("/api/state");
    const data=await r.json();
    renderTopStats(data);
    renderStats(data);
    const g=document.getElementById("grid");
    const n=data.chassis.length;
    g.innerHTML=n?data.chassis.map(renderChassis).join(""):'<div class="empty">no chassis containers found</div>';
    lastN=n;
    layoutCards(n);
    document.getElementById("age").textContent="updated "+relTime(data.generated_at);
  }catch(e){
    document.getElementById("age").textContent="fetch error: "+e.message;
  }
}
window.addEventListener("resize",()=>layoutCards(lastN));
tick();
setInterval(tick,3000);
</script>
<div class="badges">
  <span class="badge beta">beta</span>
  <span class="badge sample">sample-data</span>
</div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--demo", action="store_true",
        help="serve synthetic state instead of querying docker — useful for screenshots and UI work",
    )
    args = ap.parse_args()
    app.state.demo = args.demo
    if args.demo:
        print(f"chassis dashboard: serving DEMO data on http://{args.host}:{args.port}")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
