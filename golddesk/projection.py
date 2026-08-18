"""What the desk should expect to make — as a range, from stated assumptions.

Asked "what will this earn", the tempting answer is a number. A number implies a
precision nobody has, and on this desk it would be a fabrication: Aurum has ZERO
forward evidence, and the quant desk's gold sleeves have in-sample expectancies
that have not survived their own multiplicity correction.

So this produces a LADDER instead, from the most flattering assumption to the
most defensible, and the gap between the rungs is the actual answer:

    IN-SAMPLE          the expectancy as measured on the data that found it
    HALF-EDGE          the same book if the true edge is half the measured one
    DEFLATED           after the trial count the search actually spent
    FORWARD            what has been observed live — which is currently nothing

A projection quoting only the first rung is a brochure. Quoting the spread
between the first and the last is a forecast.

WHY THE HALF-EDGE RUNG IS NOT PESSIMISM

The measured edge is biased upward by construction: the sleeves in the ledger
are the ones that survived selection, and the conditioning thresholds were
chosen on the same data that scores them. Halving the expectancy is not a
haircut for safety, it is an estimate of what selection bias plausibly costs.
`growth.py` already sizes against it for exactly this reason.

WHAT IS DELIBERATELY NOT MODELLED

Slippage beyond the cost already inside the expectancy, because the desk has
never had a fill and `markout.py` reports execution as UNMEASURED. Adding a
guessed slippage would make the output look more careful while making it less
true — the honest treatment is to name it as an unpriced term.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

PROJECTION_VERSION = "project-2026-08-18-a"


@dataclass(frozen=True)
class Sleeve:
    """One book's measured behaviour, with the provenance of the measurement."""
    name: str
    exp_r: float                  # expectancy per trade, in R, net of modelled cost
    trades_per_year: float
    max_dd_r: float
    n_measured: int
    #: True when the expectancy comes from live forward fills rather than a
    #: backtest. Nothing on this desk currently qualifies, and the projection
    #: says so rather than quietly treating a backtest as a track record.
    forward: bool = False
    note: str = ""


@dataclass
class Rung:
    label: str
    exp_r: float
    annual_r: float
    annual_return: float          # fraction of equity, at the sized q
    why: str

    def render(self) -> str:
        return (f"  {self.label:<14}{self.exp_r:+7.3f}R/trade  "
                f"{self.annual_r:+8.1f}R/yr  {self.annual_return:+8.1%}/yr")


def deflate_expectancy(exp_r: float, n: int, n_trials: int,
                       sd_r: float = 1.0) -> float:
    """Expectancy after subtracting what the best of `n_trials` would show by luck.

    The standard error of a mean over n trades is sd/sqrt(n); the best of N
    independent noise trials sits about E[max of N normals] standard errors
    above zero. Subtracting that is the crudest possible deflation and it is
    deliberately crude — a precise correction over an imprecise trial count
    would be false precision on top of a guess.
    """
    if n < 2 or n_trials < 2:
        return exp_r
    se = sd_r / math.sqrt(n)
    a = math.sqrt(2.0 * math.log(n_trials))
    e_max = a - (math.log(math.log(n_trials)) + math.log(4.0 * math.pi)) / (2.0 * a)
    return exp_r - e_max * se


@dataclass
class Projection:
    sleeves: tuple
    q: float                      # risk per trade, as a fraction of equity
    n_trials: int
    rungs: tuple = ()
    unpriced: tuple = ()

    @property
    def total_trades(self) -> float:
        return sum(s.trades_per_year for s in self.sleeves)

    def render(self, equity: float = 0.0) -> str:
        fwd = [s for s in self.sleeves if s.forward]
        lines = [f"PROJECTION  ({PROJECTION_VERSION})",
                 f"  sleeves            {len(self.sleeves)}"
                 f"  ({len(fwd)} with FORWARD evidence)",
                 f"  trades/year        {self.total_trades:.0f}",
                 f"  risk per trade     {self.q:.3%}",
                 f"  trial count        {self.n_trials:,}",
                 ""]
        lines += [r.render() for r in self.rungs]
        if equity > 0:
            lines += ["", f"  on {equity:,.0f} of equity:"]
            for r in self.rungs:
                lines.append(f"    {r.label:<14}{equity * r.annual_return:+12,.0f}"
                             f"/yr  ->  {equity * (1 + r.annual_return):,.0f}")
        lines += [""] + [f"  {u}" for u in self.unpriced]
        if not fwd:
            lines += ["",
                      "  NO SLEEVE HERE HAS FORWARD EVIDENCE. Every rung above is "
                      "computed from backtests, including the top one. The only "
                      "number that would settle this is the one nobody has yet."]
        return "\n".join(lines)


def project(sleeves: Sequence[Sleeve], q: float, n_trials: int,
            compounding: bool = True) -> Projection:
    """The ladder. Same book, four honesty levels.

    `q` is the risk fraction per trade — from `growth.recommend`, not chosen
    here, because a projection that picks its own sizing can produce any answer
    the author wants.
    """
    sl = list(sleeves)
    tpy = sum(s.trades_per_year for s in sl)
    if not sl or tpy <= 0:
        return Projection((), q, n_trials, (),
                          ("no sleeves supplied; nothing to project.",))

    def weighted(f) -> float:
        return sum(f(s) * s.trades_per_year for s in sl) / tpy

    def rung(label: str, exp: float, why: str) -> Rung:
        annual_r = exp * tpy
        if compounding:
            # Each trade risks q of CURRENT equity, so the account compounds at
            # (1 + q*exp) per trade rather than accruing q*exp linearly. Over a
            # few hundred trades the difference is the entire result, in both
            # directions — which is why a linear projection flatters a winner
            # and hides how fast a loser dies.
            growth = (1.0 + q * exp) ** tpy - 1.0
        else:
            growth = q * annual_r
        return Rung(label, exp, annual_r, growth, why)

    in_sample = weighted(lambda s: s.exp_r)
    half = weighted(lambda s: s.exp_r * 0.5)
    defl = weighted(lambda s: deflate_expectancy(s.exp_r, s.n_measured, n_trials))
    fwd_sleeves = [s for s in sl if s.forward]
    forward = (sum(s.exp_r * s.trades_per_year for s in fwd_sleeves) / tpy
               if fwd_sleeves else 0.0)

    rungs = (
        rung("IN-SAMPLE", in_sample,
             "measured on the data that found it, thresholds included"),
        rung("HALF-EDGE", half,
             "if selection bias cost half the edge — an estimate, not a haircut"),
        rung("DEFLATED", defl,
             f"minus what the best of {n_trials:,} trials shows by luck alone"),
        rung("FORWARD", forward,
             "observed live" if fwd_sleeves else
             "NOTHING OBSERVED LIVE. This rung is zero because the desk has "
             "never had a fill, not because the edge is zero."),
    )
    unpriced = (
        "SLIPPAGE BEYOND MODELLED COST IS NOT IN THESE NUMBERS. markout reports "
        "execution as UNMEASURED because nothing has filled; a guessed slippage "
        "would make this look more careful while making it less true.",
        "The gap between IN-SAMPLE and DEFLATED is the size of the multiplicity "
        "problem. If it is large, the top rung is a description of a search, not "
        "of a market.",
    )
    return Projection(tuple(sl), q, n_trials, rungs, unpriced)


#: The armed gold book as MECHANISM_REPORT_ASIA_GOLD.md measures it. IN-SAMPLE,
#: from the sweep that selected it, with the conditioning thresholds fitted on
#: the same data — which is precisely why the ladder above exists.
ASIA_GOLD_MEASURED = (
    Sleeve("gold_asia.TREND_DAY", exp_r=0.908, trades_per_year=261 / 8.6,
           max_dd_r=-5.2, n_measured=261,
           note="prior-NY range > 1.5x median; PF 4.29, deflated t 9.85"),
    Sleeve("gold_asia.NORMAL_DAY", exp_r=0.459, trades_per_year=758 / 8.6,
           max_dd_r=-6.3, n_measured=758,
           note="PF 2.53, deflated t 9.56"),
)
