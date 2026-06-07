#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "croniter>=2.0",
# ]
# ///
"""
chassis tui — a btop-style terminal monitor for a single chassis container.

Same data as the web dashboard (dashboard/app.py), rendered with curses:
  - hero: container CPU% and memory% as braille history graphs
  - tasks: per-(agent,task) cron next-fire + live running indicator
  - audit: tail of the dispatcher log, deny/error rows highlighted
  - triggers: recent trigger fan-outs

Picks the chassis automatically when exactly one is running; pass
`--chassis <name>` when several are up. Run it on the host (it shells out
to `docker`), e.g.:

    dashboard/tui.py
    dashboard/tui.py --chassis researcher --interval 1.5

Architecture follows the three-layer split the dashboard's TUI guide
recommends: collectors do I/O and return a plain snapshot (never raise into
the UI); state.py-style ring buffers hold history; the curses layer is the
only thing that touches the terminal. A daemon Poller samples docker at the
configured interval while the render loop ticks at 4 Hz so input stays snappy.

Keys:  q quit   p pause polling   +/- poll faster/slower
"""
from __future__ import annotations

import argparse
import curses
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

try:
    from croniter import croniter
except ImportError:  # next-fire times just go blank without it
    croniter = None


# ---------- constants -----------------------------------------------------

CHASSIS_SUFFIX = "-chassis"
AUDIT_LOG = "/var/log/chassis/run-tool.jsonl"
TRIGGER_LOG = "/var/log/chassis/triggers.log"
AGENTS_LOG_ROOT = "/var/log/chassis/agents"
_AGENT_HOME = os.environ.get("AGENT_HOME", "/home/agent")

RENDER_TICK = 0.25            # render loop / input poll cadence (seconds)
DEFAULT_POLL = 2.0            # docker sampling interval
MIN_POLL, MAX_POLL = 0.5, 30.0
SERIES_MAX = 512              # history ring-buffer depth
HOST_FALLBACK_FRACTION = 0.5  # matches the dashboard's gauge scaling

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")
# C0 (minus none), DEL, and C1 control chars — stripped from any text that
# originated inside the container before it reaches the terminal.
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize(s: str) -> str:
    return _CTRL.sub(" ", s or "")


# Public extension surface — what a downstream app builds on. See
# dashboard/README.md § Extending.
__all__ = [
    "Snapshot", "History", "RenderContext", "Panel", "HeroPanel", "FnPanel",
    "FooterPanel", "default_panels", "Theme", "Painter", "Rect",
    "compute_layout", "collect", "collect_tasks", "collect_audit",
    "collect_fires", "draw_hero", "draw_tasks", "draw_audit", "draw_triggers",
    "draw_footer", "App", "Poller", "SERIES_MAX", "DEFAULT_POLL", "MIN_POLL",
    "MAX_POLL",
]


# ---------- collectors (I/O → snapshot; never raise into the UI) ----------


def _run(*cmd: str, timeout: float = 6.0) -> tuple[int, str]:
    """Run a host command with list args (never shell=True). Returns
    (returncode, stdout). Any failure collapses to (1, "")."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""
    return p.returncode, p.stdout


def _exec(name: str, script: str, user: str = "agent", timeout: float = 5.0) -> str:
    """`docker exec` a static `sh -c` script inside the container. The script
    is a constant defined in this file; the only interpolated value is the
    (validated) container name, so there's no host-shell injection surface."""
    if not _SAFE_NAME.match(name):
        return ""
    rc, out = _run("docker", "exec", "-u", user, name, "sh", "-c", script, timeout=timeout)
    return out if rc == 0 else ""


def list_chassis() -> list[dict]:
    rc, out = _run(
        "docker", "ps", "-a", "--format",
        '{{.Names}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}',
    )
    rows: list[dict] = []
    if rc != 0:
        return rows
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2].endswith(CHASSIS_SUFFIX):
            continue
        rows.append({"name": parts[0], "status_line": parts[1]})
    rows.sort(key=lambda r: r["name"])
    return rows


def resolve_chassis(want: str | None) -> tuple[str | None, str]:
    chassis = list_chassis()
    if want:
        if any(c["name"] == want for c in chassis):
            return want, ""
        return None, f"chassis {want!r} not found"
    if not chassis:
        return None, "no -chassis containers found"
    if len(chassis) > 1:
        names = ", ".join(c["name"] for c in chassis)
        return None, f"multiple chassis found ({names}); pass --chassis <name>"
    return chassis[0]["name"], ""


def _parse_cpuset(s: str) -> int:
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


def _host_cpu_cores() -> float:
    return float(os.cpu_count() or 1)


_BYTE_UNITS = {
    "B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
}
_BYTE_RE = re.compile(r"([0-9.]+)\s*([A-Za-z]+)")


def parse_bytes(s: str) -> float:
    m = _BYTE_RE.search(s or "")
    if not m:
        return 0.0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0.0
    return val * _BYTE_UNITS.get(m.group(2).upper(), 1)


@dataclass
class Snapshot:
    """Raw values from one poll. `ok` is False when the container can't be
    inspected; panels render an 'unavailable' state rather than crashing."""
    ok: bool = False
    error: str = ""
    name: str = ""
    status: str = ""
    running: bool = False
    started_at: str = ""
    restart_count: int = 0
    cpu_pct: float = 0.0          # raw docker CPUPerc (can exceed 100 on multi-core)
    cpu_cores: float = 1.0        # effective ceiling → drives the chart's vmax
    mem_pct: float = 0.0
    mem_used_bytes: float = 0.0
    mem_limit_bytes: float = 0.0
    tasks: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    fires: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    # Free-form bag for downstream collectors to attach custom data without
    # touching `config` (which draw_footer reads and is reserved for docker
    # config). Additive + last-field-with-default → non-breaking. A custom
    # collector composes: `snap = collect(name); snap.extra[...] = ...`.
    extra: dict = field(default_factory=dict)
    ts: float = 0.0

    @property
    def uptime_s(self) -> float:
        if not self.started_at:
            return 0.0
        # Docker StartedAt carries 9 fractional digits + Z; trim to microseconds.
        s = re.sub(r"(\.\d{6})\d*", r"\1", self.started_at).replace("Z", "+00:00")
        try:
            started = datetime.fromisoformat(s)
        except ValueError:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


CRON_LINE_RE = re.compile(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s")
TRIGGER_LINE_RE = re.compile(r"^(\S+)\s+(\S+)")
FIRE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] trigger (?P<src>\S+) "
    r"\((?P<kind>\S+) rc=(?P<rc>-?\d+)(?: verdict=\{[^}]*\})?\) "
    r"-> (?P<dst>\S+)"
)


def _next_fire(expr: str) -> float | None:
    if not (expr and croniter):
        return None
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        nxt = croniter(expr, now).get_next(datetime)
        return (nxt - now).total_seconds()
    except Exception:
        return None


def collect_tasks(name: str) -> list[dict]:
    raw = _exec(
        name,
        "for d in /home/agent/*/*/; do "
        '  [ -f "$d/INSTRUCTIONS.md" ] || continue; '
        '  task=$(basename "$d"); agent=$(basename "$(dirname "$d")"); '
        "  cron=$(grep -v '^[[:space:]]*#' \"$d/cron\" 2>/dev/null | grep -v '^[[:space:]]*$' | head -1 | sed 's/^[[:space:]]*//'); "
        "  trig=$(grep -v '^[[:space:]]*#' \"$d/trigger\" 2>/dev/null | grep -v '^[[:space:]]*$' | head -1 | sed 's/^[[:space:]]*//'); "
        '  printf "%s\\t%s\\t%s\\t%s\\n" "$agent" "$task" "$cron" "$trig"; '
        "done",
    )
    # run-agent processes currently executing → live "running" dots.
    ps = _exec(name, "ps -eo args 2>/dev/null", user="root")
    running: set[tuple[str, str]] = set()
    for line in ps.splitlines():
        m = re.search(r"(?:^|/)run-agent\s+(\S+)\s+(\S+)", line)
        if m:
            running.add((m.group(1), m.group(2)))

    tasks: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        agent, task, cron_line, trig_line = parts[0], parts[1], parts[2], parts[3]
        eta = _next_fire(cron_line) if cron_line else None
        trig = None
        if trig_line:
            tm = TRIGGER_LINE_RE.match(trig_line)
            if tm:
                trig = f"{tm.group(1)} {tm.group(2)}"
        tasks.append({
            "agent": agent, "task": task,
            "cron": cron_line, "eta": eta, "trigger": trig,
            "running": (agent, task) in running,
        })
    tasks.sort(key=lambda t: (t["agent"], t["task"]))
    return tasks


def collect_audit(name: str, n: int = 60) -> list[dict]:
    import json
    raw = _exec(name, f"tail -n {n} {AUDIT_LOG} 2>/dev/null")
    out: list[dict] = []
    for line in raw.strip().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()  # newest first
    return out


def collect_fires(name: str, n: int = 40) -> list[dict]:
    raw = _exec(name, f"tail -n {n} {TRIGGER_LOG} 2>/dev/null")
    fires: list[dict] = []
    for line in raw.splitlines():
        m = FIRE_RE.match(line)
        if not m:
            continue
        fires.append({
            "ts": m["ts"], "src": m["src"], "dst": m["dst"],
            "kind": m["kind"], "rc": int(m["rc"]),
        })
    fires.reverse()
    return fires


def collect(name: str) -> Snapshot:
    """One full poll. Catches everything: a transient docker hiccup yields a
    not-ok snapshot, never an exception in the render loop."""
    snap = Snapshot(name=name, ts=time.time())
    try:
        rc, out = _run(
            "docker", "inspect", name, "--format",
            "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.Running}}|"
            "{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.CpusetCpus}}",
        )
        if rc != 0:
            snap.error = "container not found"
            return snap
        p = out.strip().split("|")
        if len(p) < 7:
            snap.error = "unexpected inspect output"
            return snap
        snap.ok = True
        snap.status = p[0]
        snap.started_at = p[1]
        snap.restart_count = int(p[2] or 0)
        snap.running = p[3].lower() == "true"

        nano = int(p[4] or 0)            # HostConfig.NanoCpus
        cpuset_n = _parse_cpuset(p[6])   # HostConfig.CpusetCpus
        if nano > 0:
            snap.cpu_cores = nano / 1e9
        elif cpuset_n > 0:
            snap.cpu_cores = float(cpuset_n)
        else:
            snap.cpu_cores = _host_cpu_cores() * HOST_FALLBACK_FRACTION

        if not snap.running:
            return snap

        # docker stats --no-stream is the slow call (~1-2s); fine off the
        # render thread. CPUPerc is "12.34%", MemUsage is "used / limit".
        rc, out = _run(
            "docker", "stats", name, "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}", timeout=12.0,
        )
        if rc == 0:
            sp = out.strip().split("\t")
            if len(sp) >= 3:
                snap.cpu_pct = float(sp[0].rstrip("%") or 0)
                snap.mem_pct = float(sp[2].rstrip("%") or 0)
                used, _, limit = sp[1].partition("/")
                snap.mem_used_bytes = parse_bytes(used)
                snap.mem_limit_bytes = parse_bytes(limit)

        snap.tasks = collect_tasks(name)
        snap.audit = collect_audit(name)
        snap.fires = collect_fires(name)
    except Exception as e:  # last-resort guard — the UI must never see a raise
        snap.ok = bool(snap.name) and snap.ok
        snap.error = f"{type(e).__name__}: {e}"
    return snap


# ---------- state (ring buffers + derived series) -------------------------


class History:
    def __init__(self) -> None:
        self.cpu = deque(maxlen=SERIES_MAX)   # percent of one core (0..cores*100)
        self.mem = deque(maxlen=SERIES_MAX)   # percent 0..100

    def update(self, snap: Snapshot) -> None:
        if snap.ok and snap.running:
            self.cpu.append(snap.cpu_pct)
            self.mem.append(snap.mem_pct)


# ---------- widgets (pure: no curses, no I/O) -----------------------------

BRAILLE_BASE = 0x2800
# bit per (row 0..3 top→bottom, col 0..1)
BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def braille_chart(series, width: int, height: int, vmin: float, vmax: float) -> list[str]:
    """Render `series` into `height` rows of `width` braille cells. Each cell
    is a 2×4 dot matrix, so the chart packs width*2 samples × height*4 levels.
    History is right-aligned; a continuous one-dot floor keeps the baseline
    visible. Returns `height` equal-width strings (top row first)."""
    if width <= 0 or height <= 0:
        return []
    if vmax <= vmin:
        vmax = vmin + 1.0
    dot_cols = width * 2
    dot_rows = height * 4
    samples = list(series)[-dot_cols:]
    samples = [0.0] * (dot_cols - len(samples)) + samples

    filled = []
    for v in samples:
        norm = (v - vmin) / (vmax - vmin)
        norm = 0.0 if norm < 0 else 1.0 if norm > 1 else norm
        f = int(round(norm * dot_rows))
        if f == 0:
            f = 1  # baseline floor
        filled.append(f)

    rows = []
    for cell_row in range(height):
        line = []
        for cell_col in range(width):
            bits = 0
            for dr in range(4):
                global_row = cell_row * 4 + dr  # 0 = top dot of whole chart
                for dc in range(2):
                    f = filled[cell_col * 2 + dc]
                    # a column is filled from the bottom up
                    if global_row >= dot_rows - f:
                        bits |= BRAILLE_BITS[dr][dc]
            line.append(chr(BRAILLE_BASE + bits))
        rows.append("".join(line))
    return rows


def fmt_duration(secs: float) -> str:
    secs = int(max(0, secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def fmt_eta(secs: float | None) -> str:
    if secs is None:
        return "—"
    if secs < 0:
        return "due"
    return "in " + fmt_duration(secs)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def hhmmss(ts: str) -> str:
    """Best-effort HH:MM:SS from an ISO timestamp."""
    m = re.search(r"T(\d{2}:\d{2}:\d{2})", ts or "")
    return m.group(1) if m else (ts or "")[:8]


# ---------- theme (curses color tiers + positional gradient) --------------


class Theme:
    # btop-ish stops: green → yellow → red.
    STOPS = ((119, 202, 155), (203, 192, 108), (220, 76, 76))
    N = 24
    # Named threshold colors consumed by init(); a subclass overrides this dict
    # (or STOPS/N for the gradient) to rebrand without touching init(). HEAD is
    # special-cased to gain A_BOLD below.
    NAMED = {"GREEN": curses.COLOR_GREEN, "YELLOW": curses.COLOR_YELLOW,
             "RED": curses.COLOR_RED, "HEAD": curses.COLOR_CYAN}

    def __init__(self) -> None:
        self.grad: list[int] = []     # gradient attrs, green(0)→red(N-1)
        self.GREEN = self.YELLOW = self.RED = curses.A_NORMAL
        self.DIM = curses.A_DIM
        self.BOLD = curses.A_BOLD
        self.HEAD = curses.A_BOLD

    def _lerp(self, frac: float):
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        seg = frac * (len(self.STOPS) - 1)
        i = min(int(seg), len(self.STOPS) - 2)
        t = seg - i
        a, b = self.STOPS[i], self.STOPS[i + 1]
        return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))

    @staticmethod
    def _xterm256(r: int, g: int, b: int) -> int:
        # nearest color in the 6×6×6 cube (indices 16..231)
        q = lambda c: round(c / 255 * 5)
        return 16 + 36 * q(r) + 6 * q(g) + q(b)

    def init(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        colors = curses.COLORS
        pair = 1

        def mkpair(fg: int) -> int:
            nonlocal pair
            try:
                curses.init_pair(pair, fg, bg)
            except curses.error:
                return curses.A_NORMAL
            a = curses.color_pair(pair)
            pair += 1
            return a

        # named threshold colors (always available on an 8-color term)
        for attr_name, color in self.NAMED.items():
            a = mkpair(color)
            if attr_name == "HEAD":
                a |= curses.A_BOLD
            setattr(self, attr_name, a)

        # smooth gradient when the terminal can express it
        if colors >= 256 and curses.can_change_color():
            for i in range(self.N):
                r, g, b = self._lerp(i / (self.N - 1))
                slot = 16 + i
                try:
                    curses.init_color(slot, int(r * 1000 / 255), int(g * 1000 / 255), int(b * 1000 / 255))
                    self.grad.append(mkpair(slot))
                except curses.error:
                    self.grad = []
                    break
        elif colors >= 256:
            for i in range(self.N):
                r, g, b = self._lerp(i / (self.N - 1))
                self.grad.append(mkpair(self._xterm256(r, g, b)))

    def grad_attr(self, frac: float) -> int:
        """frac∈[0,1]; 1 → red end, 0 → green end."""
        if self.grad:
            idx = int(frac * (len(self.grad) - 1) + 0.5)
            idx = max(0, min(len(self.grad) - 1, idx))
            return self.grad[idx]
        if frac < 0.5:
            return self.GREEN
        if frac < 0.8:
            return self.YELLOW
        return self.RED


# ---------- ui: layout + painter + panels ---------------------------------


@dataclass
class Rect:
    y: int
    x: int
    h: int
    w: int

    def inner(self) -> "Rect":
        return Rect(self.y + 1, self.x + 1, max(0, self.h - 2), max(0, self.w - 2))


def compute_layout(maxy: int, maxx: int) -> dict[str, Rect]:
    body_h = maxy - 1  # last row is the footer
    hero_h = max(7, min(int(body_h * 0.42), body_h - 6))
    left_w = max(20, maxx // 2)
    right_x = left_w
    right_w = maxx - left_w
    below_y = hero_h
    below_h = body_h - hero_h
    trig_h = max(5, below_h // 3)
    audit_h = below_h - trig_h
    half = maxx // 2
    return {
        "cpu": Rect(0, 0, hero_h, half),
        "mem": Rect(0, half, hero_h, maxx - half),
        "tasks": Rect(below_y, 0, below_h, left_w),
        "audit": Rect(below_y, right_x, audit_h, right_w),
        "triggers": Rect(below_y + audit_h, right_x, trig_h, right_w),
        "footer": Rect(maxy - 1, 0, 1, maxx),
    }


class Painter:
    """curses window wrapper with edge-safe writes and box drawing."""

    def __init__(self, win, theme: Theme) -> None:
        self.win = win
        self.theme = theme
        self.maxy, self.maxx = win.getmaxyx()

    def text(self, y: int, x: int, s: str, attr: int = 0) -> None:
        if y < 0 or y >= self.maxy or x >= self.maxx:
            return
        if x < 0:
            s = s[-x:]
            x = 0
        s = s[: self.maxx - x]
        if not s:
            return
        # avoid the bottom-right cell write error
        if y == self.maxy - 1 and x + len(s) >= self.maxx:
            s = s[: self.maxx - x - 1]
        if not s:
            return
        try:
            self.win.addstr(y, x, s, attr)
        except curses.error:
            pass

    def box(self, r: Rect, title: str, attr: int = 0) -> None:
        if r.h < 2 or r.w < 2:
            return
        th = self.theme
        top = "╭" + "─" * (r.w - 2) + "╮"
        bot = "╰" + "─" * (r.w - 2) + "╯"
        self.text(r.y, r.x, top, th.DIM)
        self.text(r.y + r.h - 1, r.x, bot, th.DIM)
        for i in range(1, r.h - 1):
            self.text(r.y + i, r.x, "│", th.DIM)
            self.text(r.y + i, r.x + r.w - 1, "│", th.DIM)
        if title:
            t = f" {title} "[: max(0, r.w - 4)]
            self.text(r.y, r.x + 2, t, (attr or th.HEAD))


def draw_hero(p: Painter, r: Rect, title: str, series, cur: float, vmax: float,
              headline: str, avail: bool) -> None:
    th = p.theme
    p.box(r, title)
    inner = r.inner()
    if inner.h <= 0 or inner.w <= 0:
        return
    if not avail:
        p.text(inner.y, inner.x, "unavailable", th.DIM)
        return
    # headline row: big current value + detail
    frac = cur / vmax if vmax > 0 else 0
    p.text(inner.y, inner.x, headline, th.grad_attr(frac) | curses.A_BOLD)
    chart_h = inner.h - 1
    if chart_h <= 0:
        return
    rows = braille_chart(series, inner.w, chart_h, 0.0, vmax)
    n = len(rows)
    for i, row in enumerate(rows):
        # top rows → red, bottom rows → green (positional, btop-style)
        attr = th.grad_attr((n - i) / n)
        p.text(inner.y + 1 + i, inner.x, row, attr)


def draw_tasks(p: Painter, r: Rect, snap: Snapshot) -> None:
    th = p.theme
    n = len(snap.tasks)
    p.box(r, f"tasks · {n}")
    inner = r.inner()
    if inner.h <= 0:
        return
    if not snap.tasks:
        p.text(inner.y, inner.x, "no tasks", th.DIM)
        return
    for i, t in enumerate(snap.tasks[: inner.h]):
        y = inner.y + i
        label = sanitize(f"{t['agent']}/{t['task']}")
        if t["running"]:
            p.text(y, inner.x, "●", th.GREEN | curses.A_BOLD)
        else:
            p.text(y, inner.x, "·", th.DIM)
        name_w = max(8, inner.w - 14)
        p.text(y, inner.x + 2, label[:name_w], 0 if not t["running"] else curses.A_BOLD)
        eta = fmt_eta(t["eta"]) if t["cron"] else ("◀ " + (t["trigger"] or "") if t["trigger"] else "—")
        eta = sanitize(eta)
        ex = inner.x + inner.w - len(eta)
        if ex > inner.x + 2:
            p.text(y, ex, eta, th.DIM)
    if n > inner.h:
        p.text(inner.y + inner.h - 1, inner.x, f"…+{n - inner.h} more", th.DIM)


def draw_audit(p: Painter, r: Rect, snap: Snapshot) -> None:
    th = p.theme
    p.box(r, "audit log")
    inner = r.inner()
    if inner.h <= 0:
        return
    if not snap.audit:
        p.text(inner.y, inner.x, "no calls logged", th.DIM)
        return
    for i, e in enumerate(snap.audit[: inner.h]):
        y = inner.y + i
        status = e.get("status", "")
        exit_code = e.get("exit", 0)
        bad = status in ("denied", "error", "exec_error") or (exit_code not in (0, None))
        when = hhmmss(e.get("ts", ""))
        tool = sanitize(str(e.get("tool", "?")))
        tag = "deny" if status == "denied" else (status or "ok")
        line = f"{when} {tool} · {tag}"
        p.text(y, inner.x, line[: inner.w], th.RED if bad else 0)


def draw_triggers(p: Painter, r: Rect, snap: Snapshot) -> None:
    th = p.theme
    p.box(r, "triggers")
    inner = r.inner()
    if inner.h <= 0:
        return
    if not snap.fires:
        p.text(inner.y, inner.x, "no fan-outs yet", th.DIM)
        return
    for i, f in enumerate(snap.fires[: inner.h]):
        y = inner.y + i
        bad = f["rc"] != 0
        when = hhmmss(f["ts"])
        line = sanitize(f"{when} {f['src']} → {f['dst']} ({f['kind']})")
        p.text(y, inner.x, line[: inner.w], th.RED if bad else 0)


def draw_footer(p: Painter, r: Rect, snap: Snapshot, poll: float, paused: bool) -> None:
    th = p.theme
    if snap.ok:
        state = snap.status
        state_attr = th.GREEN if snap.running else th.RED
        left = f" {snap.name}  "
        p.text(r.y, 0, left, th.HEAD)
        x = len(left)
        p.text(r.y, x, state, state_attr | curses.A_BOLD)
        x += len(state)
        model = snap.config.get("model", "")
        bits = f"  up {fmt_duration(snap.uptime_s)}  ·  {snap.restart_count} restarts"
        if model:
            bits += f"  ·  {sanitize(model)}"
        p.text(r.y, x, bits, th.DIM)
    else:
        p.text(r.y, 0, f" {snap.error or 'connecting…'}", th.RED)
    right = f"poll {poll:.1f}s{' ⏸' if paused else ''}  ·  q quit  p pause  +/- rate "
    rx = p.maxx - len(right)
    if rx > 0:
        p.text(r.y, rx, right, th.DIM)


# ---------- panel contract (extension seam over the draw_* functions) -----
#
# The free `draw_*` functions above stay the source of truth. The Panel
# protocol + thin wrappers below let App iterate a registry instead of calling
# them by hand, so a downstream app can inject its own ordered panel list
# (with custom layout keys) without forking the render loop.


@dataclass
class RenderContext:
    """Everything a panel needs to draw one frame. `extra` is a free-form bag
    for downstream panels to read app-level state the stock panels ignore."""
    snap: Snapshot
    history: History
    interval: float
    paused: bool
    extra: dict = field(default_factory=dict)


class Panel(Protocol):
    key: str  # must match a compute_layout() key; unknown keys are skipped

    def draw(self, p: Painter, r: Rect, ctx: RenderContext) -> None: ...


class FnPanel:
    """Wraps a snapshot-only draw fn (draw_tasks/draw_audit/draw_triggers)."""

    def __init__(self, key: str, fn: Callable[[Painter, Rect, Snapshot], None]) -> None:
        self.key = key
        self.fn = fn

    def draw(self, p: Painter, r: Rect, ctx: RenderContext) -> None:
        self.fn(p, r, ctx.snap)


class FooterPanel:
    """Wraps draw_footer's (snap, interval, paused) signature."""

    key = "footer"

    def draw(self, p: Painter, r: Rect, ctx: RenderContext) -> None:
        draw_footer(p, r, ctx.snap, ctx.interval, ctx.paused)


class HeroPanel:
    """Reconciles draw_hero's wider 8-arg signature via lambdas over ctx, so a
    hero panel is fully described by its title + a handful of accessors."""

    def __init__(self, key: str, title: str,
                 series_fn: Callable[[RenderContext], object],
                 value_fn: Callable[[RenderContext], float],
                 vmax_fn: Callable[[RenderContext], float],
                 headline_fn: Callable[[RenderContext], str],
                 avail_fn: Callable[[RenderContext], bool]) -> None:
        self.key = key
        self.title = title
        self.series_fn = series_fn
        self.value_fn = value_fn
        self.vmax_fn = vmax_fn
        self.headline_fn = headline_fn
        self.avail_fn = avail_fn

    def draw(self, p: Painter, r: Rect, ctx: RenderContext) -> None:
        draw_hero(p, r, self.title, self.series_fn(ctx), self.value_fn(ctx),
                  self.vmax_fn(ctx), self.headline_fn(ctx), self.avail_fn(ctx))


def default_panels() -> list[Panel]:
    """The stock six panels, ordered, with keys matching compute_layout exactly.
    The hero headline/vmax expressions are byte-identical to the inline ones the
    old App._draw computed (cpu_vmax / cpu_head / mem_head / avail)."""
    return [
        HeroPanel(
            "cpu", "cpu",
            series_fn=lambda c: c.history.cpu,
            value_fn=lambda c: c.snap.cpu_pct,
            vmax_fn=lambda c: max(1.0, c.snap.cpu_cores * 100.0),
            headline_fn=lambda c: f"{c.snap.cpu_pct:5.1f}%  of {c.snap.cpu_cores:g} core{'s' if c.snap.cpu_cores != 1 else ''}",
            avail_fn=lambda c: c.snap.ok and c.snap.running,
        ),
        HeroPanel(
            "mem", "memory",
            series_fn=lambda c: c.history.mem,
            value_fn=lambda c: c.snap.mem_pct,
            vmax_fn=lambda c: 100.0,
            headline_fn=lambda c: f"{c.snap.mem_pct:5.1f}%  {fmt_bytes(c.snap.mem_used_bytes)} / {fmt_bytes(c.snap.mem_limit_bytes)}",
            avail_fn=lambda c: c.snap.ok and c.snap.running,
        ),
        FnPanel("tasks", draw_tasks),
        FnPanel("audit", draw_audit),
        FnPanel("triggers", draw_triggers),
        FooterPanel(),
    ]


# ---------- app: poller thread + render loop ------------------------------


class Poller(threading.Thread):
    def __init__(self, name: str, interval: float,
                 collector: Callable[[str], Snapshot] = collect) -> None:
        super().__init__(daemon=True)
        self.name = name
        self.interval = interval
        self._collector = collector
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._latest: Snapshot | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            if not self._paused.is_set():
                try:
                    snap = self._collector(self.name)
                except Exception as e:
                    # A downstream collector that raises would otherwise kill
                    # this thread silently and freeze the UI. Fall back to a
                    # not-ok snapshot so the footer surfaces the error.
                    snap = Snapshot(name=self.name, ts=time.time(),
                                    error=f"{type(e).__name__}: {e}")
                with self._lock:
                    self._latest = snap
            # sleep in small slices so pause / rate changes apply quickly
            slept = 0.0
            while slept < self.interval and not self._stop.is_set():
                step = min(0.1, self.interval - slept)
                time.sleep(step)
                slept += step

    def take(self) -> Snapshot | None:
        with self._lock:
            out, self._latest = self._latest, None
        return out

    def set_paused(self, val: bool) -> None:
        self._paused.set() if val else self._paused.clear()

    def stop(self) -> None:
        self._stop.set()


class App:
    def __init__(self, name: str, interval: float, *,
                 theme: Theme | None = None,
                 panels: list[Panel] | None = None,
                 layout: Callable[[int, int], dict[str, Rect]] | None = None,
                 collector: Callable[[str], Snapshot] = collect,
                 history: History | None = None) -> None:
        self.name = name
        self.interval = interval
        self.paused = False
        self.theme = theme if theme is not None else Theme()
        self.panels = panels if panels is not None else default_panels()
        self.layout = layout if layout is not None else compute_layout
        self.history = history if history is not None else History()
        self.last = Snapshot(name=name)
        self.poller = Poller(name, interval, collector=collector)

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(int(RENDER_TICK * 1000))
        self.theme.init()
        self.poller.start()
        while True:
            snap = self.poller.take()
            if snap is not None:
                self.history.update(snap)
                self.last = snap
            self._draw(stdscr)
            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch in (ord("q"), ord("Q")):
                self.poller.stop()
                return
            if ch in (ord("p"), ord("P")):
                self.paused = not self.paused
                self.poller.set_paused(self.paused)
            elif ch in (ord("+"), ord("=")):
                self._rate(-0.5)
            elif ch in (ord("-"), ord("_")):
                self._rate(+0.5)
            elif ch == curses.KEY_RESIZE:
                stdscr.clear()

    def _rate(self, delta: float) -> None:
        self.interval = max(MIN_POLL, min(MAX_POLL, self.interval + delta))
        self.poller.interval = self.interval

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        maxy, maxx = stdscr.getmaxyx()
        p = Painter(stdscr, self.theme)
        if maxy < 14 or maxx < 50:
            p.text(0, 0, "terminal too small (need ≥ 50×14)", self.theme.RED)
            stdscr.noutrefresh()
            curses.doupdate()
            return
        lay = self.layout(maxy, maxx)
        ctx = RenderContext(self.last, self.history, self.interval, self.paused)
        for panel in self.panels:
            r = lay.get(panel.key)
            if r is not None:
                panel.draw(p, r, ctx)

        stdscr.noutrefresh()
        curses.doupdate()


# ---------- entrypoint ----------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="btop-style TUI monitor for a chassis container.")
    ap.add_argument("--chassis", metavar="NAME", default=os.environ.get("CHASSIS_NAME"),
                    help="container to monitor (auto-picked when exactly one is running)")
    ap.add_argument("--interval", type=float, default=DEFAULT_POLL,
                    help=f"poll interval in seconds (default {DEFAULT_POLL})")
    args = ap.parse_args()

    name, err = resolve_chassis(args.chassis)
    if err:
        print(f"chassis tui: {err}")
        return 1
    interval = max(MIN_POLL, min(MAX_POLL, args.interval))
    try:
        App(name, interval).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
