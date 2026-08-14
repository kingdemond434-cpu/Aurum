"""Event-driven watcher — every bar observed locally, AI woken only when useful.

The desk sees everything. The model is asked only when the deterministic state
changed in a way that could change a decision, plus a heartbeat so an imperfect
trigger set cannot blind the analyst indefinitely.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .features import StructureState


class Event(str, Enum):
    BAR_CLOSE = "BAR_CLOSE"
    SWING_CONFIRMED = "SWING_CONFIRMED"
    SWEEP = "SWEEP"
    RECLAIM = "RECLAIM"
    DISPLACEMENT = "DISPLACEMENT"
    TREND_FLIP = "TREND_FLIP"
    HEALTH_CHANGE = "HEALTH_CHANGE"
    VOLATILITY_SHIFT = "VOLATILITY_SHIFT"
    SESSION_CHANGE = "SESSION_CHANGE"
    HEARTBEAT = "HEARTBEAT"


# Which events are worth paying a model call for. BAR_CLOSE alone is not.
WAKING = {Event.SWEEP, Event.RECLAIM, Event.DISPLACEMENT, Event.TREND_FLIP,
          Event.HEALTH_CHANGE, Event.SESSION_CHANGE, Event.HEARTBEAT}


@dataclass
class WatchResult:
    events: list[Event]
    wake: bool
    reason: str


class Watcher:
    """Holds the previous state and diffs against it. Pure local computation."""

    def __init__(self, heartbeat: timedelta = timedelta(minutes=30),
                 min_gap: timedelta = timedelta(0)):
        """min_gap defaults to ZERO — no throttle.

        A minimum gap between reads is a quota on thinking: it can silently
        drop a genuine opportunity that arrives two minutes after the last one.
        It remains configurable purely as an INFERENCE COST control for anyone
        who needs one, and it is never a selectivity device. Set it only if the
        API bill demands it, and record that you did, because it will show up
        in the missed-opportunity ledger.
        """
        self.prev: Optional[StructureState] = None
        self.prev_session: Optional[str] = None
        self.heartbeat = heartbeat
        self.min_gap = min_gap
        self.last_wake: Optional[datetime] = None

    def observe(self, st: StructureState, session: str, ts: datetime) -> WatchResult:
        ev: list[Event] = [Event.BAR_CLOSE]
        p = self.prev

        if st.sweep_state == "CONFIRMED" and (p is None or p.sweep_state != "CONFIRMED"):
            ev.append(Event.SWEEP)
        if st.reclaim_state == "CONFIRMED" and (p is None or p.reclaim_state != "CONFIRMED"):
            ev.append(Event.RECLAIM)
        if st.displacement_state in ("CONFIRMED", "EXCEPTIONAL") and \
                (p is None or p.displacement_state not in ("CONFIRMED", "EXCEPTIONAL")):
            ev.append(Event.DISPLACEMENT)
        if p and st.trend_direction != p.trend_direction:
            ev.append(Event.TREND_FLIP)
        if p and st.trend_health != p.trend_health:
            ev.append(Event.HEALTH_CHANGE)
        if p and st.volatility_state != p.volatility_state:
            ev.append(Event.VOLATILITY_SHIFT)
        if self.prev_session is not None and session != self.prev_session:
            ev.append(Event.SESSION_CHANGE)
        if p and (st.swing_high is not p.swing_high or st.swing_low is not p.swing_low):
            ev.append(Event.SWING_CONFIRMED)

        stale = self.last_wake is None or (ts - self.last_wake) >= self.heartbeat
        if stale:
            ev.append(Event.HEARTBEAT)

        self.prev, self.prev_session = st, session

        waking = [e for e in ev if e in WAKING]
        if not waking:
            return WatchResult(ev, False, "no decision-relevant change")
        if self.last_wake is not None and (ts - self.last_wake) < self.min_gap:
            return WatchResult(ev, False, f"throttled ({(ts - self.last_wake)} since last)")
        self.last_wake = ts
        return WatchResult(ev, True, "+".join(e.value for e in waking))
