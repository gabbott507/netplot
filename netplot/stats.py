"""Compute PingPlotter-style summary statistics from a list of samples."""

from __future__ import annotations

from typing import Dict, List


def summarize(samples: List[dict]) -> Dict:
    """Return min/avg/max RTT, packet loss %, jitter, and counts.

    Samples may contain duplicate ``seq`` values (a late reply after an
    `no answer yet` marker).  We keep the most favourable outcome per seq so a
    genuine reply supersedes an earlier loss flag for that same probe.
    """
    # Dedupe by seq, preferring a successful (non-lost) result.
    best: Dict[int, dict] = {}
    for s in samples:
        seq = s["seq"]
        prev = best.get(seq)
        if prev is None:
            best[seq] = s
        elif not s["lost"] and prev["lost"]:
            best[seq] = s
        elif prev["rtt_ms"] is None and s["rtt_ms"] is not None and not s["lost"]:
            best[seq] = s

    ok = [s["rtt_ms"] for s in best.values()
          if not s["lost"] and s["rtt_ms"] is not None]
    lost = sum(1 for s in best.values() if s["lost"])
    total = len(best)

    loss_pct = (lost / total * 100.0) if total else 0.0

    rtt_min = min(ok) if ok else None
    rtt_max = max(ok) if ok else None
    rtt_avg = (sum(ok) / len(ok)) if ok else None

    # Jitter = average absolute difference between consecutive RTTs.
    jitter = None
    if len(ok) >= 2:
        diffs = [abs(ok[i] - ok[i - 1]) for i in range(1, len(ok))]
        jitter = sum(diffs) / len(diffs)

    return {
        "count": total,
        "ok": len(ok),
        "lost": lost,
        "loss_pct": round(loss_pct, 2),
        "min_ms": None if rtt_min is None else round(rtt_min, 2),
        "avg_ms": None if rtt_avg is None else round(rtt_avg, 2),
        "max_ms": None if rtt_max is None else round(rtt_max, 2),
        "jitter_ms": None if jitter is None else round(jitter, 2),
        "first_ts": samples[0]["ts"] if samples else None,
        "last_ts": samples[-1]["ts"] if samples else None,
    }


def hop_stats(records: List[dict]) -> List[dict]:
    """Aggregate traceroute probe records per hop.

    Hops beyond the deepest hop that ever responded are trailing `*` filler
    (traceroute keeps printing star hops after reaching the destination), so
    we truncate the route there and only report hops 1..lastResponded.
    """
    by_hop: Dict[int, List[dict]] = {}
    for r in records:
        by_hop.setdefault(r["hop"], []).append(r)

    deepest = 0
    for hop, recs in by_hop.items():
        if any(r["rtt_ms"] is not None for r in recs):
            deepest = max(deepest, hop)

    hops = []
    for hop in sorted(by_hop):
        if hop > deepest:
            continue
        recs = by_hop[hop]
        ok = [r["rtt_ms"] for r in recs if r["rtt_ms"] is not None]
        lost = len(recs) - len(ok)
        total = len(recs)
        # newest address/host seen for this hop
        addr = recs[-1]["address"]
        host = recs[-1]["host"]

        hops.append({
            "hop": hop,
            "address": addr,
            "host": host,
            "count": total,
            "ok": len(ok),
            "lost": lost,
            "loss_pct": round(lost / total * 100.0, 1) if total else 0.0,
            "min_ms": round(min(ok), 2) if ok else None,
            "avg_ms": round(sum(ok) / len(ok), 2) if ok else None,
            "max_ms": round(max(ok), 2) if ok else None,
        })
    return hops
