"""The numbers behind the labels, given to the analyst as well as the labels.

THE COMPRESSION THAT WAS COSTING INFORMATION. The brief hands the intelligent
layer a set of categories:

    TREND=UP  HEALTH=STRONG  VOL=NORMAL  PULLBACK=MEDIUM

Those are good categories and they are not the problem. The problem is that they
are ALL the analyst gets, so two states with the same four labels arrive
identical when they are economically nothing alike:

    +3.2 ATR impulse, 41% retracement, efficiency 0.71, range expanding
    +1.6 ATR impulse, 60% retracement, efficiency 0.28, range compressing

Both are UP / STRONG / NORMAL / MEDIUM. The desk computed every one of those
numbers on its way to producing the four words, then threw them away at the
boundary where the reasoning happens. That is the compression: not that the
labels are wrong, but that they are lossy and the loss is total.

WHAT THIS ADDS AND WHAT IT DELIBERATELY DOES NOT

It adds the continuous measurements ALONGSIDE the semantic state. It does not
replace them: the labels are how the desk's own thresholds, gates and cohorts
are defined, and swapping them for raw numbers would break every grouping the
record is built on. Both layers, together, in one block.

IT HAS NO AUTHORITY. Nothing here gates, sizes, or refuses. It is evidence in
the same standing as every Context field: the model reasons over it, the
deterministic compiler still owns every price. And each field is recorded on the
SIGNAL row, so `ranker.py` can eventually ask the only question that matters
about any of them — does it predict realised R — instead of it being one more
number nobody grouped by.

EVERY FIELD IS OPTIONAL AND NONE MEANS UNMEASURED. Not zero. A retracement of
0.0 is a market at its extreme; a retracement of None is a market whose swing
structure could not be read, and those must never render the same way.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Any, Optional, Sequence

CONTINUOUS_VERSION = "cont-2026-08-29-a"

#: Bars in the efficiency window. Kaufman's ratio over 20 is the conventional
#: length and nothing here is tuned — a fitted window would be this desk
#: choosing its own answer and calling it a measurement.
EFFICIENCY_N = 20

#: Short and long windows for the realised-volatility z. Five bars is "now" on
#: M15; fifty is a bit over half a session, long enough to have a mean worth
#: comparing against and short enough to still be this regime.
VOL_SHORT, VOL_LONG = 5, 50


def _f(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


@dataclass(frozen=True)
class Continuous:
    """Raw measurements, in units that mean something across regimes.

    Distances are in ATR rather than in dollars on purpose: "18 points from the
    session high" means something different at gold 2000 in a quiet Asian
    session than at gold 4700 in an expansion, and the analyst cannot know which
    it is looking at from the dollar figure alone.
    """
    impulse_atr: Optional[float] = None
    retracement: Optional[float] = None
    efficiency: Optional[float] = None
    vol_z: Optional[float] = None
    range_vs_mean: Optional[float] = None
    atr_pct: Optional[float] = None
    dist_session_high_atr: Optional[float] = None
    dist_session_low_atr: Optional[float] = None
    body_atr: Optional[float] = None
    close_loc: Optional[float] = None
    bars_since_swing_high: Optional[int] = None
    bars_since_swing_low: Optional[int] = None
    session_window: str = ""
    session_basis: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v is not None and v != ""}
        d["version"] = CONTINUOUS_VERSION
        return d

    def render(self) -> str:
        """One line per measurement, with UNMEASURED spelled out.

        A field that could not be computed is PRINTED as UNMEASURED rather than
        omitted. An omitted line is invisible; the analyst cannot tell a
        measurement of zero from a measurement that was never taken, and this
        desk's most repeated defect is exactly that confusion.
        """
        rows = [
            ("impulse_atr", self.impulse_atr, "the current leg's size in ATR"),
            ("retracement", self.retracement, "0 = at the extreme, 1 = fully retraced"),
            ("efficiency", self.efficiency, "0 = pure chop, 1 = a straight line"),
            ("vol_z", self.vol_z, "recent bar range vs its own longer mean, in sd"),
            ("range_vs_mean", self.range_vs_mean, "this bar's range vs trailing mean"),
            ("atr_pct", self.atr_pct, "ATR as a percent of price"),
            ("dist_session_high_atr", self.dist_session_high_atr, "in ATR"),
            ("dist_session_low_atr", self.dist_session_low_atr, "in ATR"),
            ("body_atr", self.body_atr, "this bar's body in ATR"),
            ("close_loc", self.close_loc, "0 = closed on the low, 1 = on the high"),
            ("bars_since_swing_high", self.bars_since_swing_high, ""),
            ("bars_since_swing_low", self.bars_since_swing_low, ""),
        ]
        out = []
        for name, v, why in rows:
            val = "UNMEASURED" if v is None else (
                f"{v:.3f}" if isinstance(v, float) else str(v))
            out.append(f"  {name.upper():<26} {val:<12} {why}")
        if self.session_window:
            out.append(f"  {'SESSION_WINDOW':<26} {self.session_window:<12} "
                       f"the clock window the session extremes were measured over"
                       + ("" if self.session_basis == "session"
                          else f" (BASIS {self.session_basis} — DEGRADED)"))
        return "\n".join(out)


def _efficiency(closes: Sequence[float]) -> Optional[float]:
    """Kaufman's ratio: net travel over gross travel. 0 chop, 1 straight line.

    The number the labels lose most completely. TREND=UP/HEALTH=STRONG is
    assigned from swing sequence and says nothing about whether the market got
    there in a line or in a fight, and those are different trades.
    """
    if len(closes) < 3:
        return None
    gross = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
    if gross <= 0:
        return None
    return round(abs(closes[-1] - closes[0]) / gross, 4)


def _vol_z(ranges: Sequence[float]) -> Optional[float]:
    if len(ranges) < VOL_LONG:
        return None
    recent = statistics.fmean(ranges[-VOL_SHORT:])
    base = ranges[-VOL_LONG:]
    mu = statistics.fmean(base)
    sd = statistics.stdev(base) if len(base) > 1 else 0.0
    if sd <= 0:
        return None
    return round((recent - mu) / sd, 3)


def measure(bars: Sequence[Any], i: int, st: Any,
            session_high: Optional[float] = None,
            session_low: Optional[float] = None) -> Continuous:
    """Every continuous measurement available at bar i. Pure, and never raises.

    Never raises is load-bearing: this is evidence attached to a brief, and a
    brief that fails to build because an optional enrichment threw would cost a
    signal. Anything that cannot be computed is None, and None renders as
    UNMEASURED.
    """
    if not bars or i < 0 or i >= len(bars):
        return Continuous()
    b = bars[i]
    atr = _f(getattr(st, "atr", None)) or 0.0
    out: dict[str, Any] = {}

    sh = getattr(st, "swing_high", None)
    sl = getattr(st, "swing_low", None)
    hi = _f(getattr(sh, "price", None))
    lo = _f(getattr(sl, "price", None))
    close = _f(getattr(b, "close", None))

    if hi is not None and lo is not None and atr > 0:
        out["impulse_atr"] = round(abs(hi - lo) / atr, 3)
    if hi is not None and lo is not None and close is not None and hi > lo:
        span = hi - lo
        direction = getattr(st, "trend_direction", "NONE")
        if direction == "UP":
            out["retracement"] = round(max(0.0, min(1.0, (hi - close) / span)), 4)
        elif direction == "DOWN":
            out["retracement"] = round(max(0.0, min(1.0, (close - lo) / span)), 4)

    closes = [c for c in (_f(getattr(x, "close", None))
                          for x in bars[max(0, i - EFFICIENCY_N + 1):i + 1])
              if c is not None]
    out["efficiency"] = _efficiency(closes)

    ranges: list[float] = []
    for x in bars[max(0, i - VOL_LONG + 1):i + 1]:
        h, lw = _f(getattr(x, "high", None)), _f(getattr(x, "low", None))
        if h is not None and lw is not None:
            ranges.append(h - lw)
    out["vol_z"] = _vol_z(ranges)
    if ranges and len(ranges) > 1:
        mean_r = statistics.fmean(ranges[:-1])
        if mean_r > 0:
            out["range_vs_mean"] = round(ranges[-1] / mean_r, 3)

    if close and atr > 0:
        out["atr_pct"] = round(100.0 * atr / close, 4)
    if close is not None and atr > 0:
        if session_high is not None:
            out["dist_session_high_atr"] = round((session_high - close) / atr, 3)
        if session_low is not None:
            out["dist_session_low_atr"] = round((close - session_low) / atr, 3)

    op = _f(getattr(b, "open", None))
    if op is not None and close is not None and atr > 0:
        out["body_atr"] = round(abs(close - op) / atr, 3)
    cl = getattr(b, "close_loc", None)
    if isinstance(cl, (int, float)) and not isinstance(cl, bool):
        out["close_loc"] = round(float(cl), 3)

    for key, sw in (("bars_since_swing_high", sh), ("bars_since_swing_low", sl)):
        idx = getattr(sw, "idx", None)
        if isinstance(idx, int):
            out[key] = i - idx

    out["session_window"] = str(getattr(st, "session_window", "") or "")
    out["session_basis"] = str(getattr(st, "session_basis", "") or "")
    return Continuous(**out)
