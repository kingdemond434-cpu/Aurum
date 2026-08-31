"""Multi-timeframe alignment — the veto that stops an M15 entry fighting an H4 impulse.

WHAT WAS ALREADY HERE, AND WHAT WAS NOT

`live.py` already computes one higher timeframe: `aggregate(bars, htf_factor)` builds true H4
candles (first open, max high, min low, last close, wall-clock aligned) and `on_bar` receives an
`htf_state`. So the desk SEES the higher timeframe.

What it does not do is *rule on the relationship*. `htf_state` is one input among many in the
brief; nothing computes "this entry is counter to a confirmed H4 displacement" and says so in
those words. The distinction matters because a model reading two structure blocks can silently
weigh them however it likes, and a countertrend entry against a fresh higher-timeframe impulse is
not a slightly-worse trade — it is a different trade, with a different loss distribution.

THIS RULES, AND IT ONLY VETOES

`assess()` returns a verdict; it never proposes. The gradations exist because "aligned" and
"opposed" are not the only two states and collapsing them would make the veto either useless or
tyrannical:

    ALIGNED        entry direction agrees with every timeframe that has an opinion
    NEUTRAL        higher timeframes have no directional opinion — nothing to fight
    COUNTER_SOFT   opposed to a trend, but no confirmed displacement against the entry
    COUNTER_HARD   opposed to a CONFIRMED or EXCEPTIONAL displacement — the veto case

Only COUNTER_HARD is a refusal. COUNTER_SOFT is a warning that travels into the brief, because
mean reversion into a mature, weak trend is a legitimate trade and a rule that forbade it would
delete a whole family of setups the desk is supposed to take.

WHY MATURITY AND HEALTH CHANGE THE ANSWER

Opposing a YOUNG STRONG trend and opposing an EXHAUSTED WEAK one are opposite bets wearing the
same label. `StructureState` already carries `trend_maturity` and `trend_health`, so the severity
is read from them rather than from the direction alone. An exhausted trend downgrades a hard
counter to a soft one: that is precisely the reversal the desk wants to be able to take.

ABSENCE IS NEUTRAL, NOT ALIGNED

A timeframe whose state could not be computed (too few bars, warmup) contributes NOTHING. It is
never counted as agreement. Treating an unavailable timeframe as consent is how a veto quietly
stops vetoing during exactly the low-data conditions where it matters most.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional, Sequence

from .features import Bar, StructureState, aggregate, atr, classify, swings


class Alignment(str, Enum):
    ALIGNED = "ALIGNED"
    NEUTRAL = "NEUTRAL"
    COUNTER_SOFT = "COUNTER_SOFT"
    COUNTER_HARD = "COUNTER_HARD"


#: Displacement states that make an opposing timeframe a HARD counter. FORMING is deliberately
#: excluded: a displacement that has not confirmed is a candidate, and vetoing on candidates
#: would refuse most continuation entries at the moment they are cheapest.
_HARD_DISPLACEMENT = {"CONFIRMED", "EXCEPTIONAL"}

#: A trend this far along no longer earns a hard veto. Opposing an exhausted trend is the
#: reversal trade, not a mistake.
_SPENT_MATURITY = {"EXHAUSTED"}


@dataclass(frozen=True)
class TimeframeRead:
    """One timeframe's opinion, or the honest absence of one."""

    label: str
    state: Optional[StructureState]

    @property
    def direction(self) -> Literal["UP", "DOWN", "NONE"]:
        return "NONE" if self.state is None else self.state.trend_direction

    @property
    def has_opinion(self) -> bool:
        return self.state is not None and self.state.trend_direction != "NONE"


@dataclass(frozen=True)
class BiasAssessment:
    """The verdict, plus every read that produced it. A veto without its reasons is unauditable."""

    alignment: Alignment
    direction: Literal["BUY", "SELL"]
    reads: tuple[TimeframeRead, ...]
    why: str

    @property
    def vetoed(self) -> bool:
        """ONLY hard counters refuse. See the module docstring for why soft ones must not."""
        return self.alignment is Alignment.COUNTER_HARD

    def to_prompt(self) -> str:
        lines = ["[HIERARCHICAL BIAS]"]
        for r in self.reads:
            if r.state is None:
                lines.append(f"  {r.label}: UNAVAILABLE (not enough bars) — contributes nothing")
            else:
                lines.append(
                    f"  {r.label}: {r.state.trend_direction} "
                    f"{r.state.trend_health}/{r.state.trend_maturity}, "
                    f"displacement {r.state.displacement_state}")
        lines.append(f"  VERDICT: {self.alignment.value} — {self.why}")
        lines.append("[/HIERARCHICAL BIAS]")
        return "\n".join(lines)


def _opposes(direction: Literal["BUY", "SELL"], trend: str) -> bool:
    return (direction == "BUY" and trend == "DOWN") or (direction == "SELL" and trend == "UP")


def assess(direction: Literal["BUY", "SELL"],
           reads: Sequence[TimeframeRead]) -> BiasAssessment:
    """Rule on a proposed direction against every timeframe that has an opinion.

    Pure: no bars, no IO, no clock. Everything it needs is in `reads`, which makes the veto
    testable against constructed states rather than against whatever the market did today.
    """
    opinionated = [r for r in reads if r.has_opinion]
    if not opinionated:
        return BiasAssessment(
            Alignment.NEUTRAL, direction, tuple(reads),
            "no higher timeframe has a directional opinion — nothing to fight")

    against = [r for r in opinionated if _opposes(direction, r.direction)]
    if not against:
        agreeing = ", ".join(r.label for r in opinionated)
        return BiasAssessment(Alignment.ALIGNED, direction, tuple(reads),
                              f"agrees with {agreeing}")

    hard = [r for r in against
            if r.state is not None
            and r.state.displacement_state in _HARD_DISPLACEMENT
            and r.state.trend_maturity not in _SPENT_MATURITY]
    if hard:
        worst = hard[0]
        assert worst.state is not None
        return BiasAssessment(
            Alignment.COUNTER_HARD, direction, tuple(reads),
            f"{direction} against a {worst.state.displacement_state} displacement on "
            f"{worst.label} ({worst.state.trend_direction}, "
            f"{worst.state.trend_maturity}) — refused")

    spent = [r for r in against
             if r.state is not None and r.state.trend_maturity in _SPENT_MATURITY]
    note = " (opposed trend is EXHAUSTED — this is the reversal case)" if spent else ""
    return BiasAssessment(
        Alignment.COUNTER_SOFT, direction, tuple(reads),
        f"counter to {', '.join(r.label for r in against)} but no confirmed displacement "
        f"against it{note} — allowed, and the brain is told")


def read_timeframes(bars: Sequence[Bar], i: int,
                    factors: Sequence[tuple[str, int]] = (("H4", 16), ("D1", 96)),
                    ) -> list[TimeframeRead]:
    """Build reads for the entry timeframe plus each higher one, using TRUE aggregation.

    `factors` are (label, bars-per-candle) against the ENTRY timeframe. The defaults assume M15
    entries: 16 -> H4, 96 -> D1.

    **THE HIGHER-TIMEFRAME INDEX IS RECOMPUTED, NEVER REUSED.** Bar `i` on M15 is not bar `i` on
    H4. Aggregating and then classifying at the same index would read a state from months ago and
    label it "now" — a lookahead's mirror image, and just as wrong. The aggregated series is
    classified at its own final CLOSED candle.

    `aggregate` returns the still-forming candle last, by design. It is dropped here: an
    incomplete H4 has a high and low that are not yet its high and low, and a displacement read
    off a partial candle can vanish before the candle closes.
    """
    out = [TimeframeRead("entry", classify(bars, i, swings(bars[:i + 1]),
                                           _atrs(bars[:i + 1])))]
    window = list(bars[:i + 1])
    for label, factor in factors:
        agg = aggregate(window, factor)
        if len(agg) < 2:
            out.append(TimeframeRead(label, None))
            continue
        closed = agg[:-1]                      # drop the forming candle
        sw = swings(closed)
        st = classify(closed, len(closed) - 1, sw, _atrs(closed))
        out.append(TimeframeRead(label, st))
    return out


def _atrs(bars: Sequence[Bar]) -> list[Optional[float]]:
    """ATR series in the shape `classify` expects — one entry per bar, None during warmup."""
    return list(atr(bars))
