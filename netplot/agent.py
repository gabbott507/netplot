"""Standalone remote agent.

The agent makes only outbound connections to the central server (all traffic is
initiated by the agent), so no inbound ports need to be opened on the machine
running it.  It:

  1. pulls its assigned endpoints from the server (``/api/agent/config``),
  2. probes them using the same engines as the local monitor,
  3. buffers every result in a small local SQLite outbox,
  4. periodically pushes batches to the server (``/api/agent/ingest``).

If the server is unreachable the agent keeps buffering locally and retries, so
a broken link to the server is itself captured in the data it eventually
delivers.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import sqlite3
import threading
import time
import urllib.request
from typing import List, Optional, Tuple

from .probes import icmp_loop, tcp_loop
from .traceroute import run_trace

BUFFER_RETENTION_S = 24 * 3600  # how long to keep unsent data if server is down


class Outbox:
    """Local SQLite buffer of samples and hop records waiting to be pushed."""

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint_id INTEGER NOT NULL,
                    ts REAL NOT NULL, seq INTEGER NOT NULL,
                    rtt_ms REAL, lost INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS hops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint_id INTEGER NOT NULL, ts REAL NOT NULL,
                    hop INTEGER NOT NULL, address TEXT, host TEXT, rtt_ms REAL
                );
                CREATE INDEX IF NOT EXISTS idx_s_ts ON samples (ts);
                CREATE INDEX IF NOT EXISTS idx_h_ts ON hops (ts);
                """
            )
            self._conn.commit()

    def append_sample(self, endpoint_id, ts, seq, rtt_ms, lost):
        with self._lock:
            self._conn.execute(
                "INSERT INTO samples (endpoint_id, ts, seq, rtt_ms, lost) "
                "VALUES (?,?,?,?,?)",
                (endpoint_id, ts, seq, rtt_ms, 1 if lost else 0))
            self._conn.commit()

    def append_hop(self, endpoint_id, ts, hop, address, host, rtt_ms):
        with self._lock:
            self._conn.execute(
                "INSERT INTO hops (endpoint_id, ts, hop, address, host, rtt_ms) "
                "VALUES (?,?,?,?,?,?)",
                (endpoint_id, ts, hop, address, host, rtt_ms))
            self._conn.commit()

    def unsent(self, limit: int = 10000) -> Tuple[List[dict], List[dict]]:
        with self._lock:
            srows = self._conn.execute(
                "SELECT * FROM samples ORDER BY id LIMIT ?", (limit,)).fetchall()
            hrows = self._conn.execute(
                "SELECT * FROM hops ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in srows], [dict(r) for r in hrows]

    def delete_ids(self, sample_ids: List[int], hop_ids: List[int]):
        with self._lock:
            if sample_ids:
                marks = ",".join("?" * len(sample_ids))
                self._conn.execute(
                    f"DELETE FROM samples WHERE id IN ({marks})", sample_ids)
            if hop_ids:
                marks = ",".join("?" * len(hop_ids))
                self._conn.execute(
                    f"DELETE FROM hops WHERE id IN ({marks})", hop_ids)
            self._conn.commit()

    def drop_endpoints(self, keep_ids):
        # Drop buffered records for endpoints the agent no longer manages so
        # stale batches are never retried forever after an assignment change.
        keep = set(keep_ids)
        with self._lock:
            if keep:
                marks = ",".join("?" * len(keep))
                self._conn.execute(
                    f"DELETE FROM samples WHERE endpoint_id NOT IN ({marks})",
                    list(keep))
                self._conn.execute(
                    f"DELETE FROM hops WHERE endpoint_id NOT IN ({marks})",
                    list(keep))
            else:
                self._conn.execute("DELETE FROM samples")
                self._conn.execute("DELETE FROM hops")
            self._conn.commit()

    def prune(self, cutoff: Optional[float] = None):
        cutoff = cutoff or (time.time() - BUFFER_RETENTION_S)
        with self._lock:
            self._conn.execute("DELETE FROM samples WHERE ts<?", (cutoff,))
            self._conn.execute("DELETE FROM hops WHERE ts<?", (cutoff,))
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()


class Agent:
    def __init__(self, server: str, token: str, db_path: str,
                 config_interval: float = 30.0, flush_interval: float = 5.0):
        self.server = server.rstrip("/")
        self.token = token
        self.outbox = Outbox(db_path)
        self.config_interval = config_interval
        self.flush_interval = flush_interval
        self.hop_interval = 30.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._probe_tasks: dict[int, asyncio.Task] = {}
        self._hop_tasks: dict[int, asyncio.Task] = {}
        self._stops: dict[int, asyncio.Event] = {}

    # ------------------------------------------------------------------ HTTP
    # Cloudflare blocks the default "Python-urllib" User-Agent with a 403
    # (error 1010, browser-integrity check), so present a normal browser UA.
    UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(self.server + path, method="POST")
        req.add_header("User-Agent", self.UA)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Agent-Token", self.token)
        data = json.dumps(payload).encode()
        with urllib.request.urlopen(req, data=data, timeout=25) as resp:
            return json.loads(resp.read().decode())

    async def _post_async(self, path: str, payload: dict) -> dict:
        return await asyncio.to_thread(self._post, path, payload)

    async def fetch_config(self) -> dict:
        return await self._post_async("/api/agent/config", {
            "token": self.token,
            "hostname": socket.gethostname(),
        })

    # ------------------------------------------------------------------ run
    async def run(self):
        self._loop = asyncio.get_running_loop()
        # wait until we can talk to the server on first boot
        while True:
            try:
                cfg = await self.fetch_config()
                break
            except Exception as exc:
                self.outbox.prune()
                print(f"agent: cannot reach {self.server} ({exc}); retrying...")
                await asyncio.sleep(10)

        self.hop_interval = cfg.get("hop_interval", 30.0)
        self.sync_endpoints(cfg["config"]["endpoints"])
        print(f"agent: connected as '{cfg['agent']['name']}' on {platform.system()}"
              f" (os={os.name}) with {len(cfg['config']['endpoints'])} managed endpoint(s)")
        await asyncio.gather(
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._poll_loop()),
        )

    def sync_endpoints(self, endpoints: List[dict]):
        live = set()
        for ep in endpoints:
            if not ep.get("enabled", 1):
                continue
            eid = ep["id"]
            live.add(eid)
            if eid not in self._probe_tasks or self._probe_tasks[eid].done():
                stop = asyncio.Event()
                self._stops[eid] = stop
                self._probe_tasks[eid] = asyncio.create_task(
                    self._run_probe(ep, stop))
                self._hop_tasks[eid] = asyncio.create_task(
                    self._run_hops(ep, stop))
        for eid in list(self._probe_tasks):
            if eid not in live:
                self._stop(eid)
        self.outbox.drop_endpoints(live)

    def _stop(self, eid: int):
        stop = self._stops.pop(eid, None)
        for task in (self._probe_tasks.pop(eid, None),
                     self._hop_tasks.pop(eid, None)):
            if task:
                task.cancel()
        if stop:
            stop.set()

    # ------------------------------------------------------------- probe loops
    async def _run_probe(self, ep: dict, stop_event: asyncio.Event):
        def emit(result):
            self.outbox.append_sample(
                ep["id"], result.ts, result.seq, result.rtt_ms, result.lost)
        interval = ep["interval"] or 1.0
        if ep["probe_type"] == "tcp":
            await tcp_loop(ep["host"], ep["port"] or 443, interval,
                           max(2.0, interval), emit, stop_event)
        else:
            await icmp_loop(ep["host"], interval, emit, stop_event)

    async def _run_hops(self, ep: dict, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                records = await run_trace(ep["host"])
            except FileNotFoundError:
                return
            except Exception:
                records = []
            for r in records:
                self.outbox.append_hop(
                    ep["id"], r["ts"], r["hop"], r["address"], r["host"],
                    r["rtt_ms"])
            try:
                await asyncio.wait_for(stop_event.wait(),
                                       timeout=max(self.hop_interval, 1.0))
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ background
    async def _poll_loop(self):
        while True:
            await asyncio.sleep(self.config_interval)
            try:
                cfg = await self.fetch_config()
                self.hop_interval = cfg.get("hop_interval", self.hop_interval)
                self.sync_endpoints(cfg["config"]["endpoints"])
            except Exception as exc:
                print(f"agent: config poll failed ({exc}); keeping current set")

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(self.flush_interval)
            await self.flush()

    async def flush(self):
        self.outbox.prune()
        sample_rows, hop_rows = self.outbox.unsent()
        if not sample_rows and not hop_rows:
            return
        payload = {
            "samples": [
                {"endpoint": r["endpoint_id"], "ts": r["ts"], "seq": r["seq"],
                 "rtt_ms": r["rtt_ms"], "lost": bool(r["lost"])}
                for r in sample_rows
            ],
            "hops": [
                {"endpoint": r["endpoint_id"], "ts": r["ts"], "hop": r["hop"],
                 "address": r["address"], "host": r["host"], "rtt_ms": r["rtt_ms"]}
                for r in hop_rows
            ],
        }
        try:
            resp = await self._post_async("/api/agent/ingest", payload)
            if resp.get("ok"):
                self.outbox.delete_ids(
                    [r["id"] for r in sample_rows],
                    [r["id"] for r in hop_rows])
        except Exception as exc:
            print(f"agent: ingest failed ({exc}); {len(sample_rows)+len(hop_rows)} "
                  f"record(s) buffered for retry")

    async def shutdown(self):
        for eid in list(self._stops):
            self._stop(eid)
        for t in list(self._probe_tasks.values()) + list(self._hop_tasks.values()):
            try:
                await t
            except asyncio.CancelledError:
                pass
        self.outbox.close()


async def run_agent(server: str, token: str, db_path: str,
                    config_interval: float, flush_interval: float):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    agent = Agent(server, token, db_path, config_interval, flush_interval)
    try:
        await agent.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C cancels the event-loop task, which surfaces here as
        # CancelledError (not KeyboardInterrupt). Clean up and exit quietly
        # rather than dumping an asyncio traceback.
        print("\nagent: shutting down")
        try:
            await agent.shutdown()
        except BaseException:
            pass
