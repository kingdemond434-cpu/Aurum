"""What gold is priced against. Item #13.

WHY THIS IS A CAUSAL STATE AND NOT A FEATURE LIST

Gold has no earnings, no yield and no cashflow. Its price is almost entirely a
statement about three things it is quoted against:

  the dollar          gold is priced in it, so DXY moves are partly mechanical
  real yields         the opportunity cost of holding a zero-yield asset
  risk appetite       gold competes with equities for the same fear premium

Adding these as anonymous columns to a model is the standard mistake: it lets a
fitted model discover that gold rose when SPX rose in one sample and encode it
forever. Stating them as a CAUSAL STATE with a direction of expected effect
means a violated expectation is INFORMATION — "gold is up with real yields up
and the dollar up" is a genuinely unusual configuration, and unusual
configurations are where regimes turn.

WHAT IS DELIBERATELY NOT HERE

Any fetching. This desk's network access is a deployment question and its data
sources are the operator's credentials, so the module defines the STATE and the
honest UNAVAILABLE path and takes a provider. Wiring a specific vendor in here
would make the causal model depend on whose API key is present.

THE UNAVAILABLE PATH IS THE IMPORTANT PART

Every field is Optional and absence reads UNAVAILABLE, never neutral. A desk
that treats a missing DXY as "dollar unchanged" is asserting something it does
not know, and asserting it in the direction that makes everything look calm.
`coverage` reports what fraction of the state is actually observed, and
`is_actionable` refuses below a floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

CROSSMARKET_VERSION = "xmkt-2026-08-14-a"

# Below this fraction observed, the cross-market read is not a state, it is a
# guess with some numbers in it.
MIN_COVERAGE = 0.6


@dataclass(frozen=True)
class Driver:
    """One thing gold is priced against, with the SIGN stated up front.

    `expected_sign` is the direction of gold's usual response — declared, not
    fitted. Declaring it is what makes a violation detectable; a fitted sign
    absorbs the violation and reports nothing.
    """
    key: str
    label: str
    expected_sign: int          # +1 gold usually rises with it, -1 usually falls
    why: str


DRIVERS: tuple = (
    Driver("dxy", "US dollar index", -1,
           "gold is quoted in dollars, so a stronger dollar mechanically lowers "
           "the gold price before any demand effect"),
    Driver("real_yield_10y", "10y real yield", -1,
           "gold yields nothing, so the real return available elsewhere is its "
           "direct opportunity cost — the cleanest fundamental link there is"),
    Driver("spx", "equity risk appetite", -1,
           "gold competes for the same defensive allocation; risk-on usually "
           "drains it, though this link is the weakest and inverts in inflation "
           "scares"),
    Driver("breakeven_10y", "10y inflation breakeven", +1,
           "gold is bought as an inflation hedge, so expected inflation is one "
           "of the few drivers that moves it UP"),
    Driver("vix", "volatility / fear", +1,
           "genuine stress bids gold, but only when it is not a liquidity "
           "event — in a margin call gold is sold BECAUSE it is liquid"),
)
BY_KEY = {d.key: d for d in DRIVERS}


@dataclass
class Observation:
    """One driver's move, or an honest absence."""
    key: str
    change_pct: Optional[float]        # over the lookback window
    z: Optional[float] = None          # standardised, when history allows
    as_of: Optional[datetime] = None
    source: str = "UNAVAILABLE"

    @property
    def observed(self) -> bool:
        return self.change_pct is not None

    @property
    def stale_hours(self) -> Optional[float]:
        if self.as_of is None:
            return None
        return (datetime.now(timezone.utc) - self.as_of).total_seconds() / 3600.0

    def render(self) -> str:
        d = BY_KEY.get(self.key)
        label = d.label if d else self.key
        if not self.observed:
            return f"    {label:<24} UNAVAILABLE  ({self.source})"
        z = f"  z={self.z:+.1f}" if self.z is not None else ""
        age = ""
        if self.stale_hours is not None and self.stale_hours > 24:
            age = f"  [{self.stale_hours:.0f}h old]"
        return (f"    {label:<24} {self.change_pct:+7.2f}%{z}  "
                f"({self.source}){age}")


@dataclass
class CrossMarketState:
    observations: list = field(default_factory=list)
    gold_change_pct: Optional[float] = None
    lookback_hours: float = 24.0

    def get(self, key: str) -> Optional[Observation]:
        return next((o for o in self.observations if o.key == key), None)

    @property
    def coverage(self) -> float:
        if not DRIVERS:
            return 0.0
        return sum(1 for d in DRIVERS
                   if (o := self.get(d.key)) and o.observed) / len(DRIVERS)

    @property
    def is_actionable(self) -> bool:
        return (self.coverage >= MIN_COVERAGE
                and self.gold_change_pct is not None)

    def expected_direction(self) -> Optional[float]:
        """Where the observed drivers say gold should have gone, sign-weighted.

        Unweighted on purpose. Weighting the drivers by fitted coefficients is
        exactly the step that turns a stated causal model into an unstated
        fitted one, and with this desk's sample there is nothing to fit them on.
        """
        parts = [d.expected_sign * o.change_pct
                 for d in DRIVERS
                 if (o := self.get(d.key)) and o.observed]
        return sum(parts) / len(parts) if parts else None

    def divergences(self) -> list:
        """Drivers whose usual relationship is currently NOT holding.

        This is the output worth having. Agreement is the base case and carries
        little information; a driver moving against gold while gold ignores it
        is either a regime change or a driver that has stopped mattering, and
        both are things the analyst should see stated rather than inferred.
        """
        if self.gold_change_pct is None:
            return []
        out = []
        for d in DRIVERS:
            o = self.get(d.key)
            if not o or not o.observed or abs(o.change_pct) < 0.15:
                continue
            expected = d.expected_sign * (1 if o.change_pct > 0 else -1)
            actual = 1 if self.gold_change_pct > 0 else -1
            if expected != actual:
                out.append((d, o))
        return out

    def render(self) -> str:
        out = [f"  CROSS-MARKET STATE ({CROSSMARKET_VERSION}, "
               f"{self.lookback_hours:.0f}h)"]
        g = ("UNAVAILABLE" if self.gold_change_pct is None
             else f"{self.gold_change_pct:+.2f}%")
        out.append(f"    {'XAUUSD':<24} {g}")
        out += [o.render() for o in self.observations]
        out.append(f"    coverage {self.coverage:.0%}"
                   + ("" if self.is_actionable else
                      f"  — BELOW THE {MIN_COVERAGE:.0%} FLOOR, this is not a "
                      f"state, it is a partial guess"))
        if not self.is_actionable:
            return "\n".join(out)
        exp = self.expected_direction()
        if exp is not None:
            agree = (exp > 0) == (self.gold_change_pct > 0)
            out.append(f"    drivers point {'UP' if exp > 0 else 'DOWN'}; "
                       f"gold went {'UP' if self.gold_change_pct > 0 else 'DOWN'} "
                       f"— {'consistent' if agree else 'INCONSISTENT'}")
        div = self.divergences()
        if div:
            out.append("    DIVERGING (the usual relationship is not holding):")
            for d, o in div:
                out.append(f"      {d.label}: {o.change_pct:+.2f}% — {d.why}")
        else:
            out.append("    no driver is diverging from its usual relationship")
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {"version": CROSSMARKET_VERSION,
                "lookback_hours": self.lookback_hours,
                "gold_change_pct": self.gold_change_pct,
                "coverage": round(self.coverage, 3),
                "actionable": self.is_actionable,
                "observations": {o.key: {"change_pct": o.change_pct,
                                         "source": o.source} for o in self.observations},
                "diverging": [d.key for d, _ in self.divergences()]}

    def brief_lines(self) -> list:
        """What the analyst sees. Facts only, never an instruction.

        Cross-market context is EVIDENCE. It has no vote on direction and never
        becomes a gate — the same rule external signals live under, for the same
        reason: a correlation that held for a decade is still a correlation.
        """
        if not self.is_actionable:
            return ["CROSS-MARKET: unavailable or below coverage floor — "
                    "treat gold's move as unexplained rather than assuming the "
                    "drivers were quiet"]
        lines = ["CROSS-MARKET (context, not a signal):"]
        for o in self.observations:
            if o.observed:
                lines.append(f"  {BY_KEY[o.key].label}: {o.change_pct:+.2f}%")
        for d, o in self.divergences():
            lines.append(f"  DIVERGENCE — {d.label} moved {o.change_pct:+.2f}% "
                         f"and gold did not respond as it usually does")
        return lines


# --------------------------------------------------------------------------
# The provider seam
# --------------------------------------------------------------------------

Fetcher = Callable[[str, float], Optional[tuple]]


def build_state(fetch: Optional[Fetcher] = None, *,
                lookback_hours: float = 24.0,
                gold_change_pct: Optional[float] = None) -> CrossMarketState:
    """Assemble the state from whatever a fetcher can supply.

    `fetch(key, lookback_hours)` returns (change_pct, as_of, source) or None.
    Passing no fetcher yields a fully UNAVAILABLE state, which is the correct
    output when nothing is wired and is deliberately not an error: the desk
    trades without this, and it must not start failing because a data source it
    never had is still absent.
    """
    st = CrossMarketState(lookback_hours=lookback_hours,
                          gold_change_pct=gold_change_pct)
    for d in DRIVERS:
        if fetch is None:
            st.observations.append(Observation(d.key, None, source="not wired"))
            continue
        try:
            got = fetch(d.key, lookback_hours)
        except Exception as e:
            log.debug("cross-market fetch failed for %s: %s", d.key, e)
            got = None
        if not got:
            st.observations.append(Observation(d.key, None, source="fetch failed"))
            continue
        change, as_of, source = got
        st.observations.append(Observation(d.key, float(change), None,
                                           as_of, source or "unknown"))
    return st


def report(state: CrossMarketState) -> str:
    return (f"CROSS-MARKET CAUSAL STATE (#13)\n\n{state.render()}\n\n"
            "  The expected SIGN of each driver is DECLARED, not fitted. That is\n"
            "  what makes a violation detectable: a fitted sign absorbs the\n"
            "  violation and reports nothing, and the violations are the only\n"
            "  part of this with real information in it.\n\n"
            "  Absence reads UNAVAILABLE, never neutral. A missing DXY treated as\n"
            "  'dollar unchanged' asserts something unknown, in the direction\n"
            "  that makes everything look calm.")
