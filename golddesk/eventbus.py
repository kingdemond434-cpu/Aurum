"""The tick/event bus (A-share AI Analyst style).

Gold events become first-class messages: a tick impulse, a spread burst, a
level touch, a volatility shift, a macro release, a headline. The bus is the
cheap always-on sensing layer; it records every arrival and answers the one
question the expensive layer cares about — "has anything decision-relevant
happened since the last analysis?" — without running a model.

Events are deterministic facts with timestamps. Nothing here composes the
brief; the router decides what an event is worth, and events can only force a
wake, never a direction.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class BusEventKind(str, Enum):
    TICK = "TICK"                       # every quote — the eyes, not the signal
    PRICE_IMPULSE = "PRICE_IMPULSE"     # net move beyond the impulse threshold
    SPREAD_BURST = "SPREAD_BURST"       # spread beyond its own moving mean
    LEVEL_TOUCH = "LEVEL_TOUCH"         # mid crossed a near confirmed level
    VOLATILITY_SHIFT = "VOLATILITY_SHIFT"
    MACRO_RELEASE = "MACRO_RELEASE"
    DXY_SHOCK = "DXY_SHOCK"
    YIELD_SHOCK = "YIELD_SHOCK"
    GC_BASIS_SHIFT = "GC_BASIS_SHIFT"
    HEADLINE = "HEADLINE"


#: kinds strong enough to force an idle state up to a real analysis
_FORCING = {BusEventKind.PRICE_IMPULSE, BusEventKind.SPREAD_BURST,
            BusEventKind.LEVEL_TOUCH, BusEventKind.VOLATILITY_SHIFT,
            BusEventKind.MACRO_RELEASE, BusEventKind.DXY_SHOCK,
            BusEventKind.YIELD_SHOCK, BusEventKind.GC_BASIS_SHIFT,
            BusEventKind.HEADLINE}


@dataclass
class BusEvent:
    kind: BusEventKind
    ts: datetime
    detail: str = ""


@dataclass
class EventBus:
    maxlen: int = 600
    impulse_at: float = 0.0             # absolute price move that counts as impulse
    spread_mult: float = 2.0            # spread > mult * its own mean = burst
    events: deque = field(default_factory=deque)
    _prices: deque = field(default_factory=deque)          # (ts, mid)
    _spreads: deque = field(default_factory=deque)

    def emit(self, kind: BusEventKind, ts: datetime, detail: str = "") -> None:
        self.events.append(BusEvent(kind, ts, detail))
        if len(self.events) > self.maxlen:
            self.events.popleft()

    def emit_tick(self, price: float, ts: datetime,
                  bid: Optional[float] = None,
                  ask: Optional[float] = None,
                  levels: list[float] = ()) -> None:
        """Observations first, events second, so the signal is always derived."""
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else price
        self._prices.append((ts, mid))
        if len(self._prices) > 240:
            self._prices.popleft()
        if bid is not None and ask is not None:
            self._spreads.append((ts, ask - bid))
            if len(self._spreads) > 240:
                self._spreads.popleft()
        if not self._prices or len(self._prices) < 2:
            return

        lo, hi = min(p for _, p in self._prices), max(p for _, p in self._prices)
        if self.impulse_at > 0 and (hi - lo) >= self.impulse_at:
            self.emit(BusEventKind.PRICE_IMPULSE, ts,
                      f"{hi - lo:.3f} in {len(self._prices)} quotes")
            self._prices.clear()
        if len(self._spreads) >= 20:
            mean = statistics.fmean(s for _, s in self._spreads)
            cur = self._spreads[-1][1] if self._spreads else 0.0
            if mean > 0 and cur > self.spread_mult * mean:
                self.emit(BusEventKind.SPREAD_BURST, ts,
                          f"spread {cur:.3f} vs mean {mean:.3f}")
        for lv in levels:
            if abs(mid - lv) <= max(0.2, 0.25 * mean_spread_guess(self._spreads)):
                self.emit(BusEventKind.LEVEL_TOUCH, ts,
                          f"mid {mid:.2f} at level {lv:.2f}")
                break

    def kinds_since(self, t0: datetime) -> list[BusEvent]:
        return [e for e in self.events if e.ts > t0]

    def wake_worthy(self, t0: datetime) -> bool:
        return any(e.kind in _FORCING for e in self.kinds_since(t0))


def mean_spread_guess(spreads: deque) -> float:
    if not spreads:
        return 0.5
    return statistics.fmean(s for _, s in spreads)