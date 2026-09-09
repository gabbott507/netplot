"""Parse traceroute/tracert output and run the OS-appropriate command.

Auto-detects the platform and runs ``traceroute`` (Linux/macOS) or
``tracert.exe`` (Windows), parsing each probe into its own record so we can
accumulate per-hop stats (min/avg/max/loss%) over many traces.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import List

IS_WINDOWS = os.name == "nt"

# ------------------------------------------------------------- Linux/macOS
_HOP_RE = re.compile(r"^\s*(\d+)\s+")
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_HOST_RE = re.compile(r"\(([^)]+)\)")
_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms")


def parse_posix_trace_line(line: str) -> List[dict]:
    line = line.strip()
    if not line:
        return []
    m = _HOP_RE.match(line)
    if not m:
        return []
    hop = int(m.group(1))

    ipm = _IP_RE.search(line)
    address = ipm.group(1) if ipm else "*"
    hostm = _HOST_RE.search(line)
    host = hostm.group(1).strip() if hostm else None
    if host and host == address:
        host = None

    times = [float(x) for x in _TIME_RE.findall(line)]
    stars = line.count("*")

    recs = []
    for t in times:
        recs.append({"hop": hop, "address": address, "host": host, "rtt_ms": t})
    for _ in range(stars):
        recs.append({"hop": hop, "address": address, "host": host, "rtt_ms": None})
    return recs


# ---------------------------------------------------------------- Windows
# tracert prints up to 3 probe columns per hop: "N ms", "<1 ms", or "*",
# followed by the address (IP, or "hostname [ip]" with reverse DNS).
_WIN_COL = r"((?:\d+|<1)\s*ms|\*)"
_WIN_LINE_RE = re.compile(
    rf"^\s*(\d+)\s+{_WIN_COL}\s+{_WIN_COL}\s+{_WIN_COL}\s*(.*)$")


def parse_windows_trace_line(line: str) -> List[dict]:
    m = _WIN_LINE_RE.match(line)
    if not m:
        return []
    hop = int(m.group(1))
    cols = [m.group(2), m.group(3), m.group(4)]
    addr_text = (m.group(5) or "").strip()
    if not addr_text or addr_text.lower() in (
            "request timed out.", "destination host unreachable."):
        addr_text = ""

    # address: prefer the IP; keep the DNS name as "host" if present
    ipm = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", addr_text)
    address, host = None, None
    if ipm:
        ip = ipm.group(1)
        hostname = addr_text.replace(f"[{ip}]", "").replace(ip, "").strip(" ()")
        address, host = ip, (hostname or None)
    elif addr_text:
        address = addr_text  # hostname with no IP shown

    recs = []
    for c in cols:
        if c.strip() == "*":
            recs.append({"hop": hop, "address": address, "host": host,
                         "rtt_ms": None})
        else:
            val = float(c.strip().rstrip("ms").replace("<", ""))
            recs.append({"hop": hop, "address": address, "host": host,
                         "rtt_ms": val})
    return recs


def parse_trace_line(line: str) -> List[dict]:
    """Dispatch to the platform-appropriate traceroute line parser."""
    if IS_WINDOWS:
        return parse_windows_trace_line(line)
    return parse_posix_trace_line(line)


async def run_trace(host: str, query: int = 3, max_hops: int = 20,
                    wait: int = 1) -> List[dict]:
    """Run one traceroute/tracert and return a flat list of probe records."""
    if IS_WINDOWS:
        cmd = ["tracert", "-h", str(max_hops), "-w", str(wait * 1000), host]
    else:
        cmd = ["traceroute", "-w", str(wait), "-q", str(query),
               "-m", str(max_hops), host]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    ts = time.time()
    records = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        for rec in parse_trace_line(line):
            rec["ts"] = ts
            records.append(rec)
    await proc.wait()
    return records
