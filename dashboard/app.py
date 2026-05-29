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
chassis dashboard — local web monitor for a single chassis container.

Targets one container whose docker-compose project name ends with
"-chassis" and exposes an auto-refreshing page at http://127.0.0.1:8765
showing:
  - container status, uptime, restart count
  - CPU / memory usage
  - parsed cron schedule with next-fire times
  - per-agent last-run timestamp + task
  - tail of the dispatcher audit log (deny/error highlighted)

Click an audit row or agent row to drill in.

Picks the chassis automatically when exactly one is running; pass
`--chassis <name>` when multiple are running.

See dashboard/README.md for setup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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
TRIGGER_LOG = "/var/log/chassis/triggers.log"
# Drill-in file reads are restricted to these prefixes inside the container.
# Anything else returns 400 — the dashboard isn't a generic shell. AGENT_HOME
# is read from the env so a non-default chassis layout (e.g. per-user homes)
# still allows the dashboard to peek at .pi/ session files.
_AGENT_HOME = os.environ.get("AGENT_HOME", "/home/agent")
ALLOWED_FILE_PREFIXES = (f"{_AGENT_HOME}/.pi/", "/var/log/chassis/")
FILE_MAX_BYTES = 200_000

# Override target; set from --chassis. None = auto-pick when exactly one
# chassis container is running. Env var fallback lets the uvicorn --reload
# subprocess (which re-imports this module instead of running main) inherit
# the value chosen on the command line.
CHASSIS_NAME: str | None = os.environ.get("CHASSIS_NAME") or None

# Stamped at module import; changes whenever the server restarts. The dashboard
# polls this so the browser auto-reloads after a --reload-triggered restart.
SERVER_BOOT = datetime.now(timezone.utc).isoformat()

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


# When the container has no compose-set CPU/mem limit, the gauge ceiling
# defaults to this fraction of host capacity. A single chassis shouldn't
# normally consume the whole host, so 50% gives the gauge meaningful
# headroom — a half-full bar means "using a quarter of the host," and
# crossing 100% (which clamps + turns red) is a real "hot" signal.
HOST_FALLBACK_FRACTION = 0.5


def host_cpu_cores() -> float:
    return float(os.cpu_count() or 1)


def host_memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return 0


def _parse_cpuset(s: str) -> int:
    """`CpusetCpus` is a comma-separated list of indices and ranges like
    "0,2-4,7". Returns the count of distinct CPUs it pins to."""
    n = 0
    for tok in (s or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            try:
                n += int(b) - int(a) + 1
            except ValueError:
                pass
        elif tok.isdigit():
            n += 1
    return n


async def container_limits(name: str) -> dict:
    """Return the effective CPU/memory ceiling for this container so the
    dashboard gauges scale to "% of what the container can actually use,"
    not "% of host." Compose's `cpus:` populates NanoCpus, `mem_limit:`
    populates Memory; `cpuset:` populates CpusetCpus. When all three are
    unset (the chassis default), we fall back to host totals."""
    rc, out, _ = await sh(
        "docker", "inspect", name, "--format",
        "{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.CpusetCpus}}",
    )
    nano = 0
    mem = 0
    cpuset = ""
    if rc == 0:
        parts = out.strip().split("|")
        if len(parts) == 3:
            try:
                nano = int(parts[0] or 0)
                mem = int(parts[1] or 0)
                cpuset = parts[2] or ""
            except ValueError:
                pass
    if nano > 0:
        cpu_cores = nano / 1e9
        cpu_source = "limit"
    elif cpuset:
        n = _parse_cpuset(cpuset)
        if n > 0:
            cpu_cores = float(n)
            cpu_source = "cpuset"
        else:
            cpu_cores = host_cpu_cores() * HOST_FALLBACK_FRACTION
            cpu_source = "host"
    else:
        cpu_cores = host_cpu_cores() * HOST_FALLBACK_FRACTION
        cpu_source = "host"
    if mem > 0:
        mem_bytes = mem
        mem_source = "limit"
    else:
        mem_bytes = int(host_memory_bytes() * HOST_FALLBACK_FRACTION)
        mem_source = "host"
    return {
        "cpu_cores": cpu_cores,
        "cpu_source": cpu_source,
        "mem_bytes": mem_bytes,
        "mem_source": mem_source,
    }


async def docker_stats(name: str) -> dict:
    rc, out, _ = await sh(
        "docker", "stats", name, "--no-stream", "--format",
        "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
        timeout=10.0,
    )
    if rc != 0:
        return {}
    parts = out.strip().split("\t")
    if len(parts) < 3:
        return {}
    return {"cpu": parts[0], "mem": parts[1], "mem_pct": parts[2]}


async def exec_in(container: str, *cmd: str, user: str = "agent", timeout: float = 4.0) -> str:
    rc, out, _ = await sh(
        "docker", "exec", "-u", user, container, *cmd, timeout=timeout,
    )
    return out if rc == 0 else ""


async def tail_audit(container: str, n: int = 50) -> list[dict]:
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
        f"for f in {_AGENT_HOME}/*/agent.json; do "
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
        cmd = re.sub(r"^/usr/local/bin/run-agent\s+", "", cmd)
        next_fire = None
        next_fires: list[str] = []
        if croniter:
            try:
                it = croniter(expr, now)
                deadline = now + timedelta(hours=24)
                # Cap enumeration so "* * * * *" (1440 fires/day) doesn't blow
                # up the JSON payload. 300 covers everything */5-and-slower
                # fully within 24h; denser exprs render as a near-continuous
                # bar regardless.
                for _ in range(300):
                    t = it.get_next(datetime)
                    if t > deadline:
                        break
                    next_fires.append(t.replace(tzinfo=timezone.utc).isoformat())
                if next_fires:
                    next_fire = next_fires[0]
            except Exception:
                pass
        jobs.append({
            "schedule": expr,
            "command": cmd,
            "next": next_fire,
            "next_fires": next_fires,
        })
    return jobs


# Trigger files are parsed at fire time by run-agent, so we read them straight
# from each task directory rather than from a pre-rendered index.
TRIGGER_LINE_RE = re.compile(
    r"^(?P<kind>\S+)\s+(?P<ref>\S+)"
    r"(?:\s+when\s+(?P<wkey>\S+)\s+(?P<wop>==|!=|<=|>=|<|>)\s+(?P<wval>\S+))?"
)
# Fan-out log lines look like:
#   [2026-05-20T13:27:18Z] trigger writer/draft (on_success rc=0 verdict={score=7}) -> editor/polish when score > 5
FIRE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] trigger (?P<src_agent>[^/]+)/(?P<src_task>\S+) "
    r"\((?P<kind>\S+) rc=(?P<rc>-?\d+)(?: verdict=\{(?P<verdict>[^}]*)\})?\) "
    r"-> (?P<dst_agent>[^/]+)/(?P<dst_task>\S+?)"
    r"(?:\s+when\s+(?P<wkey>\S+)\s+(?P<wop>==|!=|<=|>=|<|>)\s+(?P<wval>\S+))?$"
)


async def task_graph(container: str) -> list[dict]:
    """One entry per task directory under /home/agent/<agent>/<task>/, with
    its cron line and parsed trigger (if any). Source of truth for the
    trigger graph: matches what run-agent's fan-out actually reads."""
    raw = await exec_in(
        container, "sh", "-c",
        # Reading the per-task `cron` and `trigger` files directly (rather
        # than /etc/cron.d/chassis) keeps the graph aligned with run-agent's
        # view, including changes not yet picked up by reload-cron.
        "for d in /home/agent/*/*/; do "
        "  [ -f \"$d/INSTRUCTIONS.md\" ] || continue; "
        "  task=$(basename \"$d\"); "
        "  agent=$(basename \"$(dirname \"$d\")\"); "
        "  cron=$(grep -v '^[[:space:]]*#' \"$d/cron\" 2>/dev/null | grep -v '^[[:space:]]*$' | head -1 | sed 's/^[[:space:]]*//'); "
        "  trig=$(grep -v '^[[:space:]]*#' \"$d/trigger\" 2>/dev/null | grep -v '^[[:space:]]*$' | head -1 | sed 's/^[[:space:]]*//'); "
        # Per-task last-run mtime drives the dashboard's recency glow. Same shape
        # as agent_last_run's find, scoped to this (agent,task) only.
        "  last=$(find /var/log/chassis/agents/$agent/$task -type f -name '*.jsonl' "
        "    -printf '%T@\\n' 2>/dev/null | sort -n | tail -1); "
        "  printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \"$agent\" \"$task\" \"$cron\" \"$trig\" \"$last\"; "
        "done",
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tasks: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        agent, task, cron_line, trig_line = parts[0], parts[1], parts[2] or None, parts[3] or None
        last_run_ts: float | None = None
        if len(parts) >= 5 and parts[4]:
            try:
                last_run_ts = float(parts[4])
            except ValueError:
                pass
        next_fire = None
        if cron_line and croniter:
            try:
                next_fire = croniter(cron_line, now).get_next(datetime).replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        trigger = None
        if trig_line:
            tm = TRIGGER_LINE_RE.match(trig_line)
            if tm and "/" in tm["ref"]:
                src_agent, src_task = tm["ref"].split("/", 1)
                when_clause = None
                if tm["wkey"]:
                    when_clause = {"key": tm["wkey"], "op": tm["wop"], "value": tm["wval"]}
                trigger = {
                    "kind": tm["kind"],
                    "agent": src_agent,
                    "task": src_task,
                    "when": when_clause,
                }
        tasks.append({
            "agent": agent,
            "task": task,
            "cron": cron_line,
            "next": next_fire,
            "trigger": trigger,
            "last_run_ts": last_run_ts,
        })
    tasks.sort(key=lambda t: (t["agent"], t["task"]))
    return tasks


async def trigger_fires(container: str, n: int = 30) -> list[dict]:
    raw = await exec_in(
        container, "sh", "-c",
        f"tail -n {n} {TRIGGER_LOG} 2>/dev/null",
    )
    fires: list[dict] = []
    for line in raw.splitlines():
        m = FIRE_RE.match(line)
        if not m:
            continue
        # verdict={k=v,k2=v2} → {"k": "v", "k2": "v2"}. The string is what
        # fire_triggers wrote, so we don't have to defend against arbitrary
        # punctuation — but skip malformed pairs defensively.
        verdict: dict[str, str] = {}
        vstr = m["verdict"] or ""
        for pair in vstr.split(","):
            if "=" in pair:
                k, _, v = pair.partition("=")
                if k:
                    verdict[k] = v
        when_clause = None
        if m["wkey"]:
            when_clause = {"key": m["wkey"], "op": m["wop"], "value": m["wval"]}
        fires.append({
            "ts": m["ts"],
            "src_agent": m["src_agent"], "src_task": m["src_task"],
            "kind": m["kind"],
            "rc": int(m["rc"]),
            "verdict": verdict,
            "dst_agent": m["dst_agent"], "dst_task": m["dst_task"],
            "when": when_clause,
        })
    fires.reverse()
    return fires


# LLM_* keys are injected into the container via compose `env_file: harness/llm.env`,
# so they're readable from any `docker exec` shell (not just descendants of the
# entrypoint). Order here drives the order in the Configuration modal.
CONFIG_KEYS = (
    ("LLM_MODEL_ID", "model"),
    ("LLM_CONTEXT_WINDOW", "context_window"),
    ("LLM_MAX_TOKENS", "max_tokens"),
    ("LLM_THINKING", "thinking"),
    ("LLM_API_KIND", "api_kind"),
    ("LLM_BASE_URL", "base_url"),
)


async def chassis_config(container: str) -> dict:
    """Pull LLM_* env vars from inside the container. The compose file injects
    them via env_file, so they're visible in any exec'd shell's environment."""
    raw = await exec_in(
        container, "sh", "-c",
        "for k in " + " ".join(k for k, _ in CONFIG_KEYS) + "; do "
        '  printf "%s\\t%s\\n" "$k" "$(printenv "$k" 2>/dev/null)"; '
        "done; "
        # Best-effort harness id: which CLI run-agent invokes. Cheap probe.
        'printf "HARNESS\\t%s\\n" "$(command -v pi >/dev/null 2>&1 && echo pi || echo unknown)"',
    )
    cfg: dict = {}
    for line in raw.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        key, val = parts[0], parts[1]
        if not val:
            continue
        if key == "HARNESS":
            cfg["harness"] = val
            continue
        for env_key, out_key in CONFIG_KEYS:
            if env_key == key:
                cfg[out_key] = val
                break
    return cfg


# Matches `run-agent <agent> <task>` in process listings — covers cron-triggered
# runs and trigger fan-outs (both go through /usr/local/bin/run-agent).
RUN_AGENT_RE = re.compile(r"(?:^|/)run-agent\s+(\S+)\s+(\S+)")


async def running_tasks(container: str) -> set[tuple[str, str]]:
    """Set of (agent, task) tuples whose run-agent process is currently
    executing. Process-based detection is reliable; jsonl mtime isn't,
    because the run log is one line per session, not appended throughout."""
    raw = await exec_in(
        container, "sh", "-c",
        "ps -eo args 2>/dev/null",
        user="root",
    )
    found: set[tuple[str, str]] = set()
    for line in raw.splitlines():
        m = RUN_AGENT_RE.search(line)
        if not m:
            continue
        found.add((m.group(1), m.group(2)))
    return found


async def chassis_snapshot(meta: dict) -> dict:
    name = meta["name"]
    inspect, stats, limits = await asyncio.gather(
        docker_inspect(name),
        docker_stats(name),
        container_limits(name),
    )
    snap: dict = {
        "name": name,
        "project": meta["project"],
        "status_line": meta["status_line"],
        "stats": stats,
        "limits": limits,
        **inspect,
    }
    if not inspect.get("running"):
        return snap
    audit, agents, jobs, tasks, fires, config, running = await asyncio.gather(
        tail_audit(name, 50),
        list_agents(name),
        cron_jobs(name),
        task_graph(name),
        trigger_fires(name),
        chassis_config(name),
        running_tasks(name),
    )
    agent_runs = await asyncio.gather(*(agent_last_run(name, a) for a in agents))
    # An agent counts as "running" if any of its tasks has an active run-agent
    # process. Tasks themselves keep a per-task flag for graph-node pulsing.
    running_agents = {a for a, _ in running}
    for t in tasks:
        t["running"] = (t["agent"], t["task"]) in running
    snap["agents"] = [
        {"name": a, "last_run": r, "running": a in running_agents}
        for a, r in zip(agents, agent_runs)
    ]
    snap["audit"] = audit
    snap["cron"] = jobs
    snap["tasks"] = tasks
    snap["fires"] = fires
    snap["config"] = config
    return snap


async def resolve_chassis() -> tuple[dict | None, str | None]:
    """Return (meta, error). meta is the chassis to render; error is a
    human-readable message when we can't pick one."""
    chassis = await docker_ps_chassis()
    if CHASSIS_NAME:
        meta = next((m for m in chassis if m["name"] == CHASSIS_NAME), None)
        if not meta:
            return None, f"chassis '{CHASSIS_NAME}' not found"
        return meta, None
    if not chassis:
        return None, "no -chassis containers found"
    if len(chassis) > 1:
        names = ", ".join(c["name"] for c in chassis)
        return None, f"multiple chassis found ({names}); pass --chassis <name>"
    return chassis[0], None


# ---------- app -----------------------------------------------------------

app = FastAPI(title="chassis dashboard")


@app.get("/api/state")
async def api_state():
    meta, err = await resolve_chassis()
    if err or meta is None:
        return JSONResponse({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "server_boot": SERVER_BOOT,
            "error": err,
            "chassis": None,
        })
    snap = await chassis_snapshot(meta)
    return JSONResponse({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server_boot": SERVER_BOOT,
        "chassis": snap,
    })


@app.get("/api/runs/{container}/{agent}")
async def api_runs(container: str, agent: str, limit: int = 20, task: str | None = None):
    """Most-recent run records for one agent in one chassis. Each entry is the
    first JSON line of a run log under /var/log/chassis/agents/<agent>/.
    Pass ?task=<task> to restrict to a single task subdirectory."""
    if not (_safe_name(container) and _safe_name(agent)):
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if task is not None and not _safe_name(task):
        return JSONResponse({"error": "invalid task"}, status_code=400)
    limit = max(1, min(limit, 100))
    scope = f"{AGENTS_LOG_ROOT}/{agent}/{task}" if task else f"{AGENTS_LOG_ROOT}/{agent}"
    raw = await exec_in(
        container, "sh", "-c",
        f'find {scope} -type f -name "*.jsonl" '
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
  :root {
    color-scheme: dark;
    /* Fluid root font-size: the whole UI is sized in rem off this value, so the
       dashboard scales proportionally to the screen ("fits the scale of any
       screen") instead of staying a fixed-px island. The clamp tracks both
       width and height so it shrinks on short viewports (helping the no-scroll
       layout) and grows on large/4K monitors. Tuned so 1rem ≈ 16px at a
       1440×900 laptop — the size the px design was originally hand-tuned at —
       then scales out from there. SVG graph internals are NOT in rem; they
       scale via the graph's own viewBox. */
    font-size: clamp(13px, 0.45vw + 0.45vh + 5.5px, 21px);
    /* Neutral-grey charcoal palette (R = G = B at every step) — no blue cast.
       Step sizes preserve clear separation between background, card surface,
       and the hero block. */
    --bg-topbar:     #050505;
    --bg-base:       #0c0c0c;
    --bg-card:       #181818;
    --bg-sub:        #242424;   /* pill surface */
    --bg-input:      #0a0a0a;   /* deep-recessed (code wells) */
    --bg-hover:      #2a2a2a;
    --border-1:      #262626;
    --border-2:      #3a3a3a;
    --text-1:    #f0f0f0;
    --text-2:    #c6c6c6;
    --text-3:    #8a8a8a;
    --text-4:    #5a5a5a;
    /* Brand: chassis red (logo, .name, badges, "running" pulse, brand hover).
       Distinct from error states so they don't read as failures. */
    --red:       #ef4444;
    --red-soft:  #fca5a5;
    --red-dim:   #5a1a1a;
    /* Error / failure / denial — warm amber. Separated from brand so a failed
       on_failure edge doesn't compete with the brand wordmark or the running
       pulse, and so the operator can scan "something went wrong" at a glance. */
    --danger:      #f59e0b;
    --danger-soft: #fcd34d;
    --danger-dim:  #4a3406;
    --green:       #4ade80;
  }
  html, body { height:100%; }
  html { overflow:hidden; }
  body { font: 0.781rem/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    background:var(--bg-base); color:var(--text-2);
    font-variant-numeric: tabular-nums;
    margin:0; padding:0; display:flex; flex-direction:column; overflow:hidden; }
  /* Bloomberg-leaning chrome: hard rules, no rounded corners, no drop shadow.
     The topbar is the deepest tone in the palette and is separated from the
     page by a single 1px border-2 rule. */
  .topbar { display:flex; align-items:center; gap:1rem; padding:0.375rem 0.875rem;
    background:var(--bg-topbar);
    border-bottom:1px solid var(--border-2);
    color:var(--text-1); position:relative; z-index:1; }
  /* Brand: red leftbar motif + car emoji + wordmark. The leftbar is a thin
     2px rule that visually anchors the wordmark like a terminal title. */
  .topbar .brand { display:flex; align-items:center; gap:0.5rem; font-size:0.875rem; font-weight:600; letter-spacing:.5px; }
  .topbar .brand::before { content:""; display:block; width:2px; height:1rem; background:var(--red); }
  .topbar .brand .car { font-size:1rem; filter:saturate(1.4); }
  .topbar .brand .name { color:var(--red); text-transform:uppercase; }
  .topbar .brand .chassis { color:var(--text-2); font-weight:500; }
  .topbar .config { display:flex; gap:0.875rem; align-items:center; color:var(--text-1); font-size:0.719rem; padding:0.1875rem 0.625rem; border-left:1px solid var(--border-1); cursor:pointer; transition:background .12s; }
  .topbar .config:hover { background:var(--bg-hover); }
  .topbar .config:empty { display:none; }
  .topbar .config .cfg { display:flex; align-items:baseline; gap:0.375rem; }
  .topbar .config .cfg-k { color:var(--text-3); text-transform:uppercase; font-size:0.625rem; letter-spacing:.6px; }
  .topbar .config .cfg-more { color:var(--text-3); font-size:0.625rem; }
  .topbar .age { color:var(--text-3); font-size:0.6875rem; margin-left:auto; font-variant-numeric:tabular-nums; }
  .page {
    flex:1 1 0;
    min-height:0;
    padding:0.5rem;
    /* Fill the full monitor width — no max-width cap. The page spans the whole
       viewport (minus the padding gutter); the right-hand schedule column is
       width-capped on its own, so the trigger graph absorbs the extra space on
       wide/ultrawide displays. */
    width:100%;
    box-sizing:border-box;
    display:grid;
    /* stats (auto) · main content (flex) */
    grid-template-rows: auto minmax(0,1fr);
    gap:0.375rem;
    overflow:hidden;
  }
  /* Two-column body: left holds triggers above agents/events, right holds schedule. */
  .main { display:grid; grid-template-columns: minmax(0,1fr) minmax(17.5rem,22.5rem); gap:0.375rem; min-height:0; overflow:hidden; }
  /* main-left is graph-over-blocks; weight 2.5:1 so the trigger graph gets
     ~70% of vertical room. Agents/events still scroll inside their block. */
  .main-left { display:grid; grid-template-rows: minmax(0,2.5fr) minmax(0,1fr); gap:0.375rem; min-height:0; overflow:hidden; }
  .main-right { display:grid; grid-template-rows: minmax(0,1fr); min-height:0; overflow:hidden; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(10rem,1fr)); gap:0.375rem; }
  /* Stat tiles: flat hard-rect, no sheen, no drop shadow. Bloomberg-terminal
     feel — the data is the foreground, the chrome is invisible. */
  .stat { background:var(--bg-card);
    border:1px solid var(--border-1); padding:0.3125rem 0.625rem;
    display:flex; flex-direction:column; gap:0.125rem; min-height:0; }
  .stat .label { color:var(--text-3); font-size:0.625rem; text-transform:uppercase; letter-spacing:.8px; }
  .stat .value { font-size:1.0625rem; font-weight:600; color:var(--text-1); line-height:1.1; font-variant-numeric:tabular-nums; }
  .stat .value.warn { color:var(--danger-soft); }
  .stat .value .sub { color:var(--text-3); font-size:0.656rem; font-weight:400; }
  .stat svg.gauge { display:block; width:100%; height:0.875rem; margin-top:auto; }
  /* Each block is a grid with a fixed h3 header and a 1fr body that scrolls
   * when content overflows its grid cell — the page itself never scrolls.
   * Grid is more predictable than flex here: in a fixed-height outer row
   * (1fr) the body fills the remainder; in an auto-sized outer row the body
   * sizes to its content without collapsing (which flex-basis:0 does).
   * Bloomberg-flat: hard 1px borders, no rounded corners, no shadows. */
  .block { background:var(--bg-card);
    border:1px solid var(--border-1); padding:0.375rem 0.75rem 0.625rem;
    min-width:0; min-height:0; display:grid; grid-template-rows:auto minmax(0,1fr); overflow:hidden; }
  .block-body { min-height:0; overflow:auto; }
  .blocks { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0.375rem; min-height:0; overflow:hidden; }
  @media (max-width: 900px) {
    /* Narrow viewports: surrender the no-scroll constraint — stack and let the page scroll. */
    html, body { overflow:auto; height:auto; }
    .page { overflow:visible; grid-template-rows:auto auto; }
    .main { grid-template-columns:1fr; overflow:visible; }
    .main-left, .main-right { grid-template-rows:auto; overflow:visible; }
    .block, .blocks > .block { overflow:visible; }
    .block-body { overflow:visible; }
    .blocks { grid-template-columns:1fr; }
  }
  /* Section header: brand-red square + uppercase wordmark + strong hard rule.
     Square mark (not dot) tightens the terminal feel. */
  .block h3 { margin:0 0 0.5rem; padding-bottom:0.3125rem;
    border-bottom:1px solid var(--border-2);
    font-size:0.6875rem; font-weight:700; color:var(--text-1);
    text-transform:uppercase; letter-spacing:1px;
    display:flex; align-items:center; gap:0.4375rem; }
  .block h3::before { content:""; width:0.375rem; height:0.375rem;
    background:var(--red); box-shadow:0 0 6px rgba(239,68,68,0.55); flex:none; }
  .dot { width:0.5rem; height:0.5rem; border-radius:50%; display:inline-block; flex:none; }
  .dot.up { background:var(--green); }
  .dot.down { background:var(--danger); }
  /* Running = brand red pulsing (the chassis is actively doing work, not in
     an error state — distinct from .down which uses danger). */
  .dot.running { background:var(--red); animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; box-shadow:0 0 0 0 var(--red-dim); } 50% { opacity:.55; box-shadow:0 0 0 3px transparent; } }
  .line { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding:0.0625rem 0; }
  .line.deny { color:var(--danger); }
  .ts { color:var(--text-3); display:inline-block; min-width:5.5em; }
  table { width:100%; border-collapse:collapse; font-size:0.75rem; }
  td { padding:0.125rem 0.625rem 0.125rem 0; vertical-align:top; }
  td.k { color:var(--text-2); white-space:nowrap; }
  .empty { color:var(--text-3); font-style:italic; font-size:0.719rem; padding:0.25rem 0; }
  /* Code wells: deep-recessed surface + 1px ring. Flat — no inner shadow. */
  code { background:var(--bg-input); padding:0.0625rem 0.3125rem; font-size:0.6875rem; color:var(--text-1);
    border:1px solid var(--border-1); }
  /* Pills: hard-rect tag, no rounded curve, no shadow. */
  .pill { display:inline-block; padding:0 0.375rem; font-size:0.625rem;
    background:var(--bg-sub); color:var(--text-2); border:1px solid var(--border-1);
    text-transform:uppercase; letter-spacing:.4px; }
  .pill.bad { color:var(--danger-soft); border-color:var(--danger-dim); }
  .clickable { cursor:pointer; }
  .clickable:hover { background:var(--bg-hover); }
  .error-page { padding:2.5rem; color:var(--danger); font-size:0.8125rem; }
  .modal { position:fixed; inset:0; z-index:50; display:flex; align-items:center; justify-content:center; }
  .modal[hidden] { display:none; }
  .modal-backdrop { position:absolute; inset:0; background:rgba(0,0,0,.78); }
  .modal-body { position:relative;
    background:var(--bg-card);
    border:1px solid var(--border-2);
    max-width:min(90vw,1100px); max-height:85vh; min-width:min(90vw,30rem);
    display:flex; flex-direction:column;
    box-shadow: 0 24px 48px rgba(0,0,0,0.55); }
  .modal-head { display:flex; align-items:center; justify-content:space-between; padding:0.625rem 0.875rem; border-bottom:1px solid var(--border-1); gap:0.75rem; }
  .modal-title { font-size:0.8125rem; font-weight:600; flex:1; color:var(--text-1); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .modal-close { background:none; border:none; color:var(--text-3); cursor:pointer; font-size:0.875rem; padding:0 0.25rem; }
  .modal-close:hover { color:var(--text-1); }
  .modal-content { padding:0.75rem 0.875rem; overflow:auto; flex:1; font-size:0.719rem; }
  .modal-content pre { white-space:pre-wrap; word-break:break-word; margin:0; font-family:inherit; color:var(--text-1); }
  .modal-content table { margin-top:0.25rem; }
  .modal-content td { padding:0.1875rem 0.625rem 0.1875rem 0; }
  .modal-content a.link { color:var(--text-1); cursor:pointer; text-decoration:underline; text-decoration-color:var(--text-3); }
  .modal-content a.link:hover { text-decoration-color:var(--red); }
  .modal-content .back { display:inline-block; margin-bottom:0.5rem; color:var(--text-3); cursor:pointer; }
  .modal-content .back:hover { color:var(--text-1); }
  .modal-content .kv { display:grid; grid-template-columns:max-content 1fr; gap:0.375rem 1.125rem; }
  .modal-content .kv dt { color:var(--text-3); text-transform:uppercase; font-size:0.625rem; letter-spacing:.6px; align-self:center; }
  .modal-content .kv dd { margin:0; color:var(--text-1); word-break:break-all; }
  .graph-wrap { overflow:hidden; padding:0; position:relative; }
  .graph { display:block; width:100%; height:100%; cursor:grab; touch-action:none; user-select:none; }
  .graph.panning { cursor:grabbing; }
  /* In the hero block, the graph fills the available vertical space above the legend. */
  .hero-graph .block-body { display:flex; flex-direction:column; gap:0.375rem; }
  .hero-graph .graph-wrap { flex:1 1 0; min-height:0; }
  .hero-graph .graph-legend { flex:0 0 auto; margin-top:0; }
  .graph .node rect { fill:var(--bg-sub); stroke:var(--border-2); transition:stroke .12s; }
  .graph .node.cron rect { stroke:var(--text-1); }
  .graph .node.orphan rect { stroke-dasharray:3 3; stroke:var(--border-2); }
  .graph .node.running rect { stroke:var(--red); animation:strokePulse 1.5s ease-in-out infinite; }
  @keyframes strokePulse { 0%,100% { stroke:var(--red); } 50% { stroke:var(--red-dim); } }
  .graph .node:hover rect { stroke:var(--text-1); }
  .graph .node.running:hover rect { stroke:var(--red); }
  .graph .node text.title { fill:var(--text-1); font:600 11.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; pointer-events:none; }
  .graph .node text.sub { fill:var(--text-3); font:10px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; pointer-events:none; }
  .graph .edge { fill:none; stroke-width:1.4; }
  .graph .edge.after { stroke:var(--text-3); }
  .graph .edge.on_success { stroke:var(--green); }
  .graph .edge.on_failure { stroke:var(--danger); }
  /* Halo via wide stroke matching the card bg, so the label is legible where
     it crosses the edge path without a manually sized backing rect. */
  .graph .edge-label { fill:var(--text-2); font:10px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; paint-order:stroke; stroke:var(--bg-card); stroke-width:4px; pointer-events:none; }
  /* Live-activity layer. Each packet is a circle that rides the edge via
     CSS offset-path. Polls replace innerHTML every 3s, so a fresh packet is
     re-emitted each tick with animation-delay set to -age, which fast-forwards
     it to the right point on its trajectory — animation stays continuous
     across re-renders. The packet color matches its edge kind. */
  .graph .packet { offset-distance:0%; opacity:0; pointer-events:none;
    animation:packetFlow 2.5s ease-out 1 forwards;
    animation-fill-mode:forwards; }
  .graph .packet.after      { fill:var(--text-1); }
  .graph .packet.on_success { fill:var(--green); }
  .graph .packet.on_failure { fill:var(--danger); }
  @keyframes packetFlow {
    0%   { offset-distance:0%;   opacity:0; }
    8%   { offset-distance:8%;   opacity:1; }
    90%  { offset-distance:100%; opacity:1; }
    100% { offset-distance:100%; opacity:0; }
  }
  /* Recency halo: an outer rect drawn before the main node rect, opacity set
     inline from "seconds since last_run_ts" so a node that just ran flashes
     green and fades back to invisible over a minute. Outside the main rect so
     it doesn't fight the .running stroke animation. */
  .graph .node .halo { fill:none; stroke:var(--green); stroke-width:2.5; pointer-events:none; }
  .graph-legend { display:flex; gap:0.875rem; margin-top:0.5rem; font-size:0.656rem; color:var(--text-3); flex-wrap:wrap; }
  .graph-legend .swatch { display:inline-block; width:0.875rem; height:2px; vertical-align:middle; margin-right:0.3125rem; }
  .graph-legend .swatch.dashed { border-top:1.5px dashed var(--text-3); height:0; }
  .agenda { display:flex; flex-direction:column; gap:1px; max-width:40rem; }
  .agenda-band { color:var(--text-3); font-size:0.625rem; text-transform:uppercase; letter-spacing:1px; padding:0.4375rem 0 0.125rem; margin-top:0.3125rem; border-top:1px solid var(--border-1); }
  .agenda-band:first-child { border-top:none; margin-top:0; padding-top:0.0625rem; }
  .agenda-row { display:flex; gap:0.75rem; padding:0.0625rem 0.25rem; align-items:baseline; font-size:0.75rem; line-height:1.35; border-radius:2px; }
  .agenda-row.alt { background:rgba(255,255,255,0.025); }
  .agenda-when { flex:0 0 5.5rem; color:var(--text-2); font-variant-numeric:tabular-nums; text-align:right; }
  .agenda-label { color:var(--text-1); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; }
  .ev-kind { display:inline-block; min-width:2.125rem; font-size:0.594rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.6px; margin-right:0.25rem; }
  .badges { position:fixed; bottom:0.5rem; right:0.625rem; display:flex; gap:0.375rem; z-index:40; pointer-events:none; }
  .badge { padding:0.0625rem 0.4375rem; font-size:0.625rem; font-weight:700; letter-spacing:1px; text-transform:uppercase; border:1px solid;
    background:var(--bg-card); }
  .badge.beta { color:var(--red); border-color:var(--red-dim); background:rgba(239,68,68,0.1); }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">
    <span class="car">🏎️</span>
    <span class="name">chassis</span>
    <span class="chassis" id="chassis-name"></span>
    <span class="pill" id="chassis-pill" hidden></span>
  </div>
  <div class="config" id="topconfig"></div>
  <div class="age" id="age">loading…</div>
</div>
<div class="page" id="page"></div>
<div id="modal" class="modal" hidden>
  <div class="modal-backdrop"></div>
  <div class="modal-body">
    <div class="modal-head"><span class="modal-title"></span><button class="modal-close" type="button">✕</button></div>
    <div class="modal-content"></div>
  </div>
</div>
<!-- @dagrejs/dagre — layered DAG layout for the trigger graph. Browser bundle
     exposes `dagre` globally; we use it for node positioning only and keep our
     own Bezier edge rendering on top. -->
<script src="https://cdn.jsdelivr.net/npm/@dagrejs/dagre@1/dist/dagre.min.js"></script>
<script>
function escape(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function parseNum(s){if(s==null)return 0;const m=String(s).match(/-?[\d.]+/);return m?parseFloat(m[0]):0;}
// Retro-LCD bar gauge: N small vertical segments. Lit segments interpolate
// linearly from green (left) to red (right) so the gauge reads as a health
// gradient — left cells safe, right cells hot. Color is sampled at each
// segment's center, so the gradient is quantised at cell boundaries.
const GAUGE_W = 120, GAUGE_H = 24, GAUGE_BAR_Y = 5, GAUGE_BAR_H = 14;
const GAUGE_SEGMENTS = 44;
const GAUGE_SEG_GAP  = 0.5;
const GAUGE_SEG_W    = (GAUGE_W - GAUGE_SEG_GAP * (GAUGE_SEGMENTS - 1)) / GAUGE_SEGMENTS;
const GAUGE_DIM      = "#262626";   // --border-1 — unlit segments
const GAUGE_LIT_LO   = [0x4a, 0xde, 0x80];  // --green (cool end)
const GAUGE_LIT_HI   = [0xef, 0x44, 0x44];  // --red (hot end)
function gaugeColor(t){
  // t = position along the bar in [0,1]. Linear RGB lerp from green to red.
  const f = Math.max(0, Math.min(t, 1));
  const r = GAUGE_LIT_LO[0] + (GAUGE_LIT_HI[0] - GAUGE_LIT_LO[0]) * f;
  const g = GAUGE_LIT_LO[1] + (GAUGE_LIT_HI[1] - GAUGE_LIT_LO[1]) * f;
  const b = GAUGE_LIT_LO[2] + (GAUGE_LIT_HI[2] - GAUGE_LIT_LO[2]) * f;
  const hex = ((Math.round(r)<<16) | (Math.round(g)<<8) | Math.round(b)).toString(16).padStart(6,"0");
  return `#${hex}`;
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

// Tool-calls gauge max is a heuristic ("busy" threshold); CPU/mem gauges
// scale dynamically against the container's actual ceiling — see renderStats.
const GAUGE_MAX_TOOLS = 50;

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

function renderTopBar(c){
  document.getElementById("chassis-name").textContent = c ? "· " + c.name : "";
  const pill = document.getElementById("chassis-pill");
  if (c) {
    let txt = c.status_line || c.status || "unknown";
    if (c.restart_count) txt += ` · ${c.restart_count} restart${c.restart_count === 1 ? "" : "s"}`;
    pill.textContent = txt;
    pill.hidden = false;
  } else { pill.hidden = true; }
  // Show 1-2 inline config items in the topbar so the most-asked-about runtime
  // facts (model, thinking level) are visible without a click; clicking the
  // strip opens the full Configuration modal.
  const cfg = (c && c.config) || {};
  lastConfig = cfg;
  const inline = [];
  if (cfg.model) inline.push(['model', cfg.model]);
  if (cfg.thinking) inline.push(['thinking', cfg.thinking]);
  const items = inline.map(([k, v]) => `<div class="cfg"><span class="cfg-k">${k}</span><span>${escape(v)}</span></div>`);
  const hasMore = Object.keys(cfg).length > inline.length;
  if (items.length || hasMore){
    items.push(`<span class="cfg-more">cfg ▸</span>`);
  }
  document.getElementById("topconfig").innerHTML = items.join("");
}

function renderStats(c){
  const stats = c.stats || {};
  const limits = c.limits || {};
  const cpu = parseNum(stats.cpu);
  const mem = parseNum(stats.mem_pct);
  // docker stats CPUPerc is "cores × 100"; gauge max scales to the container's
  // CPU ceiling. mem_pct is already 0–100 against the container's mem ceiling.
  const cores = limits.cpu_cores || 0;
  const cpuMax = cores > 0 ? cores * 100 : 100;
  const hourAgo = Date.now() - 3600*1000;
  const audit = c.audit || [];
  const toolsHour = audit.filter(e => { const t = Date.parse(e.ts); return t && t >= hourAgo; }).length;
  const denyHour = audit.filter(e => {
    const t = Date.parse(e.ts);
    if (!(t && t >= hourAgo)) return false;
    return e.status === "denied" || (e.exit && e.exit !== 0);
  }).length;
  const denyClass = denyHour > 0 ? "warn" : "";
  return `
    <div class="stats">
      <div class="stat">
        <div class="label">cpu</div>
        <div class="value">${cpu.toFixed(1)}<span class="sub"> %</span></div>
        ${gauge(cpu, cpuMax)}
      </div>
      <div class="stat">
        <div class="label">memory</div>
        <div class="value">${mem.toFixed(1)}<span class="sub"> %</span></div>
        ${gauge(mem, 100)}
      </div>
      <div class="stat">
        <div class="label">tool calls (1h)</div>
        <div class="value">${toolsHour}</div>
        ${gauge(toolsHour, GAUGE_MAX_TOOLS)}
      </div>
      <div class="stat">
        <div class="label">denies (1h)</div>
        <div class="value ${denyClass}">${denyHour}</div>
        ${gauge(denyHour, GAUGE_MAX_TOOLS)}
      </div>
    </div>`;
}

function renderAgents(chassis, agents){
  if (!agents || !agents.length) return '<div class="empty">no agents</div>';
  return '<table>' + agents.map(a => {
    const r = a.last_run;
    const when = r ? escape(relTime(new Date(r.mtime*1000).toISOString())) : '<span class="empty">never</span>';
    const task = r ? (r.task ? `<code>${escape(r.task)}</code>` : '<span class="empty">interactive</span>') : '';
    const live = a.running ? '<span class="dot running" style="margin-right:6px;vertical-align:middle"></span>' : '';
    return `<tr class="clickable" data-chassis="${escape(chassis)}" data-agent="${escape(a.name)}"><td class="k">${live}${escape(a.name)}</td><td>${task}</td><td>${when}</td></tr>`;
  }).join("") + '</table>';
}

// Lay tasks out in columns by "depth from a cron source," then reorder within
// each column to minimize edge crossings (barycenter heuristic). Cron-driven
// nodes are pinned at depth 0 so a `cron + on_failure` task doesn't drift
// right across cycle iterations. Pure cycles (no cron anchor) settle at
// adjacent columns thanks to the small depth cap.
// Node geometry — shared with the renderer. Sized to fit the longest
// "natural" sub-line ("↳ on_success reviewer/critique") at the .sub font.
const NODE_W = 200, NODE_H = 44;
// Padding around the dagre-computed graph rect so the right-side back-edge
// arcs and the top/bottom fan-out curves don't get clipped by the viewBox.
const PAD_L = 4, PAD_T = 50, PAD_R = 170, PAD_B = 50;

function layoutTasks(tasks){
  const byKey = new Map();
  tasks.forEach(t => byKey.set(`${t.agent}/${t.task}`, {...t, key:`${t.agent}/${t.task}`}));

  // Layered LR layout via dagre. acyclicer:'greedy' reverses a minimal feedback
  // edge set to break cycles, lays out as a DAG, then leaves the reversed
  // edges in place — coordinate-wise our edge renderer detects them as
  // backward by dst.x <= src.x and draws them as right-side loops.
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: "LR",
    nodesep: 24,    // vertical gap between nodes in the same rank
    ranksep: 110,   // horizontal gap between ranks
    marginx: PAD_L,
    marginy: PAD_T,
    acyclicer: "greedy",
    ranker: "network-simplex",
  });
  g.setDefaultEdgeLabel(() => ({}));

  for (const t of byKey.values()){
    g.setNode(t.key, { width: NODE_W, height: NODE_H });
  }
  for (const t of byKey.values()){
    if (!t.trigger) continue;
    const sk = `${t.trigger.agent}/${t.trigger.task}`;
    if (!byKey.has(sk)) continue;
    if (sk === t.key) continue;  // self-loops blow up dagre's ranker
    g.setEdge(sk, t.key, { kind: t.trigger.kind, when: t.trigger.when });
  }

  dagre.layout(g);

  // dagre reports node center as (x, y); our renderer wants top-left. Translate
  // here so the rest of the pipeline (rect at 0,0 inside the <g transform>) is
  // unchanged.
  const positioned = new Map();
  for (const t of byKey.values()){
    const n = g.node(t.key);
    positioned.set(t.key, { ...t, x: n.x - NODE_W / 2, y: n.y - NODE_H / 2 });
  }
  // Pull each edge's routing polyline out of dagre — its first/last points sit
  // on the node boundary and intermediate points route around obstacles, so we
  // can draw the path directly without our own collision logic.
  const edges = [];
  for (const e of g.edges()){
    const ed = g.edge(e);
    edges.push({ src: e.v, dst: e.w, kind: ed.kind, when: ed.when, points: ed.points });
  }
  const gg = g.graph();
  return {
    nodes: positioned, edges, NODE_W, NODE_H,
    width:  gg.width  + PAD_R,
    height: gg.height + PAD_B,
  };
}

// Arrow-marker fill colors are pulled from the same palette as the .edge
// stroke rules so the marker and line stay in sync per kind.
const EDGE_COLORS = { after: "#808080", on_success: "#4ade80", on_failure: "#f59e0b" };

// Char budgets chosen so the longest "natural" labels (e.g. "⏱ */5 * * * * ·
// next 12m ago", "↳ on_success reviewer/critique") fit inside the node at the
// configured node width. Past these limits we ellipsize and stash the full
// text in a <title> for native hover-tooltip.
const TITLE_MAX_CHARS = 26;
const SUB_MAX_CHARS   = 30;
function truncate(s, n){ return s.length > n ? s.slice(0, n - 1) + "…" : s; }

// Recency-glow window. Picks the timeframe over which a node still shows its
// "just ran" halo; opacity decays linearly across this window.
const HALO_WINDOW_S = 60;
// Packet animation duration. Must match the keyframes' total length in CSS,
// since we use it to decide whether a fire is too old to bother rendering.
const PACKET_DUR_S = 2.5;

function renderTriggers(chassis, tasks, fires){
  if (!tasks || !tasks.length) return '<div class="empty">no tasks found</div>';
  const L = layoutTasks(tasks);
  const NW = L.NODE_W, NH = L.NODE_H;
  // Single clock reading for the whole render — keeps every age computation
  // (packet, halo) on the same reference instant for this tick.
  const nowMs = Date.now();

  // Build a smooth SVG path through dagre's routing polyline. dagre's points
  // sit on the node boundary at the endpoints and at chosen waypoints in
  // between (chosen to route around other nodes); we connect them with
  // quadratic Beziers through midpoints so each segment is C1-continuous at
  // the joints, then land on the final point with an L so the arrow marker
  // tracks the actual approach angle into the destination.
  const pathThrough = (pts) => {
    if (pts.length < 2) return "";
    if (pts.length === 2) return `M${pts[0].x},${pts[0].y} L${pts[1].x},${pts[1].y}`;
    let d = `M${pts[0].x},${pts[0].y}`;
    for (let i = 1; i < pts.length - 1; i++){
      const mx = (pts[i].x + pts[i+1].x) / 2;
      const my = (pts[i].y + pts[i+1].y) / 2;
      d += ` Q${pts[i].x},${pts[i].y} ${mx},${my}`;
    }
    const last = pts[pts.length - 1];
    d += ` L${last.x},${last.y}`;
    return d;
  };

  let edgesSvg = "";
  // Remember each edge's rendered path so a fire's packet can ride the exact
  // same routing the line draws. Keyed by "src|dst" to match how fires are
  // identified on the wire.
  const edgePaths = new Map();
  for (const e of L.edges){
    const kind = ["after","on_success","on_failure"].includes(e.kind) ? e.kind : "after";
    const d = pathThrough(e.points);
    edgePaths.set(`${e.src}|${e.dst}`, { d, kind });
    edgesSvg += `<path class="edge ${kind}" d="${d}" marker-end="url(#arrow-${kind})"/>`;
    if (e.when){
      // Anchor the label at the middle waypoint of the routing polyline; for
      // a 2-point edge fall back to the segment midpoint.
      const p = e.points;
      const mid = p.length >= 3 ? p[Math.floor(p.length / 2)] : { x:(p[0].x+p[1].x)/2, y:(p[0].y+p[1].y)/2 };
      const label = `${e.when.key} ${e.when.op} ${e.when.value}`;
      edgesSvg += `<text class="edge-label" x="${mid.x}" y="${mid.y}" text-anchor="middle" dominant-baseline="middle">${escape(label)}</text>`;
    }
  }

  // Animated packets for recently-fired triggers. A fire still inside the
  // animation window emits a circle with offset-path tracing its edge; the
  // -age animation-delay places it on the curve at the proportional point,
  // so the packet keeps progressing smoothly across the 3-second poll cycle.
  let packetsSvg = "";
  if (fires){
    for (const f of fires){
      const ts = Date.parse(f.ts);
      if (!ts) continue;
      const ageS = (nowMs - ts) / 1000;
      if (ageS < -1 || ageS > PACKET_DUR_S) continue;
      const ep = edgePaths.get(`${f.src_agent}/${f.src_task}|${f.dst_agent}/${f.dst_task}`);
      if (!ep) continue;
      // Clamp negative age (client clock ahead of server) to 0 so the packet
      // starts at the beginning instead of sliding past its endpoint.
      const delay = Math.max(0, ageS).toFixed(2);
      packetsSvg += `<circle class="packet ${ep.kind}" r="5" style="offset-path:path('${ep.d}'); animation-delay:-${delay}s;"/>`;
    }
  }
  let nodesSvg = "";
  for (const t of L.nodes.values()){
    const isCron   = !!t.cron;
    const isOrphan = !isCron && !t.trigger;
    const classes = ["node", "clickable"];
    if (isCron) classes.push("cron");
    else if (isOrphan) classes.push("orphan");
    if (t.running) classes.push("running");
    const titleRaw = `${t.agent}/${t.task}`;
    // Build the sub line from raw values (not HTML-escaped yet) so we can
    // truncate by character count without slicing into an entity reference.
    let subRaw;
    if (isCron){
      const nx = t.next ? ` · next ${relTime(t.next)}` : "";
      subRaw = `⏱ ${t.cron}${nx}`;
    } else if (t.trigger){
      // The when clause is rendered on the incoming edge instead — keeps the
      // node label short and the routing condition visible at the edge.
      subRaw = `↳ ${t.trigger.kind} ${t.trigger.agent}/${t.trigger.task}`;
    } else {
      subRaw = "manual";
    }
    const titleDisp = truncate(titleRaw, TITLE_MAX_CHARS);
    const subDisp   = truncate(subRaw,   SUB_MAX_CHARS);
    // Full text in the <title> so hover always reveals what was truncated;
    // also useful when the user wants to read a long cron expression.
    const tooltip = `${titleRaw}\n${subRaw}`;
    // Recency halo: linear opacity decay across HALO_WINDOW_S. last_run_ts is
    // the mtime of the latest run-log jsonl (server clock); we compare to
    // nowMs/1000 so we don't depend on a separate server-clock field.
    let haloSvg = "";
    if (t.last_run_ts){
      const ageS = (nowMs / 1000) - t.last_run_ts;
      if (ageS < HALO_WINDOW_S){
        const opacity = Math.max(0, 1 - ageS / HALO_WINDOW_S).toFixed(2);
        haloSvg = `<rect class="halo" x="-4" y="-4" width="${NW + 8}" height="${NH + 8}" rx="8" style="opacity:${opacity}"/>`;
      }
    }
    nodesSvg += `
      <g class="${classes.join(' ')}" data-chassis="${escape(chassis)}" data-agent="${escape(t.agent)}" data-task="${escape(t.task)}" transform="translate(${t.x},${t.y})">
        <title>${escape(tooltip)}</title>
        ${haloSvg}
        <rect width="${NW}" height="${NH}" rx="6"/>
        <text class="title" x="10" y="18">${escape(titleDisp)}</text>
        <text class="sub"   x="10" y="33">${escape(subDisp)}</text>
      </g>`;
  }
  const markers = ["after","on_success","on_failure"].map(k => `
    <marker id="arrow-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="${EDGE_COLORS[k]}"/>
    </marker>`).join("");
  return `
    <div class="graph-wrap">
      <svg class="graph" viewBox="0 0 ${L.width} ${L.height}" data-nat-w="${L.width}" data-nat-h="${L.height}" preserveAspectRatio="xMidYMid meet">
        <defs>${markers}</defs>
        ${edgesSvg}
        ${packetsSvg}
        ${nodesSvg}
      </svg>
    </div>
    <div class="graph-legend">
      <span><span class="swatch" style="background:${EDGE_COLORS.on_success}"></span>on_success</span>
      <span><span class="swatch" style="background:${EDGE_COLORS.after}"></span>after</span>
      <span><span class="swatch" style="background:${EDGE_COLORS.on_failure}"></span>on_failure</span>
      <span><span class="swatch" style="background:var(--text-1)"></span>cron-rooted</span>
      <span><span class="swatch dashed"></span>manual (no cron, no trigger)</span>
    </div>`;
}

// Vertical 24h agenda of cron fires across all jobs, in chronological order.
// Band headers (now / +6h / +12h / +18h / +24h) render lazily as the timeline
// crosses each boundary, so empty bands don't take up space.
function fmtUntil(iso){
  const t = Date.parse(iso); if (!t) return iso;
  const diff = Math.max(0, Math.floor((t - Date.now()) / 1000));
  if (diff < 60) return `in ${diff}s`;
  const m = Math.floor(diff / 60);
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60), rm = m % 60;
  return rm ? `in ${h}h ${rm}m` : `in ${h}h`;
}

function renderSchedule(jobs){
  if (!jobs || !jobs.length) return '<div class="empty">no cron jobs</div>';
  const WINDOW_MS = 24 * 3600 * 1000;
  const now = Date.now();
  const events = [];
  for (const j of jobs){
    const fires = (j.next_fires && j.next_fires.length) ? j.next_fires : (j.next ? [j.next] : []);
    for (const iso of fires){
      const t = Date.parse(iso);
      if (!t || t < now || t - now > WINDOW_MS) continue;
      events.push({ t, iso, label: j.command || j.schedule, schedule: j.schedule });
    }
  }
  if (!events.length) return '<div class="empty">no fires in the next 24h</div>';
  events.sort((a, b) => a.t - b.t);
  const BANDS = [
    { ms: 0,            label: 'now'  },
    { ms: 6  * 3600000, label: '+6h'  },
    { ms: 12 * 3600000, label: '+12h' },
    { ms: 18 * 3600000, label: '+18h' },
  ];
  let html = '<div class="agenda">';
  let bandIdx = 0;
  let rowIdx = 0;
  html += `<div class="agenda-band">${BANDS[0].label}</div>`;
  bandIdx = 1;
  for (const ev of events){
    const elapsed = ev.t - now;
    while (bandIdx < BANDS.length && elapsed >= BANDS[bandIdx].ms){
      html += `<div class="agenda-band">${BANDS[bandIdx].label}</div>`;
      bandIdx++;
    }
    const alt = rowIdx++ % 2 ? ' alt' : '';
    html += `
      <div class="agenda-row${alt}">
        <span class="agenda-when">${escape(fmtUntil(ev.iso))}</span>
        <span class="agenda-label" title="${escape(ev.schedule)}">${escape(ev.label)}</span>
      </div>`;
  }
  html += `<div class="agenda-band">+24h</div>`;
  html += '</div>';
  return html;
}

// Merged audit + fires, sorted newest-first. Each row is tagged with a
// "tool" or "fire" badge so the source is unambiguous. Tool rows are
// clickable and open the audit-entry JSON modal; fire rows are static.
function renderEvents(audit, fires){
  audit = audit || [];
  fires = fires || [];
  const events = [];
  for (const e of audit){
    events.push({ ts: e.ts, kind: 'tool', bad: e.status === "denied" || (e.exit && e.exit !== 0), data: e });
  }
  for (const f of fires){
    events.push({ ts: f.ts, kind: 'fire', bad: f.rc !== 0, data: f });
  }
  if (!events.length) return '<div class="empty">no events</div>';
  events.sort((a, b) => (Date.parse(b.ts) || 0) - (Date.parse(a.ts) || 0));
  return events.slice(0, 40).map(ev => {
    const when = `<span class="ts">${escape(relTime(ev.ts))}</span>`;
    const kind = `<span class="ev-kind">${ev.kind}</span>`;
    if (ev.kind === 'tool'){
      const e = ev.data;
      const note = ev.bad ? ` ✗ ${escape(e.reason || "exit " + e.exit)}` : "";
      return `<div class="line clickable ${ev.bad ? "deny" : ""}" data-audit="${escape(JSON.stringify(e))}">${when} ${kind}${escape(e.tool)}${note}</div>`;
    }
    const f = ev.data;
    const arrow = `<span style="color:${EDGE_COLORS[f.kind] || EDGE_COLORS.after}">→</span>`;
    // The verdict snapshot answers "why did this branch fire?" — surface it
    // inline so the operator doesn't have to grep triggers.log. The `when`
    // clause (if any) appears on the right of the arrow next to its dst, so
    // the routing rule reads left-to-right with the destination.
    const verdict = f.verdict && Object.keys(f.verdict).length
      ? ` <span class="pill" style="color:var(--text-3)">${escape(Object.entries(f.verdict).map(([k,v]) => `${k}=${v}`).join(','))}</span>`
      : "";
    const wTail = f.when
      ? ` <span class="pill" style="color:var(--text-3)">when ${escape(f.when.key)} ${escape(f.when.op)} ${escape(f.when.value)}</span>`
      : "";
    return `<div class="line ${ev.bad ? 'deny' : ''}">${when} ${kind}${escape(f.src_agent)}/${escape(f.src_task)} <span class="pill${ev.bad ? ' bad' : ''}">${escape(f.kind)}${ev.bad ? ' rc=' + f.rc : ''}</span>${verdict} ${arrow} ${escape(f.dst_agent)}/${escape(f.dst_task)}${wTail}</div>`;
  }).join("");
}

function renderPage(c){
  if (!c.running){
    return `${renderStats(c)}<div class="block"><div class="block-body"><div class="empty">chassis is not running</div></div></div>`;
  }
  // Every block wraps its content in .block-body so that's what scrolls
  // when the cell can't grow further (page itself never scrolls on wide).
  return `
    ${renderStats(c)}
    <div class="main">
      <div class="main-left">
        <div class="block hero-graph"><h3>triggers</h3><div class="block-body">${renderTriggers(c.name, c.tasks, c.fires)}</div></div>
        <div class="blocks">
          <div class="block"><h3>agents</h3><div class="block-body">${renderAgents(c.name, c.agents)}</div></div>
          <div class="block"><h3>events</h3><div class="block-body">${renderEvents(c.audit, c.fires)}</div></div>
        </div>
      </div>
      <div class="main-right">
        <div class="block"><h3>schedule</h3><div class="block-body">${renderSchedule(c.cron)}</div></div>
      </div>
    </div>`;
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
// Both drill-ins (showAgent: all tasks for an agent; showTask: a single task)
// share the same row format. Differences: URL (?task= filter), modal title,
// and the "task" column is dropped when the modal is already scoped to one.
async function renderRunsModal(chassis, title, url, includeTaskCol){
  openModal(title, '<div class="empty">loading…</div>');
  try{
    const r = await fetch(url);
    const runs = await r.json();
    if(!runs.length){
      document.querySelector('.modal-content').innerHTML='<div class="empty">no runs recorded</div>';
      return;
    }
    const rows = runs.map(rec => {
      const when = escape(relTime(new Date(rec.mtime*1000).toISOString()));
      const task = rec.task ? `<code>${escape(rec.task)}</code>` : '<span class="empty">interactive</span>';
      const sess = rec.session_file ? `<a class="link" data-session="${escape(rec.session_file)}" data-chassis="${escape(chassis)}">session</a>` : '<span class="empty">—</span>';
      const log  = `<a class="link" data-file="${escape(rec.path)}" data-chassis="${escape(chassis)}">run log</a>`;
      return includeTaskCol
        ? `<tr><td class="k">${when}</td><td>${task}</td><td>${log}</td><td>${sess}</td></tr>`
        : `<tr><td class="k">${when}</td><td>${log}</td><td>${sess}</td></tr>`;
    }).join("");
    const head = includeTaskCol
      ? `<thead><tr><td class="k">when</td><td class="k">task</td><td class="k">run log</td><td class="k">pi session</td></tr></thead>`
      : `<thead><tr><td class="k">when</td><td class="k">run log</td><td class="k">pi session</td></tr></thead>`;
    document.querySelector('.modal-content').innerHTML = `<table>${head}${rows}</table>`;
  }catch(err){
    document.querySelector('.modal-content').innerHTML = `<div class="empty">fetch error: ${escape(err.message)}</div>`;
  }
}
async function showAgent(chassis,agent){
  await renderRunsModal(
    chassis,
    `${chassis} · ${agent}`,
    `/api/runs/${encodeURIComponent(chassis)}/${encodeURIComponent(agent)}?limit=20`,
    true,
  );
}
async function showTask(chassis,agent,task){
  await renderRunsModal(
    chassis,
    `${chassis} · ${agent}/${task}`,
    `/api/runs/${encodeURIComponent(chassis)}/${encodeURIComponent(agent)}?task=${encodeURIComponent(task)}&limit=20`,
    false,
  );
}
// The configuration object rides on the snapshot, so this is a no-fetch modal.
function showConfig(){
  if (!lastConfig || !Object.keys(lastConfig).length){
    openModal('configuration', '<div class="empty">no configuration available</div>');
    return;
  }
  const LABELS = {
    model: "model",
    context_window: "context window",
    max_tokens: "max tokens",
    thinking: "thinking",
    api_kind: "api kind",
    base_url: "base url",
    harness: "harness",
  };
  const rows = Object.entries(LABELS)
    .filter(([k]) => lastConfig[k])
    .map(([k, label]) => `<dt>${escape(label)}</dt><dd>${escape(lastConfig[k])}</dd>`)
    .join("");
  openModal('configuration', `<dl class="kv">${rows}</dl>`);
}
async function showFile(chassis,path,title){
  const backHTML=`<a class="back" id="modal-back">← back</a>`;
  openModal(title,backHTML+'<div class="empty">loading…</div>');
  try{
    const r=await fetch(`/api/file/${encodeURIComponent(chassis)}?path=${encodeURIComponent(path)}`);
    const j=await r.json();
    if(j.error){ document.querySelector('.modal-content').innerHTML=backHTML+`<div class="empty">${escape(j.error)}</div>`; return; }
    document.querySelector('.modal-content').innerHTML=backHTML+`<div style="color:var(--text-3);margin-bottom:6px">${escape(path)}</div><pre>${escape(j.content||"(empty)")}</pre>`;
  }catch(err){
    document.querySelector('.modal-content').innerHTML=backHTML+`<div class="empty">fetch error: ${escape(err.message)}</div>`;
  }
}
function reopenLastDrill(){
  if(!lastDrillCtx) return;
  if(lastDrillCtx.task) showTask(lastDrillCtx.chassis, lastDrillCtx.agent, lastDrillCtx.task);
  else showAgent(lastDrillCtx.chassis, lastDrillCtx.agent);
}
document.addEventListener('click',e=>{
  // Pan handler sets this after a meaningful drag so the synthetic click that
  // follows mouseup doesn't open a drill-in modal for the node we ended on.
  if(suppressNextClick){ suppressNextClick=false; return; }
  if(e.target.matches('.modal-backdrop, .modal-close')){ closeModal(); return; }
  if(e.target.id==='modal-back' || e.target.classList.contains('back')){
    reopenLastDrill();
    return;
  }
  if(e.target.closest('#topconfig')){ showConfig(); return; }
  const sess=e.target.closest('[data-session]');
  if(sess){ showFile(sess.dataset.chassis,sess.dataset.session,'pi session'); return; }
  const file=e.target.closest('[data-file]');
  if(file){ showFile(file.dataset.chassis,file.dataset.file,'run log'); return; }
  const audit=e.target.closest('[data-audit]');
  if(audit){ try{ showAudit(JSON.parse(audit.dataset.audit)); }catch(_){} return; }
  // A graph node has both data-agent and data-task; an agent row has only
  // data-agent. Same handler, two scopes — task drill wins when present.
  const target=e.target.closest('[data-agent]');
  if(target){
    const t = target.dataset.task || null;
    lastDrillCtx = { chassis: target.dataset.chassis, agent: target.dataset.agent, task: t };
    if(t) showTask(lastDrillCtx.chassis, lastDrillCtx.agent, t);
    else  showAgent(lastDrillCtx.chassis, lastDrillCtx.agent);
  }
});
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });
let lastDrillCtx=null;
let lastConfig=null;
let serverBoot=null;
let suppressNextClick=false;
// Persisted SVG viewBox state. Reset to natural bounds on each render until
// the user pans or zooms — after that, we preserve their view across the
// 3-second auto-refresh so the dashboard doesn't snap back on every tick.
let graphVB=null;
let graphInteracted=false;

function attachGraphPanZoom(){
  const svg = document.querySelector('.hero-graph .graph');
  if(!svg) return;
  const natW = parseFloat(svg.getAttribute('data-nat-w'));
  const natH = parseFloat(svg.getAttribute('data-nat-h'));
  if(!Number.isFinite(natW) || !Number.isFinite(natH)) return;
  if(!graphInteracted || !graphVB){
    graphVB = { x: 0, y: 0, w: natW, h: natH };
  }
  const applyVB = () => svg.setAttribute('viewBox',
    `${graphVB.x} ${graphVB.y} ${graphVB.w} ${graphVB.h}`);
  applyVB();

  let panning = false;
  let panStart = null;
  svg.addEventListener('mousedown', (e) => {
    if(e.button !== 0) return;
    // Clicking a node should still drill in; only pan from empty space.
    if(e.target.closest('.node')) return;
    e.preventDefault();
    panning = true;
    const rect = svg.getBoundingClientRect();
    panStart = {
      clientX: e.clientX, clientY: e.clientY,
      vbX: graphVB.x, vbY: graphVB.y,
      // CSS px → viewBox unit conversion; matches `xMidYMid meet` so the
      // factor is the smaller of the two axis ratios when aspect differs.
      scale: Math.max(graphVB.w / rect.width, graphVB.h / rect.height),
      moved: 0,
    };
    svg.classList.add('panning');
  });
  document.addEventListener('mousemove', (e) => {
    if(!panning) return;
    const dx = e.clientX - panStart.clientX;
    const dy = e.clientY - panStart.clientY;
    panStart.moved += Math.abs(dx) + Math.abs(dy);
    graphVB.x = panStart.vbX - dx * panStart.scale;
    graphVB.y = panStart.vbY - dy * panStart.scale;
    if(panStart.moved > 3) graphInteracted = true;
    applyVB();
  });
  document.addEventListener('mouseup', () => {
    if(!panning) return;
    panning = false;
    svg.classList.remove('panning');
    if(panStart && panStart.moved > 3) suppressNextClick = true;
    panStart = null;
  });

  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    // Pin the point under the cursor: convert mouse px → viewBox coords,
    // scale the viewBox, then shift x/y so the same vb point still maps to
    // the same cursor px.
    const vbMx = graphVB.x + (mx / rect.width)  * graphVB.w;
    const vbMy = graphVB.y + (my / rect.height) * graphVB.h;
    // Magnitude-aware zoom: a small trackpad nudge (deltaY≈4) gives a 0.6%
    // step; a full mouse-wheel detent (deltaY≈100) gives ~16%. Previous
    // implementation used only the sign and treated both the same, which
    // made trackpads feel runaway-twitchy. Sensitivity tuned to match the
    // old 1.15× per detent at deltaY=100. Clamped so a single fast scroll
    // event (some browsers emit deltaY>500) can't slam past the zoom range.
    const k = 0.0014;
    const factor = Math.exp(Math.max(-0.5, Math.min(0.5, e.deltaY * k)));
    const newW = graphVB.w * factor;
    // Clamp: 0.15× natural (zoomed in tight) to 6× natural (zoomed way out).
    if(newW < natW * 0.15 || newW > natW * 6) return;
    graphVB.w = newW;
    graphVB.h *= factor;
    graphVB.x = vbMx - (mx / rect.width)  * graphVB.w;
    graphVB.y = vbMy - (my / rect.height) * graphVB.h;
    graphInteracted = true;
    applyVB();
  }, { passive: false });

  svg.addEventListener('dblclick', (e) => {
    if(e.target.closest('.node')) return;
    graphVB = { x: 0, y: 0, w: natW, h: natH };
    graphInteracted = false;
    applyVB();
  });
}

async function tick(){
  try{
    const r=await fetch("/api/state");
    const data=await r.json();
    // Auto-refresh on server restart (uvicorn --reload). The boot timestamp
    // changes on each subprocess restart, so a mismatch means the embedded
    // HTML/JS is stale and we should pick up the new copy.
    if(data.server_boot){
      if(serverBoot===null) serverBoot=data.server_boot;
      else if(serverBoot!==data.server_boot){ location.reload(); return; }
    }
    const page=document.getElementById("page");
    if(data.error || !data.chassis){
      renderTopBar(null);
      page.innerHTML=`<div class="error-page">${escape(data.error || "no chassis")}</div>`;
    }else{
      renderTopBar(data.chassis);
      document.title = `chassis · ${data.chassis.name}`;
      page.innerHTML = renderPage(data.chassis);
      attachGraphPanZoom();
    }
    document.getElementById("age").textContent="updated "+relTime(data.generated_at);
  }catch(e){
    document.getElementById("age").textContent="fetch error: "+e.message;
  }
}
tick();
setInterval(tick,3000);
</script>
<div class="badges">
  <span class="badge beta">beta</span>
</div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


def main() -> None:
    global CHASSIS_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--chassis", default=None, help="Container name to monitor. Defaults to the only chassis container running.")
    ap.add_argument("--reload", action="store_true", help="Dev mode: restart the server when app.py changes; the browser auto-refreshes on the next poll.")
    args = ap.parse_args()
    CHASSIS_NAME = args.chassis
    if args.chassis:
        # Reload subprocess re-imports the module instead of running main(),
        # so propagate the chosen chassis through the environment.
        os.environ["CHASSIS_NAME"] = args.chassis
    import uvicorn
    if args.reload:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # uvicorn's reloader needs an import string and a Python path that
        # can resolve it. The reload subprocess inherits this env var.
        os.environ["PYTHONPATH"] = (
            script_dir + os.pathsep + os.environ.get("PYTHONPATH", "")
        ).rstrip(os.pathsep)
        uvicorn.run(
            "app:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[script_dir],
            log_level="warning",
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
