"""Orchestrates one probe loop (and one periodic route tracer) per endpoint.

The Monitor runs on its own asyncio event loop (in a background thread).  All
public mutating methods are thread-safe so the HTTP server can add/remove
endpoints from any worker thread.
"""

from __future__ import annotations

import asyncio
import time

from .probes import icmp_loop, tcp_loop
from .store import Store
from .traceroute import run_trace


class Monitor:
    def __init__(self, store: Store, retention_days: float = 7.0,
                 hop_interval: float = 60.0):
        self.store = store
        self.retention_days = retention_days
        self.hop_interval = hop_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[int, asyncio.Task] = {}
        self._hop_tasks: dict[int, asyncio.Task] = {}
        self._stop_events: dict[int, asyncio.Event] = {}

    async def start(self):
        self._loop = asyncio.get_running_loop()
        local_agent = self.store.get_local_agent_id()
        for ep in self.store.list_endpoints(agent_id=local_agent):
            if ep["enabled"]:
                self._spawn(ep)
        while True:
            await asyncio.sleep(3600)
            self._prune_all()

    def _call(self, coro):
        assert self._loop is not None, "Monitor not started"
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # -- public API (thread-safe) ------------------------------------------
    def add(self, endpoint: dict):
        self._call(self._add_async(endpoint))

    def remove(self, endpoint_id: int):
        self._call(self._remove_async(endpoint_id))

    def set_enabled(self, endpoint: dict, enabled: bool):
        self._call(self._set_enabled_async(endpoint, enabled))

    # -- internal (must run on the loop thread) ----------------------------
    async def _add_async(self, ep: dict):
        self._spawn(ep)

    async def _remove_async(self, endpoint_id: int):
        self._stop(endpoint_id)

    async def _set_enabled_async(self, ep: dict, enabled: bool):
        if enabled:
            self._spawn(ep)
        else:
            self._stop(ep["id"])

    def _spawn(self, ep: dict):
        eid = ep["id"]
        if eid in self._tasks and not self._tasks[eid].done():
            return
        stop = asyncio.Event()
        self._stop_events[eid] = stop
        self._tasks[eid] = asyncio.create_task(self._run_loop(ep, stop))
        if self.hop_interval > 0:
            self._hop_tasks[eid] = asyncio.create_task(
                self._run_hop_loop(ep, stop))

    def _stop(self, endpoint_id: int):
        ev = self._stop_events.pop(endpoint_id, None)
        probe = self._tasks.pop(endpoint_id, None)
        hop = self._hop_tasks.pop(endpoint_id, None)
        if ev:
            ev.set()
        for task in (probe, hop):
            if task:
                task.cancel()

    async def _run_loop(self, ep: dict, stop: asyncio.Event):
        eid = ep["id"]
        host = ep["host"]
        interval = ep["interval"] or 1.0

        def emit(result):
            self.store.insert_sample(
                eid, result.ts, result.seq, result.rtt_ms, result.lost)

        if ep["probe_type"] == "tcp":
            timeout = max(2.0, interval)
            await tcp_loop(host, ep["port"] or 443, interval, timeout, emit,
                           stop)
        else:
            await icmp_loop(host, interval, emit, stop)

    async def _run_hop_loop(self, ep: dict, stop: asyncio.Event):
        """Periodically run a traceroute and record per-hop measurements."""
        eid = ep["id"]
        host = ep["host"]
        while not stop.is_set():
            try:
                records = await run_trace(host, query=3, max_hops=20)
            except FileNotFoundError:
                return  # traceroute not installed; give up on this endpoint
            except Exception:
                records = []
            for r in records:
                self.store.insert_hop_sample(
                    eid, r["ts"], r["hop"], r["address"], r["host"], r["rtt_ms"])
            try:
                await asyncio.wait_for(stop.wait(),
                                       timeout=max(self.hop_interval, 1.0))
            except asyncio.TimeoutError:
                pass

    def _prune_all(self):
        cutoff = time.time() - self.retention_days * 86400
        for ep in self.store.list_endpoints():
            try:
                self.store.prune(ep["id"], cutoff)
            except Exception:
                pass

    async def shutdown(self):
        for eid in list(self._stop_events):
            self._stop(eid)
        for t in list(self._tasks.values()) + list(self._hop_tasks.values()):
            try:
                await t
            except asyncio.CancelledError:
                pass
