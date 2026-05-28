# dashboard

Local web monitor for a single chassis container. Shows status, CPU/mem,
cron schedule + next-fire times, last agent runs, and the dispatcher audit
log on a single auto-refreshing page.

Click an audit row to see the full record. Click an agent row to drill into
its recent runs and open the Pi session file or per-run log.

Read-only — it calls `docker ps`, `docker stats`, `docker exec` against
an already-running chassis. There is no auth and no TLS; do not expose it
on the public internet.

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
