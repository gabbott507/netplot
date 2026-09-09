# netplot

A lightweight, dependency-free [PingPlotter](https://www.pingplotter.com/)-style
network monitor. It continuously probes one or more endpoints, records latency
and packet loss to SQLite, and shows the results on a live web dashboard.

It can run as a single machine (one server that probes from itself), or in a
**server/agent** topology: lightweight agents installed on remote endpoints keep
their traffic **outbound-only** (no ports opened on the agent), pull their
monitoring assignments from the server, probe, and push results back.

No third-party packages are required — it runs on the Python standard library.

<img width="2403" height="1041" alt="image" src="https://github.com/user-attachments/assets/4fc15acd-d108-4450-9aa1-e7faf4f264d8" />

## Features

- **Continuous monitoring** of any number of endpoints at once.
- **Two probe types:**
  - `icmp` — classic ping (via the system `ping` tool), includes packet-loss
    detection.
  - `tcp` — measures TCP connect() time to a host:port (works for sites that
    block ICMP, e.g. many web servers).
- **Live dashboard** (served over HTTP) with:
  - latency-over-time chart with red packet-loss markers
  - a second chart plotting **jitter** (change between consecutive samples)
  - a **route table** with per-hop min / avg / max / loss %, like PingPlotter
  - min / avg / max / jitter / packet-loss % and probe counts
  - selectable time windows (1m → 24h)
  - add / pause / resume / remove endpoints from the UI
  - on-demand `traceroute` plus automatic periodic route traces
  - pure-canvas charting (no CDN), so it keeps working when the network is down
- **History** persisted in SQLite with automatic pruning (default 7 days).

## Quick start

```bash
cd netplot
# Start the server + monitor (defaults: 127.0.0.1:8000, ~/.netplot/netplot.db)
python3 -m netplot

# Or add endpoints from the CLI first, then start the server:
python3 -m netplot add 1.1.1.1 --name "Cloudflare DNS"
python3 -m netplot add example.com --probe tcp --port 443
python3 -m netplot list
python3 -m netplot remove 1.1.1.1
```

Then open <http://127.0.0.1:8000> in your browser.

## Dashboard authentication

The dashboard and API can be protected with a login if it will be reachable
over the internet. Set the username/password either via environment variables or
CLI flags:

```bash
NETPLOT_USER=admin NETPLOT_PASSWORD='a-strong-password' python3 -m netplot --host 0.0.0.0
# equivalent:  python3 -m netplot --host 0.0.0.0 --user admin --password 'a-strong-password'
```

When enabled:

- unauthenticated requests to `/api/*` return `401`;
- a browser login form appears (session cookie, with a Logout button);
- remote agents are *not* affected - they authenticate separately with their own
  per-agent token via `X-Agent-Token`.

If neither the env vars nor the flags are set, the dashboard runs with no login
(intended for local/trusted use only).

## Docker

There is a `Dockerfile` plus a generic `docker-compose.yml`. The image ships
`ping` and `traceroute` and adds `NET_RAW` so ICMP/traceroute work unprivileged.
History persists in a volume (`/data`). This is the deployment used for the
server/agent topology:

```bash
docker build -t netplot .
docker compose up -d
```

`netplot-stack.example.yml` is a template for deploying as a managed stack
(e.g. Portainer) behind Caddy; copy it to `netplot-stack.yml` and fill in the
secrets. (That file is git-ignored so live credentials don't leak into the repo.)

## Windows / packaging

The agent is pure Python stdlib, so it installs easily on Windows. Two options:

1. **Embedded-Python bundle (no install).** A prebuilt `dist/netplot-agent-win-x64.zip`
   bundles a Windows Python 3.12 runtime plus the agent. Unzip, set `SERVER` and
   `TOKEN` in `config.txt`, then `run-agent.bat` to test or `install-service.bat`
   (as Administrator) to register a startup scheduled task.
2. **Single `.exe` via PyInstaller.** Drop `packaging/build_exe.bat` and
   `packaging/netplot.spec` onto any Windows PC with Python and build:

   ```bat
   build_exe.bat
   ```

   which produces `dist\netplot-agent.exe` - a single file with all dependencies,
   no Python required on the endpoint.

Either way the agent only makes **outbound** HTTPS (port 443) calls, so no
inbound ports are opened on the endpoint.

## Server / agent mode


Want to monitor from *other* locations without opening any inbound ports on
them? Install a small **agent** on each remote machine. The agent only makes
outbound HTTPS calls to your server — it pulls its list of targets, probes
them, buffers results locally, and pushes them back.

```bash
# 1. On the server, register an agent (prints its token + install command):
python3 -m netplot agent-add myBranch --server https://netplot.example.com
#    -> token:  AbC...   and the exact command to run on the endpoint

# 2. In the dashboard, select "myBranch" in the agent dropdown and add
#    targets — they get assigned to that agent automatically.

# 3. On the endpoint, run the printed command:
python3 -m netplot agent --server https://netplot.example.com --token AbC...
```

- Agents authenticate with a per-agent token (`X-Agent-Token`) and can only
  push data for endpoints assigned to them.
- If the server is unreachable the agent keeps collecting into a local SQLite
  buffer and retries, so the outage itself is captured in the data.
- The dashboard dropdowns become **Agent → endpoint**, with an online/offline
  indicator from agent heartbeats.

The server itself is always an agent named `local`, probed in-process, so
existing single-machine use is unchanged.

## Platform support (auto-detected)

The agent and server auto-detect the OS at startup (`os.name`) and pick the
right tools and output parsers, so the **same** agent command runs on Linux,
macOS, or Windows:

- **ICMP ping** — Linux/macOS use the iputils `ping` (`-n -O -i`); Windows uses
  `ping -t`, parsing `Reply from ... time=Nms` and `Request timed out.` lines.
- **Route / per-hop table** — Linux/macOS use `traceroute`; Windows uses
  `tracert -h 20`, parsing its 3-column hop format (including `<1 ms` and `*`).
- **TCP probe** — stdlib asyncio sockets; fully cross-platform.

Requirements per platform:
- Linux/macOS: `ping` and (for hop stats) `traceroute` from PATH.
- Windows: a 64-bit Python 3.9+ on PATH, plus the built-in `ping.exe`/`tracert.exe`
  (present by default). No admin rights, no inbound ports, no native deps.

Note: on Windows, `ping -t` paces itself at about one probe per second (there is
no sub-second interval), independent of the configured `interval`.


## CLI reference

```
python3 -m netplot                              # run server + monitor
python3 -m netplot add <host> [options]         # add an endpoint
        --name NAME         display name (defaults to host)
        --probe icmp|tcp    probe type (default icmp)
        --port PORT         TCP port when probe=tcp (default 443)
        --interval SEC      probe interval in seconds (default 1.0, min 0.2)
python3 -m netplot list                         # list endpoints
python3 -m netplot remove <id-or-host>          # remove an endpoint
python3 -m netplot agent-add <name> [--server URL]  # register an agent + print install cmd
python3 -m netplot agent --server URL --token TOK   # run a remote agent
        --config-interval SEC    how often to pull its assignment (default 30)
        --flush-interval SEC     how often to push buffered data (default 5)
        --buffer PATH            local SQLite buffer (default ~/.netplot/agent-buffer.db)
```

Server options:

```
--host HOST      bind address (default 127.0.0.1)
--port PORT      port (default 8000)
--db PATH        SQLite database (default ~/.netplot/netplot.db)
--retention-days N   how long to keep history (default 7)
--hop-interval S     seconds between automatic route (hop) traces (default 30; 0 disables)
```

## HTTP API

| Method | Path | Description |
|---|---|---|
| GET  | `/`                        | Web dashboard |
| GET  | `/api/info`                | Version + settings |
| GET  | `/api/endpoints`           | List endpoints |
| POST | `/api/endpoints`           | Add endpoint (JSON) |
| POST | `/api/endpoints/enable`    | Pause/resume `{id, enabled}` |
| DELETE| `/api/endpoints?endpoint=` | Remove endpoint + history |
| GET  | `/api/samples?endpoint=&window=` | Recent samples (seconds) |
| GET  | `/api/summary?endpoint=&window=` | Min/avg/max/loss%/jitter |
| GET  | `/api/trace?host=`         | Run one traceroute |
| GET  | `/api/route?endpoint=&window=` | Per-hop route stats (min/avg/max/loss%) |
| GET  | `/api/agents` | List agents (with last_seen status) |
| POST | `/api/agents` | Create an agent (returns its token) |
| DELETE | `/api/agents?agent=` | Delete an agent + its endpoints/data |
| POST | `/api/agent/config` | Agent pulls its assignment (token auth) |
| POST | `/api/agent/ingest` | Agent pushes samples/hops (token auth) |

## Notes & limitations

- ICMP ping requires the system `ping` command (present on virtually all
  Linux/macOS systems). Raw-ping needs no root here because the tool shells out
  to `ping` rather than opening raw sockets itself.
- `traceroute` is optional; the Traceroute button and the automatic per-hop
  route table need it installed.
- Route traces run automatically every `--hop-interval` seconds (default 30) and
  accumulate per-hop stats over time. The route table truncates the trailing
  `*` filler traceroute prints after reaching the destination.
- The dashboard polls latency/jitter once per second and the route table every
  10 seconds.
- Loss flags from `ping -O` (late replies) are deduplicated by sequence number in
  the summary so a genuine reply supersedes a premature loss marker.

## Project layout

```
netplot/
  netplot/
    probes.py      ICMP (subprocess ping) and TCP connect probe loops
    monitor.py     orchestration: one loop per endpoint, pruning
    store.py       SQLite layer
    stats.py       min/avg/max, loss%, jitter computation
    server.py      threaded HTTP server + JSON API
    traceroute.py  per-hop route tracing (traceroute / tracert)
    agent.py       remote agent: pulls config, probes, pushes results
    web/index.html dashboard (canvas chart, no CDN)
    __main__.py    CLI entry point
  packaging/       PyInstaller spec + build script for a Windows .exe
  docker-compose.yml, Dockerfile
  README.md
```
