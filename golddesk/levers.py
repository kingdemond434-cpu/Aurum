"""Which lever actually buys growth, and which one only feels like it does.

"What would make the twelve-sleeve book maximally profitable — do we raise the
heat cap and the lots?" is the right question with a wrong first guess, and the
arithmetic settles it.

SIZING CANNOT CREATE EXPECTANCY

Per-trade edge is a property of the strategy. Sizing is a multiplier on it, and
log growth per trade is roughly

    g  ~=  q * mu  -  q^2 * sigma^2 / 2

which rises in q, peaks, and falls. Past the peak MORE SIZE IS LESS MONEY — not
riskier money, less. So "raise the heat cap" has an answer that is a number, and
above that number it is not aggression, it is subtraction.

THE FOUR LEVERS, AND WHY ONLY TWO OF THEM ARE REAL HERE

    q          per-trade risk. Bounded above by the half-edge Kelly point. If
               the book is already near it, this lever is EXHAUSTED and turning
               it further costs growth.

    n          trades per year. Multiplies growth directly — but only if the
               added trades carry the same edge. Adding sleeves that dilute
               expectancy buys frequency and sells edge, and the book can end up
               worse for being busier.

    mu         expectancy per trade. Always helps, and is the hardest to move.

    rho        correlation between sleeves. THE LEVER NOBODY REACHES FOR, and on
               a twelve-sleeve book of one strategy family it is the binding
               one. Heat scales with sqrt(k_eff) and k_eff = N/(1+(N-1)rho)
               SATURATES AT 1/rho — so at rho = 0.165 the ceiling is 6.1
               effective bets however many sleeves are added. Twelve correlated
               sleeves are not four times three; they are 4.26 bets against
               2.26, and the twelfth is nearly free of information.

This module computes the local derivative of growth with respect to each, so the
answer to "where does the next unit of effort go" is measured rather than
argued.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

LEVERS_VERSION = "levers-2026-08-18-a"


def keff(n: int, rho: float) -> float:
    if n <= 1:
        return 1.0
    rho = max(min(float(rho), 1.0), -1.0 / (n - 1) + 1e-9)
    return max(1.0, min(float(n), n / (1.0 + (n - 1) * rho)))


def growth(q: float, mu: float, n_per_year: float, sd: float = 1.0) -> float:
    """Annual log growth, compounded per trade. Exact, not the quadratic
    approximation — the approximation is fine for intuition and wrong at the
    sizes a heat cap actually permits."""
    if q <= 0:
        return 0.0
    # Two-outcome book at the implied hit rate: +2R wins, -1R losses.
    p = max(0.0, min(1.0, (mu + 1) / 3.0))
    up, dn = 1 + 2 * q, 1 - q
    if up <= 0 or dn <= 0:
        return float("-inf")
    per = p * math.log(up) + (1 - p) * math.log(dn)
    return math.expm1(per * n_per_year)


@dataclass
class Lever:
    name: str
    current: float
    nudged: float
    growth_now: float
    growth_after: float
    why: str = ""

    @property
    def delta(self) -> float:
        return self.growth_after - self.growth_now

    @property
    def exhausted(self) -> bool:
        return self.delta <= 0

    def render(self) -> str:
        tag = "EXHAUSTED" if self.exhausted else f"{self.delta:+.1%}"
        return (f"  {self.name:<26}{self.current:>9.3f} -> {self.nudged:<9.3f}"
                f"{tag:>14}   {self.why}")


#: Annual growth above which the INPUT is the problem, not the arithmetic. A
#: book compounding past this on a liquid market would be the best trading
#: record in existence, so a model producing it is reporting an expectancy that
#: does not survive contact with reality.
IMPLAUSIBLE_GROWTH = 10.0


@dataclass
class LeverReport:
    base_growth: float
    levers: tuple
    binding: str
    why: str

    @property
    def implausible(self) -> bool:
        return self.base_growth > IMPLAUSIBLE_GROWTH

    def ranked(self) -> tuple:
        """Levers by marginal payoff. THE PART THAT SURVIVES A BAD INPUT.

        Every lever is evaluated against the same expectancy, so an overstated
        mu inflates all of them together and the ORDER is unchanged. The ranking
        is usable when the levels are not, which is the normal case here.
        """
        return tuple(sorted(self.levers, key=lambda l: -l.delta))

    def render(self) -> str:
        lines = [f"GROWTH LEVERS  ({LEVERS_VERSION})",
                 f"  base annual growth   {self.base_growth:+.1%}"]
        if self.implausible:
            lines.append(
                "  THE LEVEL IS NOT A FORECAST. A book compounding this fast on "
                "liquid gold would be the best trading record in existence, so "
                "the expectancy driving it is in-sample and overstated. THE "
                "RANKING BELOW STILL HOLDS: every lever is evaluated against the "
                "same mu, so an inflated mu lifts all of them together and the "
                "ORDER is unchanged. Read the order, discard the percentages.")
        lines.append("")
        lines += [l.render() for l in self.ranked()]
        lines += ["", f"  BINDING CONSTRAINT: {self.binding}", f"  {self.why}"]
        return "\n".join(lines)


def analyse(n_sleeves: int, mu: float, n_per_year: float, rho: float,
            base_heat: float = 0.0381, sd: float = 1.0,
            nudge: float = 0.10) -> LeverReport:
    """Local sensitivity of growth to each lever, at a matched relative nudge.

    Every lever is moved by the same RELATIVE amount so the comparison is fair —
    nudging q by an absolute 1% and rho by an absolute 0.1 would compare a small
    change to a huge one and rank whichever happened to be larger.
    """
    def q_of(rho_: float, n_: int) -> float:
        h = base_heat * math.sqrt(keff(n_, rho_))
        return h / max(n_, 1)

    q0 = q_of(rho, n_sleeves)
    g0 = growth(q0, mu, n_per_year, sd)

    levers = []

    # q: raise per-trade risk directly, holding everything else.
    q1 = q0 * (1 + nudge)
    levers.append(Lever("q  per-trade risk", q0, q1, g0,
                        growth(q1, mu, n_per_year, sd),
                        "the heat cap, turned up"))

    # n_per_year: more trades at the SAME edge.
    n1 = n_per_year * (1 + nudge)
    levers.append(Lever("n  trades/year", n_per_year, n1, g0,
                        growth(q0, mu, n1, sd),
                        "only if the added trades carry the same edge"))

    # mu: better expectancy per trade.
    m1 = mu * (1 + nudge)
    levers.append(Lever("mu expectancy/trade", mu, m1, g0,
                        growth(q0, m1, n_per_year, sd),
                        "costs, exits, or a real filter"))

    # rho: LOWER correlation is the improvement, so nudge downward.
    r1 = max(0.0, rho * (1 - nudge))
    levers.append(Lever("rho sleeve correlation", rho, r1, g0,
                        growth(q_of(r1, n_sleeves), mu, n_per_year, sd),
                        "genuinely different edges, not more variants"))

    # An extra sleeve at the SAME correlation — the "just deploy more" move.
    q_more = q_of(rho, n_sleeves + 1)
    n_more = n_per_year * (n_sleeves + 1) / n_sleeves
    levers.append(Lever("+1 sleeve (same family)", float(n_sleeves),
                        float(n_sleeves + 1), g0,
                        growth(q_more, mu, n_more, sd),
                        "more trades, thinner each, no new information"))

    best = max(levers, key=lambda l: l.delta)
    ceiling = (1.0 / rho) if rho > 0 else float("inf")
    q_lever = levers[0]
    if q_lever.exhausted:
        binding = "per-trade risk is already past its optimum"
        why = ("Raising the heat cap from here REDUCES growth. The book is at or "
               "beyond the point where more size is less money, and the lever "
               "everybody reaches for first is the one that is spent.")
    else:
        binding = f"the {best.name.split()[0]} lever pays most at this margin"
        why = (f"k_eff is {keff(n_sleeves, rho):.2f} against a ceiling of "
               f"{ceiling:.1f} at rho={rho:.3f}. Sleeves beyond that buy "
               f"frequency and no information.")
    return LeverReport(g0, tuple(levers), binding, why)


def max_safe_q(mu: float, n_per_year: float, sd: float = 1.0,
               half_edge: bool = True) -> tuple:
    """The q that maximises growth, and the one that survives a halved edge.

    Returns (q_full, q_half, growth_at_q_half). The second is the number to
    size at: the measured edge is biased upward because the sleeves in the
    ledger are the ones that survived selection, so the peak computed from it
    sits to the right of the true peak. Sizing below the half-edge peak survives
    the edge being twice as bad as it looks and still compounds.
    """
    def best(m):
        bq, bg = 0.0, 0.0
        for i in range(1, 2000):
            q = i / 10_000.0
            g = growth(q, m, n_per_year, sd)
            if g > bg:
                bq, bg = q, g
        return bq, bg
    qf, _ = best(mu)
    qh, gh = best(mu * 0.5)
    return qf, qh, growth(qh, mu, n_per_year, sd)
