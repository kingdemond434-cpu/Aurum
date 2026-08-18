"""Maximum safe aggressive growth — solved from evidence, never chosen.

Ported from the MT5 desk's risk layer and adapted to Aurum, which advises rather
than executes. The constitution's demand is explicit: the risk fraction, the
heat cap and the leverage must never be hardcoded, must push the largest SAFE
size, must leave no growth on the table, and must scale as capital grows. Every
number below is therefore DERIVED from two inputs the principal can argue about
— the drawdown they will sit through, and the worst drawdown the book has
actually produced — and nothing here is a literal chosen because it looked
prudent.

WHY "MAXIMUM AGGRESSION" IS NOT "MAXIMUM SIZE", AND WHY THAT IS NOT TIMIDITY

Log growth per trade at risk fraction q is E[ln(1 + qR)]. It rises, peaks at the
Kelly optimum, and then FALLS — and past roughly twice Kelly it goes negative
while every backtest number still looks excellent, because backtests report
arithmetic mean return and the account compounds geometrically. So beyond the
peak, more size is LESS money. Not riskier money: less.

That makes the location of the peak the whole question, and the peak is
estimated from a measured edge that is itself uncertain — and biased upward,
because the mechanisms that reached the ledger are the ones that survived
selection. `half_edge_check()` is the guard: recompute the optimum assuming the
true edge is HALF the measured one, and require the recommendation to sit
comfortably below THAT. A desk that survives its edge being twice as bad as it
looks is aggressive. A desk sized at the measured peak is merely betting that an
in-sample number is exact, which is the opposite of aggression — it is optimism
with leverage.

WHY THE HEAT BUDGET SCALES WITH SQRT(k_eff) AND WHY THAT IS THE COMPOUNDING ENGINE

Portfolio drawdown for k independent bets at total heat H scales roughly as
H/sqrt(k). Holding drawdown fixed therefore lets total heat GROW with sqrt(k) —
five genuinely independent mechanisms are safer at 6% than three correlated ones
at 4%. That is the mechanism by which the desk is supposed to widen as it earns
breadth, and it is the difference between a desk that compounds and one pinned
to a three-leg budget forever.

On the MT5 desk this ladder was dead code: nothing computed k_eff, so the budget
returned its base on every call for the life of the desk. A scaling term nothing
supplies is a constant with extra steps, and in that case a constant that
permanently capped compounding. Here k_eff is measured, and when it cannot be
measured the budget stays at base — because not-yet-measured must never read as
independent. That is exactly how a correlated book comes to size like a
diversified one and discovers its real correlation during the drawdown.

THE CEILING NOBODY CAN ENGINEER AWAY

k_eff = N/(1 + (N−1)ρ) saturates at 1/ρ. At a mean correlation of 0.165 the
ceiling is about 6.1 effective bets no matter how many mechanisms are added.
Past that point breadth stops buying heat, and the honest answer to "how do we
grow faster" becomes more capital or genuinely uncorrelated edges, not more
variants of the ones already held. `saturation()` reports it so the limit is a
number rather than a surprise.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

GROWTH_VERSION = "growth-2026-08-18-a"

#: THE DRAWDOWN THE PRINCIPAL WILL SIT THROUGH. The single risk input: per-trade
#: size and the heat budget are both solved from it, so there is one number to
#: argue about rather than two that can silently diverge. A default, overridable
#: on every call — nothing downstream may bake it in.
MAX_DRAWDOWN_TOLERANCE = 0.35

#: Overlapping days a PAIR of mechanisms needs before its correlation is used.
#: Below this the estimate is noise, and a noisy correlation near zero is
#: indistinguishable from genuine independence — the error that raises leverage.
MIN_PAIR_OVERLAP = 20

#: One-sided 95%.
_Z = 1.645

#: Base total heat with no measured breadth. The floor the ladder climbs from.
BASE_HEAT = 0.0381


# --------------------------------------------------------------- per-trade size

def risk_per_trade(tolerance: float = MAX_DRAWDOWN_TOLERANCE,
                   dd_r: float = 30.0) -> float:
    """The risk fraction that spends exactly `tolerance` over a `dd_r` R drawdown.

    A book suffering dd_r R of drawdown at per-trade risk q loses about
    1 − (1−q)^dd_r of equity. Inverting gives q* = 1 − (1−tol)^(1/dd_r): every
    basis point the stated tolerance allows, stopping precisely where the
    principal said to stop. `dd_r` is a MEASUREMENT of the book, not a constant,
    so this function takes it rather than owning it.
    """
    if not 0 < tolerance < 1:
        raise ValueError(f"tolerance {tolerance} must be in (0, 1)")
    if dd_r <= 0:
        raise ValueError(f"dd_r {dd_r} must be positive — a book with no measured "
                         f"drawdown has no basis for a risk fraction")
    return 1.0 - (1.0 - tolerance) ** (1.0 / dd_r)


def worst_drawdown_r(r_multiples: Sequence[float]) -> float:
    """Worst peak-to-trough of the cumulative R curve. The book's own number.

    In R rather than in currency so it is invariant to the size it was collected
    at — the whole point is to solve for size from it.
    """
    peak, worst, cum = 0.0, 0.0, 0.0
    for r in r_multiples:
        if not math.isfinite(r):
            continue
        cum += float(r)
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def log_growth(q: float, r_multiples: Sequence[float]) -> float:
    """E[ln(1 + qR)] — the geometric rate the account actually compounds at.

    Not the arithmetic mean. The gap between the two is the entire reason a book
    can look excellent and shrink.
    """
    vals = [float(r) for r in r_multiples if math.isfinite(r)]
    if not vals:
        return 0.0
    tot = 0.0
    for r in vals:
        x = 1.0 + q * r
        if x <= 0:
            return float("-inf")        # ruin on this path; not a small number
        tot += math.log(x)
    return tot / len(vals)


def kelly_optimum(r_multiples: Sequence[float], hi: float = 0.95,
                  steps: int = 400) -> tuple[float, float]:
    """argmax of the log-growth curve, by scan. Returns (q*, growth at q*).

    A scan rather than a solver on purpose: the curve over empirical R-multiples
    is not guaranteed smooth or unimodal on a small sample, and a Newton step
    that lands past a ruin point returns −inf and a confident wrong answer.
    """
    vals = [float(r) for r in r_multiples if math.isfinite(r)]
    if not vals:
        return 0.0, 0.0
    best_q, best_g = 0.0, 0.0
    for i in range(1, steps + 1):
        q = hi * i / steps
        g = log_growth(q, vals)
        if g > best_g:
            best_q, best_g = q, g
    return best_q, best_g


@dataclass
class HalfEdgeCheck:
    """Would the recommendation still compound if the edge were half as good?"""
    q_recommended: float
    q_optimum_measured: float
    q_optimum_half_edge: float
    growth_at_recommended: float
    growth_at_half_edge: float
    safe: bool
    why: str

    def render(self) -> str:
        return (f"  recommended q         {self.q_recommended:.4%}\n"
                f"  measured optimum      {self.q_optimum_measured:.4%}\n"
                f"  half-edge optimum     {self.q_optimum_half_edge:.4%}\n"
                f"  {'SAFE' if self.safe else 'PAST THE CLIFF'}: {self.why}")


def halve_edge(r_multiples: Sequence[float]) -> list:
    """The same book with half the expected value and the SAME risk.

    Subtracting half the mean from every observation, NOT scaling every R by
    0.5. Scaling halves the losses too, which is not a worse edge — it is the
    same edge at half the volatility, and its Kelly optimum is HIGHER, not
    lower. This module's own test caught that: the "degraded" book came back
    with an optimum twice the original and the guard cheerfully approved full
    Kelly.

    A loss is −1R by construction on this desk; edge degradation shows up in the
    wins and the hit rate, so a location shift is the transformation that
    actually models it.
    """
    vals = [float(r) for r in r_multiples if math.isfinite(r)]
    if not vals:
        return []
    shift = 0.5 * (sum(vals) / len(vals))
    return [r - shift for r in vals]


def half_edge_check(q: float, r_multiples: Sequence[float]) -> HalfEdgeCheck:
    """THE GUARD. Recompute the optimum with the expected value halved.

    The measured edge is biased upward — the mechanisms in the ledger are the
    ones that survived selection. Sizing at the measured peak bets that an
    in-sample number is exact. Sizing below the HALF-edge peak survives the edge
    being twice as bad as it looks, and still compounds faster than a timid
    fraction would.
    """
    vals = [float(r) for r in r_multiples if math.isfinite(r)]
    q_full, _ = kelly_optimum(vals)
    half = halve_edge(vals)
    q_half, _ = kelly_optimum(half)
    g_rec = log_growth(q, vals)
    g_half = log_growth(q, half)
    safe = q <= q_half and g_half > 0
    if not vals:
        return HalfEdgeCheck(q, 0.0, 0.0, 0.0, 0.0, False,
                             "no resolved R-multiples; nothing supports any size")
    why = ("the recommendation sits below the optimum even if the true edge is "
           "half the measured one, so it compounds under both."
           if safe else
           f"at half the measured edge this size compounds at "
           f"{g_half:+.5f}/trade and the optimum is {q_half:.4%}. Past the peak "
           f"more size is LESS money, not merely riskier money.")
    return HalfEdgeCheck(q, q_full, q_half, g_rec, g_half, safe, why)


# ------------------------------------------------------------- effective breadth

def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None                     # a constant series has no correlation, not zero
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def daily_returns(rows: Iterable[dict], key: str = "mechanism",
                  value_key: str = "realised_r",
                  time_key: str = "ts") -> dict:
    """Ledger rows -> {mechanism: {date: summed R}}.

    Same-day trades in one mechanism are SUMMED, because daily P&L is what
    correlates with another mechanism's daily P&L. Days absent from a
    mechanism's map are ABSENT, not zero — writing 0.0 for a day a mechanism did
    not trade deflates every correlation and manufactures diversification that
    does not exist.
    """
    out: dict = defaultdict(dict)
    for r in rows:
        name, ts = r.get(key), r.get(time_key) or r.get("time")
        if not name or not ts:
            continue
        try:
            v = float(r.get(value_key))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        day = str(ts)[:10]
        out[name][day] = out[name].get(day, 0.0) + v
    return dict(out)


def mean_pairwise_corr(series: dict,
                       min_overlap: int = MIN_PAIR_OVERLAP) -> tuple:
    """Fisher-z mean of pairwise correlations, and its upper 95% bound.

    THE UPPER BOUND, NEVER THE POINT ESTIMATE. Correlations rise in exactly the
    regime where the risk budget would be spent, and a sample mean is a point
    estimate from whatever regime happened to be sampled. Taking the growth the
    evidence supports at the PESSIMISTIC end of the correlation estimate is the
    difference between aggression and optimism.
    """
    names = sorted(series)
    zs: list[float] = []
    smallest = 0
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(set(series[a]) & set(series[b]))
            if len(common) < min_overlap:
                continue                      # absent overlap, not zero correlation
            r = _pearson([series[a][d] for d in common], [series[b][d] for d in common])
            if r is None:
                continue
            r = max(min(r, 0.999999), -0.999999)      # arctanh undefined at +-1
            zs.append(math.atanh(r))
            smallest = len(common) if smallest == 0 else min(smallest, len(common))
    if not zs:
        return None, 0, 0
    z_bar = sum(zs) / len(zs)
    # Bound from the THINNEST contributing pair, and deliberately not shrunk by
    # sqrt(n_pairs): pairwise correlations inside one book are themselves
    # dependent, so treating them as independent samples would narrow the
    # interval on a false assumption, in the direction that raises leverage.
    se = 1.0 / math.sqrt(max(smallest - 3, 1))
    return math.tanh(z_bar + _Z * se), len(zs), smallest


def effective_bets(n: int, rho: float) -> float:
    """k_eff = N / (1 + (N−1)ρ), clamped to [1, N]."""
    if n <= 1:
        return 1.0
    rho = max(min(float(rho), 1.0), -1.0 / (n - 1) + 1e-9)
    return max(1.0, min(float(n), n / (1.0 + (n - 1) * rho)))


def saturation(rho: float) -> Optional[float]:
    """The ceiling k_eff approaches as N grows: 1/ρ.

    Reported so the limit on breadth is a number the desk has seen rather than a
    wall it discovers. Past it, adding mechanisms buys no heat and the honest
    answer to "grow faster" is more capital or genuinely uncorrelated edges.
    """
    return (1.0 / rho) if rho > 0 else None


def measure_k_eff(rows: Iterable[dict],
                  min_overlap: int = MIN_PAIR_OVERLAP) -> tuple:
    """Effective independent bets from realised daily returns. None if unmeasurable.

    The reason string is ALWAYS populated: a budget that silently widened would
    be indistinguishable from one that was never measured.
    """
    series = daily_returns(rows)
    n = len(series)
    if n < 2:
        return None, (f"k_eff UNMEASURED: {n} mechanism(s) with resolved returns; "
                      f"correlation needs two. Heat stays at base.")
    rho, pairs, overlap = mean_pairwise_corr(series, min_overlap)
    if rho is None:
        return None, (f"k_eff UNMEASURED: no pair has {min_overlap} overlapping "
                      f"trading days yet ({n} mechanisms). Heat stays at base.")
    k = effective_bets(n, rho)
    sat = saturation(rho)
    tail = (f"; ceiling {sat:.1f} however many mechanisms are added"
            if sat and sat < n * 2 else "")
    return k, (f"k_eff {k:.2f} from {n} mechanisms, {pairs} pair(s), thinnest "
               f"overlap {overlap}d, rho<={rho:.3f} (95% upper bound, not the "
               f"point estimate){tail}")


def heat_budget(k_eff: Optional[float], base: float = BASE_HEAT) -> tuple:
    """Total portfolio heat, scaled by sqrt(effective breadth).

    None routes to base. NOT-YET-MEASURED MUST NEVER READ AS INDEPENDENT — that
    is precisely how a correlated book sizes like a diversified one and finds
    out during the drawdown instead of before it.
    """
    if k_eff is None:
        return base, ("no measured breadth; heat at base. Absence of a "
                      "correlation measurement is not evidence of independence.")
    k = max(1.0, float(k_eff))
    h = base * math.sqrt(k)
    return h, (f"heat {h:.2%} = base {base:.2%} x sqrt(k_eff {k:.2f}). Breadth "
               f"earned, budget widened.")


# ------------------------------------------------------------ the recommendation

@dataclass
class Recommendation:
    q: float
    heat: float
    k_eff: Optional[float]
    dd_r: float
    tolerance: float
    check: HalfEdgeCheck
    min_equity_for_expression: Optional[float]
    why: list

    @property
    def actionable(self) -> bool:
        return self.check.safe and self.q > 0

    def render(self) -> str:
        lines = [f"GROWTH RECOMMENDATION  ({GROWTH_VERSION})",
                 f"  tolerance (stated)    {self.tolerance:.1%} drawdown",
                 f"  book worst drawdown   {self.dd_r:.1f}R  (measured, not assumed)",
                 f"  risk per trade        {self.q:.4%}  DERIVED, not chosen",
                 f"  total heat            {self.heat:.2%}",
                 f"  k_eff                 "
                 + ("UNMEASURED" if self.k_eff is None else f"{self.k_eff:.2f}"),
                 ""]
        lines.append(self.check.render())
        if self.min_equity_for_expression:
            lines += ["",
                      f"  EXPRESSIBLE FROM     {self.min_equity_for_expression:,.0f} "
                      f"account currency",
                      "    Below this the venue's lot granularity forces a larger "
                      "realised risk than the recommendation. The fix is equity, "
                      "not a smaller q."]
        lines += [""] + [f"  {w}" for w in self.why]
        return "\n".join(lines)


def min_equity_for(q: float, r_per_trade_price: float, contract_size: float,
                   min_lot: float = 0.01) -> Optional[float]:
    """Smallest equity at which `q` is expressible at the venue's lot grain.

    A recommendation the account cannot express is not conservative — the floor
    forces a LARGER realised risk than intended, and the desk runs hotter than
    its own policy while believing it runs at policy. Reported rather than
    silently rounded, because the fix is equity rather than a smaller q.
    """
    if q <= 0 or r_per_trade_price <= 0 or contract_size <= 0:
        return None
    risk_at_min_lot = min_lot * contract_size * r_per_trade_price
    return risk_at_min_lot / q


def recommend(r_multiples: Sequence[float], rows: Iterable[dict] = (),
              tolerance: float = MAX_DRAWDOWN_TOLERANCE,
              base_heat: float = BASE_HEAT,
              r_per_trade_price: float = 0.0,
              contract_size: float = 100.0) -> Recommendation:
    """Everything solved at once, from the ledger and one stated tolerance.

    Nothing here is a literal. Change the tolerance and every number moves;
    resolve more trades and the drawdown estimate, the breadth and the budget all
    move with them. That is the constitution's requirement made operational.
    """
    vals = [float(r) for r in r_multiples if math.isfinite(r)]
    why: list = []
    dd = worst_drawdown_r(vals)
    if dd <= 0:
        # FAILS CLOSED. No observed drawdown is not a drawdown-free book, it is
        # a book nobody has watched long enough. Sizing from it would divide by
        # optimism.
        return Recommendation(
            0.0, 0.0, None, 0.0, tolerance,
            HalfEdgeCheck(0.0, 0.0, 0.0, 0.0, 0.0, False,
                          "no measured drawdown yet"),
            None,
            ["NO RECOMMENDATION: the book has produced no peak-to-trough "
             "drawdown to solve against. That is not a drawdown-free book, it "
             "is a book nobody has watched long enough."])
    q = risk_per_trade(tolerance, dd)
    why.append(f"q solved from {tolerance:.0%} tolerance over a measured {dd:.1f}R "
               f"drawdown: q = 1 - (1-tol)^(1/dd_r).")

    k, k_why = measure_k_eff(list(rows))
    why.append(k_why)
    heat, h_why = heat_budget(k, base_heat)
    why.append(h_why)

    check = half_edge_check(q, vals)
    if not check.safe:
        # The tolerance implies a size the edge cannot support. Retreat to the
        # half-edge optimum and SAY SO, rather than quietly serving the number
        # the tolerance asked for.
        q = min(q, check.q_optimum_half_edge)
        check = half_edge_check(q, vals)
        why.append("q reduced to the half-edge optimum: the stated tolerance "
                   "implied a size past the point where more size is less money. "
                   "The tolerance is not the binding constraint here, the edge is.")

    return Recommendation(
        q, heat, k, dd, tolerance, check,
        min_equity_for(q, r_per_trade_price, contract_size) if r_per_trade_price else None,
        why)
