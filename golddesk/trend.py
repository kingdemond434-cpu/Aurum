"""Quant's trend detector, ported for gold, feeding the analyst brief.

WHAT WAS MEASURED BEFORE THIS WAS PORTED

The quant desk built and validated this across 22 instruments and eight years.
Forward 24-bar move in the detected direction, in ATRs, is monotone in strength:
+0.024 / +0.073 / +0.136 for strength 0.3-0.5 / 0.5-0.7 / 0.7-1.0. Those carry
raw t of 3.6 / 10.7 / 8.6, but they are overlapping horizons sampled every bar,
so the honest figures after dividing by sqrt(24) are ~0.7 / 2.2 / 1.8. Real,
monotone, modest. It is not a signal generator and it is not wired as one here.

WHY IT COMES IN AS CONTEXT AND NOT AS A RULE

An imported detector is evidence about the universe it was measured on. Aurum
trades one symbol; asserting a cross-instrument result about XAUUSD because the
same code produced it is precisely the cargo-culting absorb.py exists to
prevent. So `strength` and `direction` enter the MarketBrief as MEASURED
CONTEXT -- facts the analyst may weigh -- and nothing downstream gates on them
until Aurum's own ledger has confirmed them on Aurum's own data.

NOTHING HERE KNOWS WHAT A POINT IS

Every quantity is a ratio: a move in ATRs, a range against its own trailing
median, a count against its own total. Multiply every input price by three and
the output is unchanged. That is what lets one detector serve gold at 1,800 and
gold at 4,500, a quiet Tuesday and an FOMC afternoon, in each one's own units --
and it is why small trend days are not thrown away as chop.

SYMMETRY IS TESTED, NOT INTENDED

`strength` is direction-agnostic; `direction` carries the sign. The suite
mirrors the series and requires strength identical and direction flipped. A
detector that quietly works better on rallies is the most expensive available
bug on an instrument that falls faster than it rises, and it never shows up in
aggregate returns.

`dying` IS MEASURED AGAINST THE DIRECTION THAT WAS IN FORCE

Not the current one. When a long trend rolls into a clean short trend, strength
stays high the whole way -- both halves are trends -- so comparing to the
current direction sees nothing wrong at the exact moment a runner must be
banked. That was a real defect in the first implementation and it is pinned by
a test here.

IT IS EXPORTED BUT NOT WIRED TO EXITS, DELIBERATELY. On quant's book, banking a
position on `dying` lost on 0 of 22 instruments, mean t -21.96 -- the trail
already exits a dying trend and does it better. It is here for the analyst to
read, not for the engine to act on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = ["TrendGauge", "efficiency_ratio", "gauge_from_bars"]


def _atr(high: Sequence[float], low: Sequence[float], close: Sequence[float],
         n: int = 14) -> Optional[float]:
    """Simple-averaged true range over the last n bars, or None."""
    if len(close) < n + 1:
        return None
    trs = []
    for i in range(len(close) - n, len(close)):
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]),
                 abs(low[i] - close[i - 1]))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def efficiency_ratio(close: Sequence[float], n: int) -> Optional[float]:
    """Kaufman's ratio: net distance over path length, in [0, 1].

    1.0 is a straight line, 0.0 a round trip. The cleanest available statement
    of trend-or-chop precisely because it is a ratio of two lengths and so has
    no units to be wrong about.
    """
    if len(close) < n + 1:
        return None
    path = sum(abs(close[i] - close[i - 1])
               for i in range(len(close) - n, len(close)))
    if path <= 0:
        return 0.0
    return abs(close[-1] - close[-1 - n]) / path


@dataclass(frozen=True)
class TrendGauge:
    """What the detector says right now. Every field is dimensionless."""
    strength: float            # 0..1, direction-agnostic
    direction: int             # -1 / 0 / +1
    dying: bool
    er: float                  # efficiency ratio
    expansion: float           # ATR against its own trailing median
    displacement: float        # |net move| in ATRs
    persistence: float         # bar agreement with the net direction

    #: Strength below which calling something a trend stops meaning anything.
    #: Not a prediction threshold — a naming one.
    FLOOR = 0.35

    @property
    def label(self) -> str:
        if self.direction == 0:
            return "CHOP"
        side = "UP" if self.direction > 0 else "DOWN"
        band = ("EXTREME" if self.strength >= 0.7 else
                "STRONG" if self.strength >= 0.55 else "MODEST")
        return f"{band}_{side}" + ("_DYING" if self.dying else "")

    def render(self) -> str:
        """One block for the brief. Reports the measured effect size too.

        The analyst is told what this is worth, not just what it says. A gauge
        quoted without its effect size invites the reader to treat 0.8 as a
        conviction multiplier, and the measured forward edge at that level is
        about a seventh of an ATR.
        """
        exp = ("+0.14 ATR / 24 bars (deflated t ~1.8)" if self.strength >= 0.7
               else "+0.07 ATR / 24 bars (deflated t ~2.2)" if self.strength >= 0.5
               else "+0.02 ATR / 24 bars (deflated t ~0.7)" if self.strength >= 0.3
               else "no measured forward edge")
        return "\n".join([
            f"  TREND_GAUGE                    {self.label}",
            f"  TREND_STRENGTH                 {self.strength:.2f}  (0..1)",
            f"  TREND_EFFICIENCY_RATIO         {self.er:.2f}",
            f"  TREND_RANGE_EXPANSION          {self.expansion:.2f}x own median",
            f"  TREND_DISPLACEMENT             {self.displacement:.2f} ATR",
            f"  TREND_DYING                    {self.dying}",
            f"  TREND_MEASURED_EDGE            {exp}",
            f"  TREND_PROVENANCE               quant/22 instruments, 8y — "
            f"NOT yet confirmed on XAUUSD's own ledger",
        ])


def gauge_from_bars(high: Sequence[float], low: Sequence[float],
                    close: Sequence[float], *, n: int = 12, atr_n: int = 14,
                    regime_n: int = 240, decay: float = 0.6,
                    shock_k: float = 1.0,
                    prior_direction: int = 0) -> Optional[TrendGauge]:
    """Score the trend at the LAST bar, using only bars at or before it.

    `prior_direction` is the direction that was in force before this bar, and
    it is what `dying` is measured against — see the module docstring. Callers
    that do not track it get the degraded-but-honest behaviour of comparing
    against the current direction, which cannot see a reversal.
    """
    if len(close) < max(n, atr_n) + 2:
        return None
    a = _atr(high, low, close, atr_n)
    if a is None or a <= 0:
        return None
    er = efficiency_ratio(close, n)
    if er is None:
        return None

    # Range expansion against a TRAILING median of ATR — rolling, never
    # full-sample. A median over all history is a number from the future
    # wearing the clothes of a constant.
    hist = []
    for end in range(max(atr_n + 1, len(close) - regime_n), len(close) + 1):
        v = _atr(high[:end], low[:end], close[:end], atr_n)
        if v is not None and v > 0:
            hist.append(v)
    med = sorted(hist)[len(hist) // 2] if hist else a
    expansion = a / med if med > 0 else 1.0

    net = close[-1] - close[-1 - n]
    displacement = abs(net) / a
    sgn = (net > 0) - (net < 0)

    ups = sum(1 for i in range(len(close) - n, len(close))
              if close[i] > close[i - 1])
    frac = (ups / n) if sgn > 0 else (1.0 - ups / n) if sgn < 0 else 0.5
    persistence = max(0.0, 2.0 * (frac - 0.5))

    def squash(x: float, cap: float) -> float:
        return min(1.0, max(0.0, x / cap))

    # Unweighted mean. Fitting four weights on the sample the result is read
    # from is how a detector scores well once and never again.
    strength = (squash(er, 1.0) + squash(expansion, 2.0)
                + squash(displacement, 3.0) + squash(persistence, 1.0)) / 4.0
    direction = sgn if strength >= TrendGauge.FLOOR else 0

    # `dying` here detects TWO of the three deaths quant's version knows: an
    # adverse bar of shock_k ATRs against the direction that was in force, and
    # an outright flip of that direction. The third -- strength fading to a
    # fraction of its own recent PEAK -- needs a series of strengths and this
    # function only ever sees one bar. Recomputing strength for the last n bars
    # would repeat the trailing-median scan n times per call for a component
    # that is the weakest of the three, so it is omitted rather than faked.
    #
    # Saying so matters: a caller that reads `dying is False` here must not
    # conclude the trend is healthy, only that it has neither flipped nor taken
    # a shock. `decay` is accepted and unused for exactly that reason and is
    # kept in the signature so the series-level version stays call-compatible.
    held = prior_direction or direction
    bar = close[-1] - close[-2]
    adverse = bool(held) and (-held * bar / a) >= shock_k
    flipped = bool(held and direction and direction != held)
    dying = bool((adverse or flipped) and held != 0)

    return TrendGauge(strength=round(strength, 4), direction=int(direction),
                      dying=dying, er=round(er, 4),
                      expansion=round(expansion, 4),
                      displacement=round(displacement, 4),
                      persistence=round(persistence, 4))
