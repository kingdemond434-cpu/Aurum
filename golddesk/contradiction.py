"""The evidence FOR and AGAINST a direction, scored, from measured state only.

WHY THIS EXISTS, and it is the desk's own measurement that asked for it:

    read_quality selection: taken trades resolve -0.14R while refusals reached
    +0.56R at best. The analyst is selecting AGAINST itself — that is not
    caution, it is being wrong in a direction no win-rate shows.

That is not a frequency problem. The desk is not taking too few trades; it is
choosing the wrong ones out of a set that contained better. More selectivity
would make it worse, and so would more signals. What is missing is RANKING.

WHAT WAS ACTUALLY WRONG WITH THE OLD ARRANGEMENT. The analyst already writes a
counter-argument on every read, and they are good ones — the 2026-08-28 short
said, unprompted, "TRENDMATURITY already reads EXHAUSTED", "ratio moves are not
a timing tool", "gold can fall for a week on this premise and still bounce 40
points first, which the L3 stop will not survive". Every one of those was right.
They were also PROSE, in a paragraph, weighted by nobody, and the trade went out
anyway with a conf of 2/5 that no number stood behind. A contradiction that
lives in a sentence is a contradiction that loses to enthusiasm.

So the contradictions are counted. Each item below is a fact the desk MEASURES,
carries a sign and a weight, and lands in a net balance the operator can see and
a ranker can sort by.

THIS SCORES. IT DOES NOT GATE. Nothing here refuses anything, and that is
deliberate rather than timid: the desk's standing order is maximum frequency,
and the evidence says the problem is ordering rather than volume. A gate would
cut the tail of good trades along with the bad, and this desk has fourteen
resolved trades — nowhere near enough to know where such a line belongs. The
score is recorded so that "does a negative balance actually predict a worse
outcome" becomes answerable, and if it does, THEN it can earn authority.

WEIGHTS ARE RANKED BY RELIABILITY, not by strength of feeling: direct price
observation outranks derived state, which outranks regime interpretation. The
numbers are small integers on purpose. A weighting scheme tuned to three decimal
places on fourteen trades would be a fit to noise wearing a lab coat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

CONTRADICTION_VERSION = "contra-2026-08-29-a"

#: Net balance at or below which the proposition is carrying more measured
#: evidence against it than for it. Reported, never enforced.
NEGATIVE_AT = 0


@dataclass(frozen=True)
class Item:
    """One measured fact, its direction of support, and how much it is worth."""
    fact: str
    weight: int
    supports: bool

    @property
    def line(self) -> str:
        return f"    {'+' if self.supports else '-'}{self.weight}  {self.fact}"


@dataclass(frozen=True)
class Balance:
    direction: str
    items: Sequence[Item] = field(default_factory=tuple)

    @property
    def for_score(self) -> int:
        return sum(i.weight for i in self.items if i.supports)

    @property
    def against_score(self) -> int:
        return sum(i.weight for i in self.items if not i.supports)

    @property
    def net(self) -> int:
        return self.for_score - self.against_score

    @property
    def contradicted(self) -> bool:
        """More measured evidence against than for. NOT a refusal."""
        return self.net <= NEGATIVE_AT and bool(self.items)

    def to_dict(self) -> dict:
        return {"version": CONTRADICTION_VERSION, "direction": self.direction,
                "net": self.net, "for": self.for_score,
                "against": self.against_score,
                "contradicted": self.contradicted,
                "items": [{"fact": i.fact, "w": i.weight, "supports": i.supports}
                          for i in self.items]}

    def render(self) -> str:
        if not self.items:
            return ("EVIDENCE BALANCE: UNMEASURED — no structural state was "
                    "available to weigh. Absence of contradiction is not support.")
        head = (f"EVIDENCE BALANCE {self.direction}: net {self.net:+d} "
                f"(for {self.for_score}, against {self.against_score})")
        if self.contradicted:
            head += " — MORE MEASURED EVIDENCE AGAINST THAN FOR"
        return "\n".join([head] + [i.line for i in self.items])


def _get(ctx: Any, name: str) -> Optional[str]:
    v = getattr(ctx, name, None)
    return v if isinstance(v, str) else None


def weigh(direction: str, ctx: Any) -> Balance:
    """Score a proposed direction against measured structure. Pure.

    Only facts the desk MEASURES appear here. Nothing is inferred, nothing is
    read from prose, and a state the desk did not compute contributes nothing
    rather than contributing zero — an unmeasured context yields an empty
    balance, which render() reports as UNMEASURED rather than as neutral.
    """
    if direction not in ("LONG", "SHORT"):
        return Balance(direction, ())

    long = direction == "LONG"
    with_dir = "UP" if long else "DOWN"
    against_dir = "DOWN" if long else "UP"
    items: list[Item] = []

    trend = _get(ctx, "trend_direction")
    health = _get(ctx, "trend_health")
    if trend == with_dir:
        # WEIGHT 3, the largest here, because the direction of the trend is the
        # single most directly observed fact in the set.
        items.append(Item(f"trend is {trend}, with the trade", 3, True))
        if health == "STRONG":
            items.append(Item("trend health STRONG", 1, True))
        elif health == "WEAK":
            items.append(Item("trend health WEAK — the trend agreeing is worth "
                              "less than it looks", 1, False))
    elif trend == against_dir:
        items.append(Item(f"trend is {trend}, AGAINST the trade", 3, False))
    elif trend == "NONE":
        items.append(Item("no measured trend — direction has no structural "
                          "support either way", 2, False))

    disp = _get(ctx, "displacement_state")
    if disp in ("CONFIRMED", "EXCEPTIONAL"):
        if trend == with_dir:
            items.append(Item(f"displacement {disp} in the trade's direction",
                              2, True))
        elif trend == against_dir:
            items.append(Item(f"displacement {disp} AGAINST the trade — an "
                              f"impulse is being entered into", 3, False))
    elif disp == "NONE":
        items.append(Item("no displacement — nothing is impulsing", 1, False))

    if _get(ctx, "reclaim_state") == "CONFIRMED":
        items.append(Item("reclaim CONFIRMED", 2, True))
    elif _get(ctx, "reclaim_state") == "WEAK":
        items.append(Item("reclaim only WEAK — the level is not established", 1,
                          False))

    if _get(ctx, "trend_maturity") == "EXHAUSTED":
        # THE ONE THE LIVE READ NAMED AND TRADED THROUGH ANYWAY.
        items.append(Item("trend maturity EXHAUSTED — late in the move", 2, False))
    elif _get(ctx, "trend_maturity") == "YOUNG" and trend == with_dir:
        items.append(Item("trend maturity YOUNG", 1, True))

    vol = _get(ctx, "volatility_state")
    if vol == "LOW" and disp in ("CONFIRMED", "EXCEPTIONAL"):
        items.append(Item("displacement in LOW volatility — grinding breaks "
                          "revert more often than they run", 1, False))
    elif vol == "EXTREME":
        items.append(Item("volatility EXTREME — stop distance sized off a "
                          "trailing average will not match this tape", 1, False))

    if _get(ctx, "distance_from_session_extreme") == "NEAR" and \
            _get(ctx, "sweep_state") != "CONFIRMED":
        items.append(Item("entering NEAR the session extreme with no sweep "
                          "confirmed", 1, False))

    return Balance(direction, tuple(items))
