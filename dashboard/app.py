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
import re
import shlex
from datetime import datetime, timezone

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

# Override target; set from --chassis. None = auto-pick when exactly one
# chassis container is running.
CHASSIS_NAME: str | None = None

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
        cmd = re.sub(r"^/usr/local/bin/run-agent\s+", "", cmd)
        next_fire = None
        if croniter:
            try:
                next_fire = croniter(expr, now).get_next(datetime).replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        jobs.append({"schedule": expr, "command": cmd, "next": next_fire})
    return jobs


async def chassis_snapshot(meta: dict) -> dict:
    name = meta["name"]
    inspect, stats = await asyncio.gather(
        docker_inspect(name),
        docker_stats(name),
    )
    snap: dict = {
        "name": name,
        "project": meta["project"],
        "status_line": meta["status_line"],
        "stats": stats,
        **inspect,
    }
    if not inspect.get("running"):
        return snap
    audit, agents, jobs = await asyncio.gather(
        tail_audit(name, 50),
        list_agents(name),
        cron_jobs(name),
    )
    agent_runs = await asyncio.gather(*(agent_last_run(name, a) for a in agents))
    snap["agents"] = [{"name": a, "last_run": r} for a, r in zip(agents, agent_runs)]
    snap["audit"] = audit
    snap["cron"] = jobs
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
            "error": err,
            "chassis": None,
        })
    snap = await chassis_snapshot(meta)
    return JSONResponse({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chassis": snap,
    })


@app.get("/api/runs/{container}/{agent}")
async def api_runs(container: str, agent: str, limit: int = 20):
    """Most-recent run records for one agent in one chassis. Each entry is the
    first JSON line of a run log under /var/log/chassis/agents/<agent>/."""
    if not (_safe_name(container) and _safe_name(agent)):
        return JSONResponse({"error": "invalid name"}, status_code=400)
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
  .topbar .brand .chassis { color:#8b949e; font-weight:500; }
  .topbar .config { display:flex; gap:14px; align-items:center; color:#c9d1d9; font-size:11.5px; padding-left:8px; border-left:1px solid #21262d; }
  .topbar .config:empty { display:none; }
  .topbar .config .cfg { display:flex; align-items:baseline; gap:6px; }
  .topbar .config .cfg-k { color:#6e7681; text-transform:uppercase; font-size:10px; letter-spacing:.6px; }
  .topbar .age { color:#6e7681; font-size:11px; margin-left:auto; }
  .page { padding:14px; max-width:1400px; margin:0 auto; box-sizing:border-box; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-bottom:14px; }
  .stat { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px 12px; display:flex; flex-direction:column; gap:4px; min-height:80px; }
  .stat .label { color:#6e7681; font-size:10px; text-transform:uppercase; letter-spacing:.6px; }
  .stat .value { font-size:20px; font-weight:600; color:#c9d1d9; line-height:1.15; }
  .stat .value .sub { color:#6e7681; font-size:11px; font-weight:400; }
  .stat svg.gauge { display:block; width:100%; height:24px; margin-top:auto; }
  .blocks { display:grid; grid-template-columns:1fr; gap:12px; }
  .block { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:12px 14px; }
  .block h3 { margin:0 0 8px; font-size:12px; font-weight:600; color:#6e7681; text-transform:uppercase; letter-spacing:.6px; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; flex:none; }
  .dot.up { background:#3fb950; }
  .dot.down { background:#f85149; }
  .line { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding:1px 0; }
  .line.deny { color:#f85149; }
  .ts { color:#6e7681; display:inline-block; min-width:5.5em; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  td { padding:2px 10px 2px 0; vertical-align:top; }
  td.k { color:#8b949e; white-space:nowrap; }
  .empty { color:#6e7681; font-style:italic; font-size:11.5px; padding:4px 0; }
  code { background:#21262d; padding:1px 5px; border-radius:3px; font-size:11px; }
  .pill { display:inline-block; padding:1px 6px; border-radius:10px; font-size:10px; background:#21262d; color:#8b949e; }
  .clickable { cursor:pointer; }
  .clickable:hover { background:#1f2630; }
  .error-page { padding:40px; color:#f85149; font-size:13px; }
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

// Gauge scales: chosen so a typical-load chassis sits ~40-60%, leaving room
// for the gradient to communicate "headroom" vs "approaching limit."
const GAUGE_MAX_CPU   = 400;   // 4 fully-saturated cores
const GAUGE_MAX_MEM   = 100;   // % of host
const GAUGE_MAX_TOOLS = 50;    // calls/hr is "busy"

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
  if (c) { pill.textContent = c.status || "unknown"; pill.hidden = false; }
  else { pill.hidden = true; }
  const CFG_FIELDS = ["harness", "model", "source"];
  const cfgItems = c ? CFG_FIELDS
    .filter(k => c[k])
    .map(k => `<div class="cfg"><span class="cfg-k">${k}</span><span>${escape(c[k])}</span></div>`) : [];
  document.getElementById("topconfig").innerHTML = cfgItems.join("");
}

function renderStats(c){
  const stats = c.stats || {};
  const cpu = parseNum(stats.cpu);
  const mem = parseNum(stats.mem_pct);
  const hourAgo = Date.now() - 3600*1000;
  const audit = c.audit || [];
  const toolsHour = audit.filter(e => { const t = Date.parse(e.ts); return t && t >= hourAgo; }).length;
  const denyHour = audit.filter(e => {
    const t = Date.parse(e.ts);
    if (!(t && t >= hourAgo)) return false;
    return e.status === "denied" || (e.exit && e.exit !== 0);
  }).length;
  return `
    <div class="stats">
      <div class="stat">
        <div class="label">cpu</div>
        <div class="value">${cpu.toFixed(1)}<span class="sub"> %</span></div>
        ${gauge(cpu, GAUGE_MAX_CPU)}
      </div>
      <div class="stat">
        <div class="label">memory</div>
        <div class="value">${mem.toFixed(1)}<span class="sub"> % host</span></div>
        ${gauge(mem, GAUGE_MAX_MEM)}
      </div>
      <div class="stat">
        <div class="label">tool calls (1h)</div>
        <div class="value">${toolsHour}${denyHour ? ` <span class="sub">· ${denyHour} bad</span>` : ""}</div>
        ${gauge(toolsHour, GAUGE_MAX_TOOLS)}
      </div>
      <div class="stat">
        <div class="label">uptime</div>
        <div class="value">${escape(c.status_line || "")}</div>
        <div class="sub" style="color:#6e7681;font-size:10.5px">restarts ${c.restart_count ?? 0}</div>
      </div>
    </div>`;
}

function renderAgents(chassis, agents){
  if (!agents || !agents.length) return '<div class="empty">no agents</div>';
  return '<table>' + agents.map(a => {
    const r = a.last_run;
    const when = r ? escape(relTime(new Date(r.mtime*1000).toISOString())) : '<span class="empty">never</span>';
    const task = r ? (r.task ? `<code>${escape(r.task)}</code>` : '<span class="empty">interactive</span>') : '';
    return `<tr class="clickable" data-chassis="${escape(chassis)}" data-agent="${escape(a.name)}"><td class="k">${escape(a.name)}</td><td>${task}</td><td>${when}</td></tr>`;
  }).join("") + '</table>';
}

function renderCron(jobs){
  if (!jobs || !jobs.length) return '<div class="empty">no cron jobs</div>';
  return '<table>' + jobs.map(j => {
    const next = j.next ? escape(relTime(j.next)) : '<span class="empty">—</span>';
    return `<tr><td class="k">${escape(j.schedule)}</td><td>${escape(j.command)}</td><td>${next}</td></tr>`;
  }).join("") + '</table>';
}

function renderAudit(audit){
  if (!audit || !audit.length) return '<div class="empty">no audit entries</div>';
  return audit.map(e => {
    const bad = e.status === "denied" || (e.exit && e.exit !== 0);
    const note = bad ? ` ✗ ${escape(e.reason || "exit " + e.exit)}` : "";
    return `<div class="line clickable ${bad ? "deny" : ""}" data-audit="${escape(JSON.stringify(e))}"><span class="ts">${escape(relTime(e.ts))}</span> ${escape(e.tool)}${note}</div>`;
  }).join("");
}

function renderPage(c){
  if (!c.running){
    return `${renderStats(c)}<div class="block"><div class="empty">chassis is not running</div></div>`;
  }
  return `
    ${renderStats(c)}
    <div class="blocks">
      <div class="block"><h3>agents</h3>${renderAgents(c.name, c.agents)}</div>
      <div class="block"><h3>cron</h3>${renderCron(c.cron)}</div>
      <div class="block"><h3>recent tools</h3>${renderAudit(c.audit)}</div>
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
    const page=document.getElementById("page");
    if(data.error || !data.chassis){
      renderTopBar(null);
      page.innerHTML=`<div class="error-page">${escape(data.error || "no chassis")}</div>`;
    }else{
      renderTopBar(data.chassis);
      document.title = `chassis · ${data.chassis.name}`;
      page.innerHTML = renderPage(data.chassis);
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
    args = ap.parse_args()
    CHASSIS_NAME = args.chassis
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
