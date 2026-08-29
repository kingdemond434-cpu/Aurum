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
    #: WHICH window `distance_from_session_extreme` was measured over, and how
    #: that window was derived. These exist because the field above used to be
    #: computed from `bars[i-24:i+1]` and called "the session's own extremes" --
    #: six hours on M15, a whole day on H1, five weeks on D1, and aligned with
    #: no real session on any of them. A label naming one quantity and measuring
    #: another is worse than a missing one, so the label now travels with its
    #: basis and the degrade is visible wherever the state is rendered.
    #:
    #:   session   a real clock window: NY / LONDON / ASIA / DAY
    #:   bars-24   the old rolling bar count, used ONLY when no bar carried a
    #:             usable timestamp inside the window -- and it says so
    session_window: str = "bars-24"
    session_basis: str = "bars-24"


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
    #
    # THE WINDOW IS A CLOCK WINDOW NOW, not `bars[i-24:i+1]`. That slice was 6
    # hours on the live M15 path, a whole day on H1 and five trading weeks on
    # D1, and it aligned with no session's open or close on any timeframe -- the
    # analyst was told "session", reasoned about a session, and was shown a
    # rolling bar count. See sessions.py for the full account.
    #
    # The bar count survives as a FALLBACK for one case only: bars whose
    # timestamps put nothing inside the window (a synthetic fixture, a feed
    # hole, a desk started mid-session). It is labelled `bars-24` on the state
    # when that happens, so a degraded measurement is never mistaken for the
    # real one.
    d_hi = d_lo = None
    win_name, basis = "bars-24", "bars-24"
    try:
        from .sessions import current_window, extremes as _extremes
        w = current_window(b.ts)
        ex = _extremes(bars, w, upto=i)
        if ex is not None:
            d_hi, d_lo, win_name, basis = ex.high, ex.low, w.name, "session"
    except Exception:                                            # noqa: BLE001
        d_hi = d_lo = None
    if d_hi is None or d_lo is None:
        day = bars[max(0, i - 24):i + 1]
        d_hi, d_lo = max(x.high for x in day), min(x.low for x in day)
    span = max(d_hi - d_lo, 1e-9)
    near = min(abs(b.close - d_hi), abs(b.close - d_lo)) / span
    dist = "NEAR" if near <= 0.15 else "MID" if near <= 0.4 else "FAR"

    return StructureState(direction, health, maturity, vol, disp, sweep, reclaim,
                          pull, dist, a, sh, sl, psh, psl, trigger, legs,
                          win_name, basis)


def session_of(ts: datetime) -> str:
    """UTC -> session, through the tz database. Same vocabulary as before.

    THIS USED TO BE FIXED UTC HOUR BUCKETS, and they were wrong for most of the
    year. London opens at 08:00 LOCAL — 08:00 UTC in winter, 07:00 in summer —
    and New York at 13:30 UTC in summer, 14:30 in winter. The buckets said
    LONDON began at 06:00 UTC in every month, so the boundary was an hour or two
    out for the roughly eight months either side of the changeovers, and the
    OVERLAP band — the highest-liquidity span of the gold day — was displaced
    with it.

    The desk's own economic calendar already resolves New York event times
    through zoneinfo because DST matters there. Both statements could not be
    right, and it was the calendar that was right.

    Kept as a thin delegate rather than deleted: it is imported by name in
    several places, and a module-level rename is a worse diff than a two-line
    function that says where the arithmetic went.
    """
    from .sessions import session_of as _session_of
    return _session_of(ts)
