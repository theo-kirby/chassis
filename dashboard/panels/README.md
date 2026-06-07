# dashboard panels — project extension seam

The dashboard ships as a single self-contained `app.py`. This directory is the
**one seam** a project layered on chassis uses to add its own panels *without
forking that file*. Drop a module here and the dashboard auto-discovers it at
import time; with no modules present the dashboard is byte-identical to stock
and the seam is wholly inert.

A panel is a Python module at `dashboard/panels/<name>.py`. Files starting with
`_` are ignored. Modules are imported in sorted filename order. Each module may
expose any subset of these attributes:

| Attr | Req | Meaning |
|---|---|---|
| `ID: str` | **yes** | Unique panel id — used as the snapshot key (`chassis.panels[ID]`) and the DOM id (`panel-<ID>`). A module with no truthy `ID` is loaded but **not registered**. |
| `TITLE: str` | no | Panel header text. Defaults to `ID`. |
| `COLUMN: str` | no | `"full"` (default) renders a full-width row below the main grid. `"left"`/`"right"` are accepted and reserved for column placement; **v1 renders them full-width too** (column-specific placement is a documented follow-up). |
| `async def snapshot(container, exec_in)` | no | Server-side data collector. Receives the chassis **container name** and the engine's `exec_in(container, *cmd, ...)` coroutine (use it to read from inside the container — it is the only sanctioned way in). Must return a JSON-serializable value. Runs only when the container is up. Wrapped in `try/except` → the panel's data becomes `{"_error": "<msg>"}` on failure (the dashboard never crashes on a bad panel). |
| `JS: str` | no | Client code spliced into the inline `<script>`. It is expected to call `registerPanel({id, title, column, render})`. `render(data, chassis)` is **synchronous** — its `data` argument is already `chassis.panels[id]` from the latest poll; it returns an HTML string for the panel body. |
| `CSS: str` | no | Styles spliced into `<head>`. |
| `FILE_PREFIXES: tuple[str, ...]` | no | Extra path prefixes unioned into the `/api/file` read allowlist, so a panel can offer drill-in links (`data-file=...`) to files it cares about. |

## Client helpers a panel can piggyback

The core JS already provides, and panels may reuse:

- `registerPanel({id, title, render})` — register your panel's renderer.
- `escape(s)` — HTML-escape a string.
- The `.block` / `.block-body` markup (your body is rendered inside one) and the
  existing `openModal(title, html)` + click-delegation: an element with
  `data-file="<path>"` opens that file (subject to the allowlist) in the modal;
  `data-audit` shows an audit record. Reuse these instead of inventing new UI.

## Minimal example

```python
# dashboard/panels/example.py
ID = "example"
TITLE = "example"
COLUMN = "full"
FILE_PREFIXES = ("/home/agent/lab/",)


async def snapshot(container, exec_in):
    return {"head": await exec_in(container, "sh", "-c",
                                  "head -c 2000 /home/agent/lab/THESIS.md 2>/dev/null")}


JS = r"""
registerPanel({
  id: "example", title: "example", column: "full",
  render(data, chassis){
    if(!data || data._error) return `<div class="empty">no data</div>`;
    return `<pre>${escape(data.head || "")}</pre>`;
  }
});
"""
```

## Security

Panel modules are **operator-authored host code**: they run in the dashboard
process (the same trust boundary as `app.py` itself), not inside the container,
and are loaded only from this directory. They do not widen the container's
attack surface. Two invariants hold regardless of what a panel does:

- `/api/file` stays gated by `ALLOWED_FILE_PREFIXES` (which a panel may *extend*
  via `FILE_PREFIXES`, never bypass) and the `FILE_MAX_BYTES` cap. Panels read
  container files only through `exec_in` / `/api/file`, never a raw shell.
- The dashboard remains **read-only and unauthenticated** — a panel must never
  write. Keep it bound to localhost / your tailnet; never expose it publicly.
