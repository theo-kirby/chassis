# dashboard

Local web monitor for a single chassis container. Shows status, CPU/mem,
cron schedule + next-fire times, last agent runs, and the dispatcher audit
log on a single auto-refreshing page.

Click an audit row to see the full record. Click an agent row to drill into
its recent runs and open the Pi session file or per-run log.

Read-only — it calls `docker ps`, `docker stats`, `docker exec` against
an already-running chassis. There is no auth and no TLS; do not expose it
on the public internet.

Prefer the terminal? [`tui.py`](tui.py) is a btop-style curses monitor over
the same data — CPU/mem history graphs, task next-fire times with live
running dots, and tails of the audit log and trigger fan-outs. See
[Terminal UI](#terminal-ui) below.

## Run

The script declares its own dependencies via [PEP 723](https://peps.python.org/pep-0723/),
so [uv](https://docs.astral.sh/uv/) handles them automatically:

```sh
./app.py                         # http://127.0.0.1:8765
./app.py --chassis my-chassis    # explicit target
./app.py --host 0.0.0.0 --port 9000
./app.py --reload                # dev: restart on app.py changes, browser auto-refreshes
```

The chassis is auto-picked when exactly one container with a
`-chassis`-suffixed compose project is running. If you have multiple,
pass `--chassis <container-name>` (run a separate instance per chassis
on different ports).

If you don't have uv, install the three deps manually and run with python:

```sh
pip install 'fastapi>=0.110' 'uvicorn>=0.27' 'croniter>=2.0'
python app.py
```

## Terminal UI

`tui.py` is a [btop](https://github.com/aristocratos/btop)-style monitor in
the terminal — no browser, no server. Same `docker`-only, read-only data
sources as the web dashboard, rendered with curses. Run it on the host:

```sh
./tui.py                         # auto-picks the running chassis
./tui.py --chassis my-chassis    # explicit target
./tui.py --interval 1            # sample docker every 1s (default 2s)
```

Layout: CPU% and memory% as braille history graphs up top (positional
green→red gradient, degrading to 256/8-color as the terminal allows), then
tasks (cron next-fire + a live `●` for tasks whose `run-agent` is executing),
the dispatcher audit tail (deny/error in red), and recent trigger fan-outs.

Keys: `q` quit · `p` pause polling · `+`/`-` poll faster/slower.

A background thread samples docker at the poll interval while the screen
redraws at 4 Hz, so input stays responsive even though `docker stats` is
slow. `uv` resolves the one dependency (`croniter`, for next-fire times);
without uv, `pip install 'croniter>=2.0'` and run with `python tui.py`.

## Extending

Both monitors are a reusable backbone: an app *built on* chassis can rebrand
and extend them without forking. With no env vars set and no kwargs passed,
`./tui.py` and `./app.py` behave identically to stock — every seam below
defaults to today's value.

> Note: `chassis tui` spawns `tui.py` as a subprocess, so these import-time
> hooks are for a downstream app that drives `tui.App(...)` itself; they don't
> change the bundled CLI.

### TUI — custom panels

A panel is anything matching the `Panel` protocol: a `key: str` plus
`draw(self, p: Painter, r: Rect, ctx: RenderContext)`. The free `draw_*`
functions stay the source of truth; thin wrappers adapt them:

- `FnPanel(key, fn)` — for a `fn(p, r, snap)` (like `draw_tasks`).
- `FooterPanel()` — wraps `draw_footer(p, r, snap, interval, paused)`.
- `HeroPanel(key, title, series_fn, value_fn, vmax_fn, headline_fn, avail_fn)`
  — each `*_fn` takes the `RenderContext` and returns its value.

`RenderContext` carries `snap: Snapshot`, `history: History`,
`interval: float`, `paused: bool`, and a free-form `extra: dict`.

`default_panels()` returns the stock six (`cpu, mem, tasks, audit, triggers,
footer`). Build your own list in any order. **A panel's `key` must match a key
returned by your layout** — `App` looks the rect up by key and *silently skips*
a panel whose key has no rect, which is exactly the seam for adding a region
(custom `layout` → new key) plus a matching panel:

```python
def my_layout(maxy, maxx):
    lay = tui.compute_layout(maxy, maxx)
    lay["queue"] = tui.Rect(0, 0, 1, maxx)   # carve out a new region
    return lay

class QueuePanel:
    key = "queue"
    def draw(self, p, r, ctx):
        p.text(r.y, r.x, f"queue: {ctx.snap.extra.get('queue_depth', 0)}")

panels = tui.default_panels() + [QueuePanel()]
```

### TUI — custom collectors + `snap.extra`

A collector is a `name -> Snapshot` callable. `Snapshot.extra` is a free-form
dict reserved for your data (don't reuse `config` — `draw_footer` reads it).
Compose on top of the stock collector:

```python
def my_collector(name):
    snap = tui.collect(name)        # stock docker poll
    snap.extra["queue_depth"] = read_my_queue(name)
    return snap
```

The collector runs on the poller thread; its result is published under the
existing lock, so it's thread-safe with the render loop. If it raises, the
poller falls back to a not-ok `Snapshot` (the footer shows the error) rather
than freezing the UI — but prefer catching inside the collector and returning a
not-ok snapshot yourself.

`History` stays concrete (`cpu`/`mem` deques, referenced by the hero panels).
To track extra series, subclass it — call `super().update(snap)`, then append
from `snap.extra`:

```python
class MyHistory(tui.History):
    def __init__(self):
        super().__init__()
        self.queue = collections.deque(maxlen=tui.SERIES_MAX)
    def update(self, snap):
        super().update(snap)
        if snap.ok and snap.running:
            self.queue.append(snap.extra.get("queue_depth", 0))
```

### TUI — custom theme

Subclass `Theme`. Override the gradient by setting the class attrs `STOPS`
(a tuple of `(r, g, b)` stops) and/or `N` (gradient resolution). Override the
named threshold colors via the `NAMED` dict (keys `GREEN`/`YELLOW`/`RED`/`HEAD`,
values `curses.COLOR_*` ints); `HEAD` always gains `A_BOLD`. `init()` consumes
both — no need to override it:

```python
class MyTheme(tui.Theme):
    STOPS = ((0, 0, 255), (128, 0, 255), (255, 0, 255))   # blue → purple
    NAMED = dict(tui.Theme.NAMED, HEAD=curses.COLOR_MAGENTA)
```

### TUI — wiring it together

```python
from dashboard import tui

tui.App(
    name, interval,
    theme=MyTheme(),
    panels=panels,
    layout=my_layout,
    collector=my_collector,
    history=MyHistory(),
).run()
```

Every kwarg is optional and defaults to the stock value.

### Dashboard — palette via `CHASSIS_THEME_CSS`

Set `CHASSIS_THEME_CSS=/path/to/override.css` to recolor the page. The file is
appended after the base `<style>`, so by CSS cascade a later `:root { … }` wins
— list **only** the vars you change. A bad path never 500s the page (it falls
back to stock). The overridable `:root` custom properties:

```
--bg-topbar --bg-base --bg-card --bg-sub --bg-input --bg-hover
--border-1 --border-2
--text-1 --text-2 --text-3 --text-4
--red --red-soft --red-dim
--danger --danger-soft --danger-dim
--green
--edge-after --gauge-lo --gauge-hi
```

The JS-drawn parts (the CPU/mem gauge and the trigger graph's edge/arrow
colors) read these vars at startup via `getComputedStyle`, so they follow your
override automatically — the palette is the single source of truth. Example
`override.css`:

```css
:root { --red: #7c3aed; --green: #10b981; --edge-after: #555; }
```

### Dashboard — branding

```
CHASSIS_BRAND_NAME=myapp        # topbar wordmark (default "chassis")
CHASSIS_BRAND_TITLE=My Monitor  # <title> (default "chassis")
CHASSIS_BRAND_EMOJI=🚀          # favicon + topbar mark (default 🏎️)
```

### Importing the modules

The hooks above need the modules importable. `tui.py` and `app.py` are both
plain importable modules (the PEP 723 header is comment-only; curses/uvicorn
only start under the `__main__` guard). They live in `dashboard/`, so:

```python
from dashboard import tui    # needs the repo root on sys.path
import app                   # needs dashboard/ on sys.path
```

Each module's `__all__` documents its public surface.

## Remote access via Tailscale

Bind to all interfaces and reach the dashboard at your machine's tailnet name:

```sh
./app.py --host 0.0.0.0
# http://<machine>.<tailnet>.ts.net:8765
```

Tailscale ACLs gate who on your tailnet can connect. The dashboard has no
auth of its own, so the tailnet ACL is the access control — don't combine
`--host 0.0.0.0` with a public IP.

## File reads

The drill-in panel calls `/api/file/{container}?path=...`. Reads are
restricted to `/home/agent/.pi/` and `/var/log/chassis/` and capped at
200 KB (last bytes). The endpoint is not a generic shell.
