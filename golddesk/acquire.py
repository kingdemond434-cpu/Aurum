"""Active information acquisition (#1).

Sol is an investigator, not a passive classifier: it may say "last 30s of
ticks", "m1 close-up", "refresh dxy", "refresh 2y yield", "gc volume around
last move" — and the desk fulfills what it can deterministically, appends a
[REQUESTED FOLLOW-UP] block to the SAME brief, and only then accepts the final
read.

Rules from the constitution:
  - The desk never invents a number. Any domain with no underlying observation
    renders UNAVAILABLE and says why; absence must not read as calm.
  - The cycle is bounded (one follow-up round per wake) and cheap: everything
    here is local determinism, never another model call.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

Fetcher = Callable[[str, float], Optional[tuple[Optional[float], datetime, str]]]

_BLOCK_TITLE = "[REQUESTED FOLLOW-UP]"


@dataclass
class Tick:
    ts: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class TickRing:
    """Bounded ring of the last observed quotes. Fed on every tick by the desk."""
    maxlen: int = 900
    ticks: deque = field(default_factory=deque)

    def push(self, tick: Tick) -> None:
        self.ticks.append(tick)
        if len(self.ticks) > self.maxlen:
            self.ticks.popleft()

    def last_seconds(self, seconds: float) -> list[Tick]:
        now = self.ticks[-1].ts if self.ticks else None
        if now is None:
            return []
        start = now - timedelta(seconds=seconds)
        return [t for t in self.ticks if t.ts >= start]

    def minutes(self, n: int) -> list[Tick]:
        if not self.ticks:
            return []
        start = self.ticks[-1].ts - timedelta(minutes=n)
        return [t for t in self.ticks if t.ts >= start]

    def stats(self, ticks: Sequence[Tick]) -> dict:
        if not ticks:
            return {}
        mids = [t.mid for t in ticks]
        spreads = [t.ask - t.bid for t in ticks]
        moves = [b - a for a, b in zip(mids, mids[1:])]
        ups = sum(1 for m in moves if m > 0)
        downs = sum(1 for m in moves if m < 0)
        dur = (ticks[-1].ts - ticks[0].ts).total_seconds() or 1.0
        return {
            "n": len(ticks),
            "first": ticks[0].mid,
            "last": ticks[-1].mid,
            "net": ticks[-1].mid - ticks[0].mid,
            "high": max(mids), "low": min(mids),
            "ticks_per_s": round(len(ticks) / dur, 2),
            "up_ticks": ups, "down_ticks": downs,
            "mean_spread": round(statistics.fmean(spreads), 3),
            "max_spread": round(max(spreads), 3),
            "mid_velocity_per_s": round((ticks[-1].mid - ticks[0].mid) / dur, 4),
        }

    def render_seconds(self, seconds: float) -> str:
        s = self.stats(self.last_seconds(seconds))
        if not s:
            return "UNAVAILABLE — tick buffer empty (no quotes yet this session)."
        return (f"last {seconds:g}s of quote flow: {s['n']} ticks at "
                f"{s['ticks_per_s']}/s, mid {s['first']:.2f} -> {s['last']:.2f} "
                f"({s['net']:+.2f}, high {s['high']:.2f} low {s['low']:.2f}), "
                f"up {s['up_ticks']} / down {s['down_ticks']}, "
                f"spread mean {s['mean_spread']:.3f} max {s['max_spread']:.3f}")

    def render_m1(self) -> str:
        ticks = self.minutes(2)
        if not ticks:
            return "UNAVAILABLE — tick buffer empty (no quotes yet this session)."
        buckets: dict[str, list[float]] = {}
        for t in ticks:
            buckets.setdefault(t.ts.strftime("%H:%M"), []).append(t.mid)
        if len(buckets) < 2:
            s = self.stats(ticks)
            return ("M1 close-up: fewer than two full minutes observed — "
                    f"{s['n']} ticks spanning "
                    f"{ticks[0].ts:%H:%M:%S}-{ticks[-1].ts:%H:%M:%S}, "
                    f"range {s['low']:.2f}-{s['high']:.2f}, net {s['net']:+.2f}")
        line = []
        for bucket, mids in sorted(buckets.items()):
            line.append(
                f"{bucket} O:{mids[0]:.2f} H:{max(mids):.2f} L:{min(mids):.2f} "
                f"C:{mids[-1]:.2f} ticks:{len(mids)}")
        last_mid = mids[-1]
        return "M1 close-up (from live quotes):\n  " + "\n  ".join(line)


@dataclass
class AcquireState:
    """What this desk can currently fetch. Honest caps, deliberately defaults off."""
    tick_ring: Optional[TickRing] = None
    fetcher: Optional[Fetcher] = None          # e.g. crossmarket.build_state(…).fetch
    fetch_hours: float = 24.0

    def fetch_change(self, key: str) -> Optional[tuple[float, datetime, str]]:
        if self.fetcher is None:
            return None
        try:
            got = self.fetcher(key, self.fetch_hours)
        except Exception:
            return None
        if not got:
            return None
        change, as_of, source = got
        if change is None:
            return None
        return change, as_of, source or "unknown"


def fulfill_requests(requests: Sequence[str], state: AcquireState,
                     now: Optional[datetime] = None) -> list[tuple[str, str]]:
    """Turn each request into a (label, block) follow-up. Order-preserving."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in requests:
        req = (raw or "").strip()
        key = req.lower()
        if not req or key in seen:
            continue
        seen.add(key)
        if ("tick" in key or "last " in key and "sec" in key or "30" in key):
            if state.tick_ring is not None:
                out.append((req, state.tick_ring.render_seconds(30)))
            else:
                out.append((req, _absent("tick buffer")))
        elif "m1" in key or "close-up" in key or "tighter" in key:
            if state.tick_ring is not None:
                out.append((req, state.tick_ring.render_m1()))
            else:
                out.append((req, _absent("M1/close-up source")))
        elif "dxy" in key or "dollar" in key:
            out.append((req, _cross(state.fetch_change("DXY"))))
        elif "yield" in key or "2y" in key or "10y" in key:
            got = state.fetch_change("US10Y") or state.fetch_change("REAL10Y")
            out.append((req, _cross(got)))
        elif "gc" in key or "comex" in key or "volume" in key or "futures" in key:
            got = state.fetch_change("GC")
            out.append((req, _cross(got, note="volume/level data needs the COMEX seam")))
        else:
            out.append((req, _absent("request not in the fulfillable vocabulary")))
    return out


def render_follow_up(blocks: Sequence[tuple[str, str]]) -> str:
    if not blocks:
        return ""
    lines = [_BLOCK_TITLE]
    for label, text in blocks:
        lines.append(f"  {label}: {text}")
    return "\n".join(lines)


def _absent(what: str) -> str:
    return f"UNAVAILABLE — {what} is not wired. Nothing invented."


def _cross(got: Optional[tuple[float, datetime, str]], note: str = "") -> str:
    if got is None:
        return _absent("the requested cross-market refresh") + (" " + note if note else "")
    change, as_of, source = got
    if math.isnan(change):
        return _absent("the requested cross-market refresh (obs NaN)") + (
            " " + note if note else "")
    return (f"{source.upper()} {as_of:%H:%M:%S}UtC change "
            f"{change * 100:+.2f}%")