"""Uncertainty, decomposed. Item #10.

One confidence number is worse than none, because it invites arithmetic that
does not apply. Two reads at "65%" can mean completely different things:

  A  the mechanism has 400 resolved trades, the regime is familiar, the feed is
     clean, and the estimate is genuinely 65% with a tight interval.
  B  the mechanism has never traded, the regime looks nothing like anything in
     the sample, one input is stale, and 65% is a guess wearing a number.

Averaging those, comparing them, or gating on them treats a measurement and a
shrug as the same object. The analyst should see WHICH KIND of uncertainty it is
facing, because the correct response differs: A argues for normal size, B argues
for either standing aside or sizing as exploration.

SIX SOURCES, KEPT SEPARATE ON PURPOSE

  estimation   how thin is the evidence behind the estimate itself
  regime       how unlike the validated history is the current state
  data         staleness, gaps, disagreement between feeds
  model        disagreement between the arms that have an opinion
  execution    spread relative to the stop; how much of R the venue takes
  event        proximity to a scheduled release that reprices the instrument

They are NOT collapsed into a scalar. A single number is exactly the artefact
this module exists to prevent, and the moment one is produced somebody will
threshold on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass(frozen=True)
class Component:
    name: str
    level: str                     # LOW / MODERATE / HIGH / UNKNOWN
    basis: str                     # the fact behind the label, always
    n: Optional[int] = None

    def render(self) -> str:
        n = f" (n={self.n})" if self.n is not None else ""
        return f"    {self.name:<11} {self.level:<9} {self.basis}{n}"


def _band(x: float, lo: float, hi: float) -> str:
    return "LOW" if x <= lo else ("MODERATE" if x <= hi else "HIGH")


@dataclass
class Uncertainty:
    components: list = field(default_factory=list)

    def add(self, c: Component) -> "Uncertainty":
        self.components.append(c)
        return self

    @property
    def highest(self) -> list:
        return [c for c in self.components if c.level == "HIGH"]

    def render(self) -> str:
        out = ["  UNCERTAINTY — six sources, deliberately not combined"]
        out += [c.render() for c in self.components]
        hi = self.highest
        if hi:
            out.append(f"    -> dominated by: {', '.join(c.name for c in hi)}")
        else:
            out.append("    -> no single source dominates")
        out.append("    A confidence number is NOT provided. These do not reduce "
                   "to one,")
        out.append("    and the right response differs by which one is large.")
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {c.name: {"level": c.level, "basis": c.basis, "n": c.n}
                for c in self.components}


def estimation(n_resolved: int, thin_below: int = 30) -> Component:
    """How much evidence stands behind the estimate."""
    if n_resolved <= 0:
        return Component("estimation", "HIGH",
                         "no resolved history for this mechanism — the estimate "
                         "is a prior, not a measurement", 0)
    if n_resolved < thin_below:
        return Component("estimation", "MODERATE",
                         f"thin cohort, shrunk toward the prior", n_resolved)
    se = math.sqrt(0.25 / n_resolved)
    return Component("estimation", "LOW",
                     f"standard error on a hit rate is about {se:.1%}", n_resolved)


def regime(similarity: Optional[float]) -> Component:
    """How unlike the validated history is now.

    `similarity` is 0..1 and may be None, which is reported as UNKNOWN rather
    than assumed familiar — an unfamiliar regime that claims familiarity is the
    failure mode that makes historical models confidently wrong.
    """
    if similarity is None:
        return Component("regime", "UNKNOWN",
                         "no regime comparison available; historical estimates "
                         "carry unmeasured extrapolation risk")
    lvl = "LOW" if similarity >= 0.7 else ("MODERATE" if similarity >= 0.4 else "HIGH")
    return Component("regime", lvl,
                     f"current state is {similarity:.0%} similar to validated history")


def data_quality(tick_age_s: float, max_age_s: float = 30.0,
                 feeds_disagree: Optional[float] = None,
                 unavailable: int = 0) -> Component:
    bits = [f"quote {tick_age_s:.0f}s old"]
    lvl = "LOW"
    if tick_age_s > max_age_s:
        lvl = "HIGH"
        bits.append("STALE")
    if unavailable:
        lvl = "HIGH" if lvl == "HIGH" else "MODERATE"
        bits.append(f"{unavailable} input(s) UNAVAILABLE")
    if feeds_disagree is not None:
        bits.append(f"feeds differ by ${feeds_disagree:.2f}")
        if feeds_disagree > 0.5:
            lvl = "HIGH"
    return Component("data", lvl, "; ".join(bits))


def model_disagreement(views: dict) -> Component:
    """Do the arms that have an opinion agree? Disagreement is information."""
    vals = [v for v in views.values() if v is not None]
    if len(vals) < 2:
        return Component("model", "UNKNOWN",
                         "fewer than two independent views to compare",
                         len(vals))
    longs = sum(1 for v in vals if str(v).upper().startswith("L"))
    shorts = sum(1 for v in vals if str(v).upper().startswith("S"))
    if longs and shorts:
        return Component("model", "HIGH",
                         f"{longs} long vs {shorts} short — the views conflict",
                         len(vals))
    return Component("model", "LOW", f"all {len(vals)} views agree", len(vals))


def execution(spread: float, risk_price: float) -> Component:
    """How much of R the venue takes before the idea has to be right at all."""
    if risk_price <= 0:
        return Component("execution", "UNKNOWN", "no risk unit")
    cost_r = spread / risk_price
    lvl = _band(cost_r, 0.03, 0.10)
    return Component("execution", lvl,
                     f"round trip is about {cost_r:.1%} of R "
                     f"(spread ${spread:.2f} on a ${risk_price:.2f} stop)")


def event_risk(minutes_to_event: Optional[float], name: str = "") -> Component:
    if minutes_to_event is None:
        return Component("event", "UNKNOWN",
                         "no event calendar wired — proximity to a scheduled "
                         "release is not being checked")
    if minutes_to_event < 0:
        return Component("event", "MODERATE", f"{-minutes_to_event:.0f}m since {name}")
    lvl = "HIGH" if minutes_to_event < 30 else ("MODERATE" if minutes_to_event < 120 else "LOW")
    return Component("event", lvl, f"{minutes_to_event:.0f}m until {name or 'a release'}")


def assess(*, n_resolved: int = 0, similarity: Optional[float] = None,
           tick_age_s: float = 0.0, max_age_s: float = 30.0,
           feeds_disagree: Optional[float] = None, unavailable: int = 0,
           views: Optional[dict] = None, spread: float = 0.0,
           risk_price: float = 0.0,
           minutes_to_event: Optional[float] = None,
           event_name: str = "") -> Uncertainty:
    """Build the full decomposition. Every field optional; absence reads UNKNOWN."""
    u = Uncertainty()
    u.add(estimation(n_resolved))
    u.add(regime(similarity))
    u.add(data_quality(tick_age_s, max_age_s, feeds_disagree, unavailable))
    u.add(model_disagreement(views or {}))
    u.add(execution(spread, risk_price))
    u.add(event_risk(minutes_to_event, event_name))
    return u
