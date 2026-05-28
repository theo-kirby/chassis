# tools/

The chassis's tool registry.

## tools.json

Each entry:

```json
{
  "name": "echo",
  "description": "Echo a message back. Used for smoke tests.",
  "script": "echo",
  "runner": "python3",
  "args_schema": {
    "type": "object",
    "properties": { "msg": { "type": "string" } },
    "required": ["msg"],
    "additionalProperties": false
  },
  "secrets": []
}
```

| field | meaning |
|---|---|
| `name` | What the agent sees and what callers pass to `run-tool`. Lowercase + hyphens. |
| `description` | One line, surfaced to the agent. Write it for the agent. |
| `script` | Path under `tools/` to the implementation. |
| `runner` | Interpreter: `python3`, `bash`, `node`, etc. Resolved via `$PATH` inside the container. |
| `args_schema` | JSON Schema validated by the dispatcher before exec. Use `additionalProperties: false`. |
| `secrets` | List of `.env` keys to inject into the tool's child env. Nothing else from `.env` is visible. |

## Implementation contract

The tool script is exec'd as: `<runner> <script> <json-args>`. The single argv element is the JSON-encoded args object that already passed `args_schema` validation in the dispatcher.

```python
import json, sys
args = json.loads(sys.argv[1])
```

The dispatcher captures stdout and stderr, redacts every `.env` value out of both, and returns the result to the agent. Exit non-zero on failure.

## Adding a tool

1. Write the script at `tools/<name>`; read JSON args from `argv[1]`. The script runs **as root** with only `PATH`/`HOME`/`LANG`, the declared `secrets`, and the chassis-context vars (`CHASSIS_AGENT`, `CHASSIS_TASK`, `CHASSIS_VERDICT_FILE`) in its env.
2. Append an entry to `tools/tools.json` (see schema above).
3. `./chassis reload-cron` — re-renders `/etc/chassis/tools-public.json` so the next agent session sees the new tool.
