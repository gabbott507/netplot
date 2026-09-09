"""Probe implementations: continuous ICMP ping and raw TCP connect.

Auto-detects the operating system and picks the right ``ping`` invocation and
output parser (Linux/macOS iputils ``ping`` vs Windows ``ping.exe``). The TCP
probe uses stdlib asyncio sockets and is fully cross-platform.

Each probe is a coroutine that calls ``emit(ProbeResult)`` for every completed
probe tick.  ``stop`` is an asyncio.Event the owning loop sets for shutdown.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

IS_WINDOWS = os.name == "nt"


@dataclass
class ProbeResult:
    ts: float
    seq: int
    rtt_ms: Optional[float] = None
    lost: bool = False


# Linux/macOS iputils ping lines:
#   "... icmp_seq=12 ... time=34.5 ms"   and   "no answer yet for icmp_seq=12"
_REPLY_RE = re.compile(r"icmp_seq=(\d+).*?time=([0-9.]+)")
_OSS_RE = re.compile(r"no answer yet for icmp_seq=(\d+)")

# Windows ping.exe replies:
#   "Reply from 1.1.1.1: bytes=32 time=30ms TTL=59"  (time= or time<)
_WIN_REPLY_RE = re.compile(r"Reply from .*?time[=<]([\d.]+)\s*ms")


def _terminate(proc: asyncio.subprocess.Process):
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


async def icmp_loop(host, interval, emit, stop):
    """ICMP ping with OS-specific command and parser."""
    if IS_WINDOWS:
        await _icmp_windows_loop(host, emit, stop)
    else:
        await _icmp_posix_loop(host, interval, emit, stop)


async def _icmp_posix_loop(host, interval, emit, stop):
    wait = max(2.0, interval)  # how long ping waits before declaring loss
    while not stop.is_set():
        cmd = ["ping", "-n", "-O", "-i", str(interval), "-W", str(wait), host]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            await asyncio.sleep(max(interval, 1.0))
            continue

        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                m = _REPLY_RE.search(line)
                if m:
                    emit(ProbeResult(time.time(), int(m.group(1)),
                                     rtt_ms=float(m.group(2))))
                    continue
                m2 = _OSS_RE.search(line)
                if m2:
                    emit(ProbeResult(time.time(), int(m2.group(1)), lost=True))
            await proc.wait()
        except asyncio.CancelledError:
            _terminate(proc)
            raise
        _terminate(proc)
        await asyncio.sleep(max(interval, 1.0))


async def _icmp_windows_loop(host, emit, stop):
    """Windows ping -t never stops on its own; it paces itself (~1/sec)."""
    while not stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-t", host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            await asyncio.sleep(1.0)
            continue

        seq = 0
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                m = _WIN_REPLY_RE.search(line)
                if m:
                    seq += 1
                    emit(ProbeResult(time.time(), seq, rtt_ms=float(m.group(1))))
                    continue
                low = line.lower()
                if "timed out" in low or "unreachable" in low or "no reply" in low:
                    seq += 1
                    emit(ProbeResult(time.time(), seq, lost=True))
            await proc.wait()
        except asyncio.CancelledError:
            _terminate(proc)
            raise
        _terminate(proc)
        await asyncio.sleep(1.0)


async def tcp_loop(host, port, interval, timeout, emit, stop):
    """Probe a TCP endpoint by timing a connect() to ``host:port``."""
    ip = host
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
        if infos:
            ip = infos[0][4][0]
    except OSError:
        pass

    seq = 0
    while not stop.is_set():
        seq += 1
        start = time.monotonic()
        successful = False
        rtt = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout)
            rtt = (time.monotonic() - start) * 1000.0
            successful = True
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        except (asyncio.TimeoutError, OSError):
            pass

        if stop.is_set():
            break
        emit(ProbeResult(time.time(), seq, rtt_ms=rtt, lost=not successful))
        await asyncio.sleep(max(interval, 0.0))
