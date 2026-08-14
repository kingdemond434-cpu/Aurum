"""How unlike the validated history is now. Item #11.

THE FAILURE THIS EXISTS TO CATCH

A model estimated on one regime and applied in another is not merely less
accurate — it is confidently wrong, which is worse than being uncertain,
because confidence is what sizing and gating read. Gold has one price history,
and most of it was made under monetary conditions that no longer hold. A
mechanism with 200 resolved trades, every one of them from a single regime, has
a hit rate that is a measurement of that regime and a guess about this one.

`uncertainty.regime()` has always accepted a similarity score and has always
been handed None, so it has reported UNKNOWN on every decision the desk has
ever made. That was honest but useless. This module computes the number.

WHAT SIMILARITY MEANS HERE, PRECISELY

It is NOT "does today look like a day I have seen". It is: of the resolved
trades that back the estimate I am about to use, what fraction were taken in a
context resembling this one? A high score means the estimate is interpolation.
A low score means it is extrapolation, and the estimate should be discounted
even though nothing about it looks wrong.

WHY HAMMING AND NOT SOMETHING CLEVERER

The desk's Context is already a vector of discrete, deliberately-chosen
semantic states — trend direction, health, maturity, volatility, HTF alignment,
displacement, sweep, reclaim, pullback depth, distance from session extreme.
Those dimensions were chosen because the desk's own research says they
discriminate. Agreement across them is the natural measure, and a weighted
match on ten interpretable axes can be read and argued with, which a learned
embedding on twenty resolved trades could not.

Ordinal fields (health, maturity, volatility, pullback depth) score PARTIAL
credit for adjacent states, because MODERATE next to STRONG is a near miss and
MODERATE next to WEAK is not. Nominal fields are exact-match. Both are stated
here rather than buried, because the weighting is a judgement and judgements in
this desk have to be visible enough to be attacked.

THE HONEST LIMIT

With twenty resolved trades, ANY similarity score is itself an estimate on
almost no data. `similarity_to_history` returns None — not a low number, not a
high one — when the comparison set is too thin to mean anything, and the caller
reports UNKNOWN. A confident novelty score computed from nothing would be the
same error one level up.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)

REGIME_VERSION = "regime-2026-08-14-a"

# Below this many comparable trades, novelty is not measurable and saying so is
# the only correct answer. Deliberately not 1: a similarity computed against
# three trades is a statement about three trades.
MIN_COMPARISON_N = 12


# Ordinal scales — adjacency earns partial credit. The ORDER is the claim; each
# is the desk's existing Context vocabulary, unchanged.
ORDINAL: dict[str, tuple[str, ...]] = {
    "trend_health": ("WEAK", "MODERATE", "STRONG"),
    "trend_maturity": ("YOUNG", "MID", "MATURE", "EXHAUSTED"),
    "volatility_state": ("LOW", "NORMAL", "ELEVATED", "EXTREME"),
    "pullback_depth": ("NONE", "SHALLOW", "MEDIUM", "DEEP"),
    "reclaim_state": ("NONE", "WEAK", "CONFIRMED"),
    "displacement_state": ("NONE", "FORMING", "CONFIRMED", "EXCEPTIONAL"),
    "distance_from_session_extreme": ("NEAR", "MID", "FAR"),
}

# Nominal fields — no ordering exists, so only exact agreement counts.
NOMINAL: tuple[str, ...] = ("trend_direction", "htf_alignment", "sweep_state",
                            "session")

# Weights. Trend direction dominates because a mechanism measured in an uptrend
# tells you very little about the same mechanism in a downtrend, and volatility
# is next because it rescales every distance the desk measures in ATR. These are
# a JUDGEMENT, versioned with the module, and ablatable like anything else.
WEIGHTS: dict[str, float] = {
    "trend_direction": 2.0,
    "volatility_state": 1.6,
    "htf_alignment": 1.3,
    "trend_health": 1.2,
    "trend_maturity": 1.0,
    "displacement_state": 1.0,
    "session": 0.9,
    "sweep_state": 0.8,
    "reclaim_state": 0.8,
    "pullback_depth": 0.7,
    "distance_from_session_extreme": 0.5,
}


def _field_score(key: str, a, b) -> Optional[float]:
    """1.0 identical, 0.0 unrelated, partial credit for adjacent ordinals."""
    if a is None or b is None:
        return None
    a, b = str(a), str(b)
    if a == b:
        return 1.0
    scale = ORDINAL.get(key)
    if not scale or a not in scale or b not in scale:
        return 0.0
    gap = abs(scale.index(a) - scale.index(b))
    # One step apart is a near miss, two steps is most of the way to unrelated,
    # three or more is unrelated. Linear in the gap, floored at zero.
    return max(0.0, 1.0 - gap / 2.0)


def context_similarity(a: dict, b: dict) -> Optional[float]:
    """Weighted agreement between two context vectors, 0..1, or None.

    None when the two share too few comparable fields to say anything — an old
    ledger row with three context keys and a current brief with eleven are not
    0% similar, they are incomparable, and reporting 0% would read as "wildly
    novel" when it means "I cannot tell".
    """
    total = matched = 0.0
    seen = 0
    for key in list(ORDINAL) + list(NOMINAL):
        s = _field_score(key, a.get(key), b.get(key))
        if s is None:
            continue
        w = WEIGHTS.get(key, 1.0)
        total += w
        matched += w * s
        seen += 1
    if seen < 4 or total <= 0:
        return None
    return matched / total


@dataclass
class Novelty:
    """The answer, with everything needed to argue with it."""
    similarity: Optional[float]
    comparable_n: int
    nearest: Optional[float]
    mean_top_quartile: Optional[float]
    basis: str
    dissimilar_fields: list = field(default_factory=list)

    @property
    def measurable(self) -> bool:
        return self.similarity is not None

    def render(self) -> str:
        if not self.measurable:
            return f"  REGIME NOVELTY  unmeasurable — {self.basis}"
        odd = (f"  most unlike: {', '.join(self.dissimilar_fields)}"
               if self.dissimilar_fields else "")
        return (f"  REGIME NOVELTY  similarity {self.similarity:.0%} "
                f"(n={self.comparable_n}, nearest {self.nearest:.0%})\n"
                f"  {self.basis}\n{odd}".rstrip())

    def to_dict(self) -> dict:
        return {"similarity": (None if self.similarity is None
                               else round(self.similarity, 4)),
                "comparable_n": self.comparable_n,
                "nearest": None if self.nearest is None else round(self.nearest, 4),
                "dissimilar_fields": self.dissimilar_fields,
                "basis": self.basis, "version": REGIME_VERSION}


def assess_novelty(current: dict, history: Sequence[dict],
                   *, min_n: int = MIN_COMPARISON_N) -> Novelty:
    """How well does the resolved history cover the state we are in now?

    `history` is the list of resolved outcomes (opportunity.resolved_outcomes),
    each carrying the `context` it was taken in.

    The headline number is the mean of the TOP QUARTILE of similarities, not the
    mean of all of them. The question is not "is this the average state I have
    traded" — it never is — but "do I have a meaningful body of experience that
    resembles this one". A handful of close matches inside a large dissimilar
    sample is genuine coverage; the overall mean would hide it.
    """
    sims = []
    for h in history:
        s = context_similarity(current, h.get("context") or {})
        if s is not None:
            sims.append(s)

    if len(sims) < min_n:
        return Novelty(None, len(sims), None, None,
                       f"only {len(sims)} comparable resolved trades; below the "
                       f"{min_n} needed before a novelty score means anything")

    sims.sort(reverse=True)
    k = max(1, len(sims) // 4)
    top = sims[:k]
    headline = statistics.fmean(top)

    # Which dimensions are doing the damage? Reported so a low score is
    # actionable rather than a shrug — "novel because the HTF is conflicted and
    # volatility is EXTREME" is a fact a human can weigh.
    worst: list[tuple[float, str]] = []
    for key in list(ORDINAL) + list(NOMINAL):
        cur = current.get(key)
        if cur is None:
            continue
        vals = [_field_score(key, cur, (h.get("context") or {}).get(key))
                for h in history]
        vals = [v for v in vals if v is not None]
        if len(vals) >= min_n:
            worst.append((statistics.fmean(vals), key))
    worst.sort()
    odd = [k2 for score, k2 in worst[:3] if score < 0.5]

    if headline >= 0.7:
        why = ("the estimate is interpolation — a real body of resolved trades "
               "was taken in states close to this one")
    elif headline >= 0.4:
        why = ("partial coverage — the estimate is being stretched beyond the "
               "states it was measured in")
    else:
        why = ("EXTRAPOLATION — no meaningful body of resolved history resembles "
               "this state, so cohort statistics describe a different market")
    return Novelty(headline, len(sims), sims[0],
                   statistics.fmean(top), why, odd)


def similarity_to_history(current: dict, cohorts, history) -> Optional[float]:
    """The scalar `uncertainty.regime()` wants. None when unmeasurable.

    Kept deliberately thin: the decomposition asks one question and gets one
    number or an honest None. Anything that wants the reasoning calls
    assess_novelty() and gets the whole object.
    """
    if not history:
        return None
    try:
        return assess_novelty(current, history).similarity
    except Exception as e:                    # novelty must never break a decision
        log.debug("novelty unavailable: %s", e)
        return None


def load_history(rows: Iterable[dict]) -> list[dict]:
    """Resolved outcomes in the shape assess_novelty wants. One reader, shared."""
    from .opportunity import resolved_outcomes
    return list(resolved_outcomes(list(rows)))
