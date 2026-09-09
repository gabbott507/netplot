"""Command line interface for netplot.

Usage:
    python3 -m netplot [--host H] [--port P] [--db PATH]   # run dashboard
    python3 -m netplot add <host> [--name N] [--probe icmp|tcp] [--port P] [--interval SEC]
    python3 -m netplot list
    python3 -m netplot remove <id-or-host>

The bare invocation runs the background monitor plus the web server.  The
`add` / `list` / `remove` commands operate on the same SQLite DB so you can
manage endpoints from the CLI and then serve them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading

from .agent import run_agent
from .monitor import Monitor
from .server import Handler, NetPlotServer
from .store import Store

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".netplot", "netplot.db")


def _get_db(args) -> str:
    return getattr(args, "db", None) or DEFAULT_DB


def cmd_add(args):
    store = Store(_get_db(args))
    eid = store.add_endpoint(
        args.host, args.name or args.host, args.port, args.probe, args.interval)
    print(f"added #{eid}: {args.host} [{args.probe}] every {args.interval}s")
    store.close()


def cmd_list(args):
    store = Store(_get_db(args))
    rows = store.list_endpoints()
    if not rows:
        print("(no endpoints)  add one with:  python3 -m netplot add <host>")
        return
    print(f"{'id':<4} {'enabled':<8} {'type':<5} {'int(s)':<7} host")
    for r in rows:
        print(f"{r['id']:<4} {str(bool(r['enabled'])):<8} {r['probe_type']:<5} "
              f"{r['interval']:<7} {r['host']}"
              + (f" :{r['port']}" if r['probe_type'] == 'tcp' else ""))
    store.close()


def cmd_remove(args):
    store = Store(_get_db(args))
    target = args.key
    found = [r for r in store.list_endpoints()
             if str(r["id"]) == target or r["host"] == target]
    if not found:
        print(f"no endpoint matching {target!r}")
        store.close()
        sys.exit(1)
    for r in found:
        store.delete_endpoint(r["id"])
        print(f"removed #{r['id']} {r['host']}")
    store.close()


def cmd_agent_add(args):
    store = Store(_get_db(args))
    agent = store.add_agent(args.name)
    server = args.server or f"http://{args.host}:{args.port}"
    print(f"created agent '{args.name}' (id={agent['id']})")
    print(f"token:  {agent['token']}")
    print(f"add targets for it, then install on the endpoint and run:")
    print(f"  python3 -m netplot agent --server {server} --token {agent['token']}")
    store.close()


def cmd_agent(args):
    asyncio.run(run_agent(args.server, args.token, args.buffer,
                         args.config_interval, args.flush_interval))


def run_server(args):
    db_path = os.path.expanduser(_get_db(args))
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    store = Store(db_path)
    monitor = Monitor(store, retention_days=args.retention_days,
                      hop_interval=args.hop_interval)

    def run_monitor():
        asyncio.run(monitor.start())

    threading.Thread(target=run_monitor, daemon=True).start()

    auth_user = os.environ.get("NETPLOT_USER", "") or (args.user or "")
    auth_pass = os.environ.get("NETPLOT_PASSWORD", "") or (args.password or "")
    server = NetPlotServer(
        (args.host, args.port), Handler, store, monitor, args.retention_days,
        auth_user=auth_user, auth_pass=auth_pass)
    if auth_user:
        print(f"auth enabled for user '{auth_user}'")
    url = f"http://{args.host}:{args.port}"
    print(f"netplot {__import__('netplot').__version__} listening on {url}"
          f"   (db: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="netplot", description="PingPlotter-style network monitor")
    parser.add_argument("--db", default=None, help="SQLite database path")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="add an endpoint to monitor")
    p_add.add_argument("host")
    p_add.add_argument("--name", default=None)
    p_add.add_argument("--probe", choices=["icmp", "tcp"], default="icmp")
    p_add.add_argument("--port", type=int, default=443)
    p_add.add_argument("--interval", type=float, default=1.0)

    sub.add_parser("list", help="list monitored endpoints")

    p_rm = sub.add_parser("remove", help="remove an endpoint by id or host")
    p_rm.add_argument("key")

    p_tag = sub.add_parser("agent-add", help="register a remote agent on the server")
    p_tag.add_argument("name")
    p_tag.add_argument("--server", default=None,
                       help="server URL to print in the install command")

    p_ag = sub.add_parser("agent", help="run a remote monitoring agent")
    p_ag.add_argument("--server", required=True, help="central server base URL")
    p_ag.add_argument("--token", required=True, help="agent auth token")
    p_ag.add_argument("--buffer", default=os.path.expanduser("~/.netplot/agent-buffer.db"),
                      help="local SQLite buffer path")
    p_ag.add_argument("--config-interval", type=float, default=30.0)
    p_ag.add_argument("--flush-interval", type=float, default=5.0)

    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--retention-days", type=float, default=7.0)
    parser.add_argument("--hop-interval", type=float, default=30.0,
                        help="seconds between route (hop) traces; 0 disables")
    parser.add_argument("--user", default=None,
                        help="dashboard login username (or NETPLOT_USER)")
    parser.add_argument("--password", default=None,
                        help="dashboard login password (or NETPLOT_PASSWORD)")

    args = parser.parse_args(argv)
    if args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "remove":
        cmd_remove(args)
    elif args.cmd == "agent":
        cmd_agent(args)
    elif args.cmd == "agent-add":
        cmd_agent_add(args)
    else:
        run_server(args)


if __name__ == "__main__":
    main()
