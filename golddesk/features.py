"""Deterministic structure engine — real swings, ATR, displacement, regime.

This is the layer that must be right before any intelligence is worth adding.
It computes, from closed bars only, everything the analyst brief needs.

NO LOOKAHEAD. A fractal swing at index i is only knowable at i+right, so every
Swing carries confirmed_idx and analyse_at(i) refuses to use one whose
confirmation lies in the future. This mirrors the discipline already in the
desk's structure.py and is asserted in the test at the bottom of runner.py.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Sequence
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def close_loc(self) -> float:
        """0.0 = closed on the low, 1.0 = closed on the high."""
        return 0.5 if self.range <= 0 else (self.close - self.low) / self.range


@dataclass(frozen=True)
class Swing:
    idx: int
    confirmed_idx: int          # only knowable from here on
    price: float
    kind: Literal["HIGH", "LOW"]


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def aggregate(bars: Sequence[Bar], factor: int, *,
              align: bool = True) -> list[Bar]:
    """Build true higher-timeframe candles from a finer series.

    SAMPLING IS NOT AGGREGATION. Taking every 16th M15 bar yields a series of
    fifteen-minute candles spaced four hours apart: its highs and lows are those
    of one M15 bar, not of the four-hour range. Handing that to a model labelled
    "H4" misrepresents both the volatility and the structure of the higher
    timeframe, and every swing, sweep and displacement read off it is wrong.

    A real H4 candle takes the FIRST open, the MAX high, the MIN low and the
    LAST close of its constituent bars.

    `align` snaps groups to wall-clock boundaries (an H4 candle starts at an
    hour divisible by four) rather than to an arbitrary offset from the start of
    the array, so the same bar always lands in the same higher-timeframe candle
    regardless of how much history happens to be loaded. Without that, the H4
    series silently changes shape as the window grows.

    The final group is INCOMPLETE by construction — it is the higher-timeframe
    candle still forming. It is returned, because refusing to show the current
    partial candle hides where price is right now, and callers that need only
    closed candles drop the last element.
    """
    if factor <= 1 or not bars:
        return list(bars)
    groups: list[list[Bar]] = []
    key = None
    for b in bars:
        if align:
            step_s = int((bars[1].ts - bars[0].ts).total_seconds()) if len(bars) > 1 else 60
            span = step_s * factor
            k = int(b.ts.timestamp()) // span
        else:
            k = len(groups) - 1 if groups and len(groups[-1]) < factor else len(groups)
        if k != key:
            groups.append([])
            key = k
        groups[-1].append(b)

    out: list[Bar] = []
    for g in groups:
        if not g:
            continue
        out.append(Bar(
            ts=g[0].ts, open=g[0].open,
            high=max(x.high for x in g), low=min(x.low for x in g),
            close=g[-1].close,
            volume=sum((x.volume or 0.0) for x in g),
            spread=g[-1].spread))
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> list[Optional[float]]:
    """Wilder ATR. Index-aligned; None until enough history exists."""
    out: list[Optional[float]] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs = []
    for i in range(1, len(bars)):
        prev = bars[i - 1].close
        trs.append(max(bars[i].high - bars[i].low,
                       abs(bars[i].high - prev), abs(bars[i].low - prev)))
    val = statistics.fmean(trs[:period])
    out[period] = val
    for i in range(period + 1, len(bars)):
        val = (val * (period - 1) + trs[i - 1]) / period
        out[i] = val
    return out


def swings(bars: Sequence[Bar], left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal swings, each stamped with the index that confirms it."""
    out: list[Swing] = []
    for i in range(left, len(bars) - right):
        window = bars[i - left:i + right + 1]
        if bars[i].high == max(b.high for b in window) and \
                all(bars[i].high > b.high for b in window if b is not bars[i]):
            out.append(Swing(i, i + right, bars[i].high, "HIGH"))
        if bars[i].low == min(b.low for b in window) and \
                all(bars[i].low < b.low for b in window if b is not bars[i]):
            out.append(Swing(i, i + right, bars[i].low, "LOW"))
    return out


def visible_swings(sw: Sequence[Swing], at_idx: int) -> list[Swing]:
    """Only swings whose confirmation has already happened. The lookahead gate."""
    return [s for s in sw if s.confirmed_idx <= at_idx]


# --------------------------------------------------------------------------
# Derived state
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StructureState:
    trend_direction: Literal["UP", "DOWN", "NONE"]
    trend_health: Literal["STRONG", "MODERATE", "WEAK"]
    trend_maturity: Literal["YOUNG", "MID", "MATURE", "EXHAUSTED"]
    volatility_state: Literal["LOW", "NORMAL", "ELEVATED", "EXTREME"]
    displacement_state: Literal["NONE", "FORMING", "CONFIRMED", "EXCEPTIONAL"]
    sweep_state: Literal["NONE", "CONFIRMED"]
    reclaim_state: Literal["NONE", "WEAK", "CONFIRMED"]
    pullback_depth: Literal["NONE", "SHALLOW", "MEDIUM", "DEEP"]
    distance_from_session_extreme: Literal["NEAR", "MID", "FAR"]
    atr: float
    swing_high: Optional[Swing]
    swing_low: Optional[Swing]
    prior_swing_high: Optional[Swing]
    prior_swing_low: Optional[Swing]
    trigger_price: Optional[float]
    legs_in_trend: int


# Displacement thresholds match the desk's production config.
DISP_BODY_ATR = 0.9
DISP_RANGE_ATR = 1.0
DISP_CLOSE_LOC = 0.66


def classify(bars: Sequence[Bar], i: int, sw: Sequence[Swing],
             atrs: Sequence[Optional[float]]) -> Optional[StructureState]:
    """Full deterministic state at bar i, using only information available then."""
    a = atrs[i]
    if a is None or a <= 0 or i < 30:
        return None

    vis = visible_swings(sw, i)
    highs = [s for s in vis if s.kind == "HIGH"]
    lows = [s for s in vis if s.kind == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    sh, psh = highs[-1], highs[-2]
    sl, psl = lows[-1], lows[-2]

    # --- direction from swing sequence
    hh, hl = sh.price > psh.price, sl.price > psl.price
    lh, ll = sh.price < psh.price, sl.price < psl.price
    if hh and hl:
        direction = "UP"
    elif lh and ll:
        direction = "DOWN"
    else:
        direction = "NONE"

    # --- health: how cleanly the structure is holding, in ATR units
    if direction == "UP":
        impulse = (sh.price - psl.price) / a
        retrace = (sh.price - min(b.low for b in bars[sh.idx:i + 1])) / max(sh.price - psl.price, 1e-9)
    elif direction == "DOWN":
        impulse = (psh.price - sl.price) / a
        retrace = (max(b.high for b in bars[sl.idx:i + 1]) - sl.price) / max(psh.price - sl.price, 1e-9)
    else:
        impulse, retrace = 0.0, 1.0
    if direction != "NONE" and impulse >= 3.0 and retrace <= 0.5:
        health = "STRONG"
    elif direction != "NONE" and impulse >= 1.5 and retrace <= 0.786:
        health = "MODERATE"
    else:
        health = "WEAK"

    # --- maturity: how many confirmed legs the trend has printed
    legs = 0
    for s in reversed(vis):
        if direction == "UP" and s.kind == "LOW":
            legs += 1
        elif direction == "DOWN" and s.kind == "HIGH":
            legs += 1
        if legs >= 6:
            break
    maturity = ("YOUNG" if legs <= 1 else "MID" if legs <= 3
                else "MATURE" if legs <= 5 else "EXHAUSTED")

    # --- volatility percentile against its own trailing distribution
    hist = [x for x in atrs[max(0, i - 250):i + 1] if x is not None]
    pct = sum(x <= a for x in hist) / len(hist) if hist else 0.5
    vol = ("LOW" if pct < 0.25 else "NORMAL" if pct < 0.70
           else "ELEVATED" if pct < 0.92 else "EXTREME")

    # --- displacement on the just-closed bar
    b = bars[i]
    if b.body >= DISP_BODY_ATR * a and b.range >= DISP_RANGE_ATR * a:
        loc_ok = (b.close_loc >= DISP_CLOSE_LOC) if b.close > b.open \
            else (b.close_loc <= 1 - DISP_CLOSE_LOC)
        disp = ("EXCEPTIONAL" if (loc_ok and b.body >= 1.6 * a)
                else "CONFIRMED" if loc_ok else "FORMING")
    else:
        disp = "NONE"

    # --- sweep + reclaim against the most recent opposing swing
    sweep = "NONE"
    reclaim = "NONE"
    trigger = None
    if direction in ("UP", "NONE") and b.low < sl.price <= b.close:
        sweep, reclaim, trigger = "CONFIRMED", "CONFIRMED", sl.price
    elif direction in ("DOWN", "NONE") and b.high > sh.price >= b.close:
        sweep, reclaim, trigger = "CONFIRMED", "CONFIRMED", sh.price
    elif b.low < sl.price:
        sweep = "CONFIRMED"
        reclaim = "WEAK"
    elif b.high > sh.price:
        sweep = "CONFIRMED"
        reclaim = "WEAK"
    if trigger is None and disp in ("CONFIRMED", "EXCEPTIONAL"):
        trigger = b.open        # displacement origin

    # --- pullback depth from the active impulse
    if direction == "UP":
        depth = (sh.price - b.close) / max(sh.price - psl.price, 1e-9)
    elif direction == "DOWN":
        depth = (b.close - sl.price) / max(psh.price - sl.price, 1e-9)
    else:
        depth = 0.0
    pull = ("NONE" if depth <= 0.05 else "SHALLOW" if depth <= 0.382
            else "MEDIUM" if depth <= 0.618 else "DEEP")

    # --- distance from the session's own extremes
    day = session_window(bars, i)
    d_hi, d_lo = max(x.high for x in day), min(x.low for x in day)
    span = max(d_hi - d_lo, 1e-9)
    near = min(abs(b.close - d_hi), abs(b.close - d_lo)) / span
    dist = "NEAR" if near <= 0.15 else "MID" if near <= 0.4 else "FAR"

    return StructureState(direction, health, maturity, vol, disp, sweep, reclaim,
                          pull, dist, a, sh, sl, psh, psl, trigger, legs)


def session_of(ts: datetime) -> str:
    """Market session using civil clocks, including London/NY DST.

    Fixed UTC cut-offs are wrong for half the year and especially wrong during
    the two DST-mismatch weeks. Gold's trading-day rollover follows New York;
    London and New York opens follow their own local clocks.
    """
    london = ts.astimezone(ZoneInfo("Europe/London"))
    ny = ts.astimezone(ZoneInfo("America/New_York"))
    lh = london.hour + london.minute / 60
    nh = ny.hour + ny.minute / 60
    if 16 <= nh < 17:
        return "ROLLOVER"
    if nh >= 17 or lh < 8:
        return "ASIA"
    if nh < 8:
        return "LONDON"
    if lh < 16:
        return "OVERLAP"
    return "NY"


def session_window(bars: Sequence[Bar], i: int) -> list[Bar]:
    """Closed bars belonging to the current contiguous market session."""
    label = session_of(bars[i].ts)
    start = i
    while start > 0 and session_of(bars[start - 1].ts) == label:
        start -= 1
    return list(bars[start:i + 1])


def trading_day_key(ts: datetime):
    """New-York rollover trading date (17:00 belongs to the next day)."""
    ny = ts.astimezone(ZoneInfo("America/New_York"))
    return (ny.date() + timedelta(days=1)) if ny.hour >= 17 else ny.date()


def prior_trading_day_window(bars: Sequence[Bar], i: int) -> list[Bar]:
    current = trading_day_key(bars[i].ts)
    prior_keys = [trading_day_key(b.ts) for b in bars[:i + 1]
                  if trading_day_key(b.ts) < current]
    if not prior_keys:
        return []
    prior = max(prior_keys)
    return [b for b in bars[:i + 1] if trading_day_key(b.ts) == prior]
