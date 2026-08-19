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

    PREFER solve_heat() WHERE DAILY RETURNS EXIST. This function multiplies a
    CONSTANT by sqrt(k_eff), and that constant is a ceiling nobody derived — it
    caps the book at 3.81% x sqrt(k) forever regardless of what the book
    actually does. solve_heat measures the answer instead of assuming it.
    """
    if k_eff is None:
        return base, ("no measured breadth; heat at base. Absence of a "
                      "correlation measurement is not evidence of independence.")
    k = max(1.0, float(k_eff))
    h = base * math.sqrt(k)
    return h, (f"heat {h:.2%} = base {base:.2%} x sqrt(k_eff {k:.2f}). Breadth "
               f"earned, budget widened.")


def solve_heat(daily_returns: Sequence[float],
               tolerance: float = MAX_DRAWDOWN_TOLERANCE,
               half_edge: bool = True, hi: float = 0.60,
               iters: int = 60) -> tuple:
    """Total heat that puts THIS book at exactly `tolerance` drawdown. No constant.

    WHY THIS REPLACES A HARDCODED BASE

    BASE_HEAT is 3.81%, and nothing derives it. Multiplying it by sqrt(k_eff)
    scales a number that was never measured, so the book is permanently capped
    by a literal — it cannot grow past 3.81% x sqrt(k) however good it gets, and
    it cannot shrink below it however bad. Both directions are wrong.

    This solves instead. Bisect for the heat whose worst drawdown on the book's
    OWN daily series equals the stated tolerance. Everything then moves on its
    own: a book that adds genuinely independent sleeves has shallower drawdowns,
    so the solve returns MORE heat without anyone widening a constant; a book
    whose edge decays has deeper ones and the solve returns less on the next
    call. The only input is the drawdown the principal will sit through.

    HALF-EDGE BY DEFAULT, AND THAT IS WHAT MAKES IT SAFE TO BE AGGRESSIVE

    The measured edge is biased upward — this book is the survivor of a search.
    Solving on the raw series would size to a drawdown that only holds if the
    in-sample number is exact. Solving on the half-edge series (a LOCATION
    SHIFT, not a rescale) returns the heat that still respects the tolerance
    when the edge turns out half as good, which is the aggressive choice rather
    than the timid one: it is the largest size that survives being wrong.

    Returns (heat, why). Heat of 0.0 when the series cannot support any size.
    """
    vals = [float(r) for r in daily_returns if math.isfinite(float(r))]
    if len(vals) < 30:
        return 0.0, (f"only {len(vals)} daily observations; a drawdown solved on "
                     f"this is noise. No size authorised — the fix is more days, "
                     f"not a fallback constant.")
    if not 0 < tolerance < 1:
        raise ValueError(f"tolerance {tolerance} must be in (0, 1)")
    shift = 0.5 * (sum(vals) / len(vals)) if half_edge else 0.0
    v = [r - shift for r in vals]

    # A DRAWDOWN SOLVE IS NOT A PROFITABILITY TEST, and on its own it will
    # cheerfully size a book that only loses. Bisecting on drawdown alone finds
    # that a monotonically losing series stays inside ANY tolerance at a small
    # enough q — a 200-day book of straight losses came back authorised at
    # 0.21% heat, which is not safety, it is losing money slowly on purpose.
    # Expectancy has to be checked separately and first.
    mean_v = sum(v) / len(v)
    if mean_v <= 0:
        return 0.0, (f"mean daily return {mean_v:+.5f} at "
                     + ("half edge" if half_edge else "full edge")
                     + "; no size is correct for a book with no expectancy. A "
                       "drawdown solve would still return a small positive heat "
                       "here, because losing slowly stays inside any tolerance.")

    def worst_dd(q: float) -> float:
        eq, peak, worst = 1.0, 1.0, 0.0
        for r in v:
            eq *= (1.0 + q * r)
            if eq <= 0:
                return 1.0                      # ruin: treat as total drawdown
            peak = max(peak, eq)
            worst = max(worst, 1.0 - eq / peak)
        return worst

    lo, high = 0.0, float(hi)
    for _ in range(iters):
        mid = 0.5 * (lo + high)
        if worst_dd(mid) > tolerance:
            high = mid
        else:
            lo = mid
    if lo <= 1e-6:
        return 0.0, (f"no positive heat keeps this book inside a {tolerance:.0%} "
                     f"drawdown; its own history is worse than the tolerance.")
    return lo, (f"heat {lo:.2%} SOLVED from {len(vals)} days at a {tolerance:.0%} "
                f"tolerance"
                + (" on the half-edge series" if half_edge else " IN-SAMPLE")
                + ". No constant: rises as the book earns breadth, falls as it "
                  "decays, on every call.")


def lot_ladder(r_multiples: Sequence[float], risk_per_lot: Sequence[float],
               equity: float, min_lot_risk: Optional[float] = None,
               paths: int = 3000, seed: int = 7,
               steps: Sequence[float] = (2000, 1000, 600, 400, 300, 200, 150,
                                         100, 75, 50, 35),
               max_lots: int = 200) -> tuple:
    """When to add another minimum lot, solved for maximum E[log wealth].

    THE LEVER NOBODY SETS, AND IT IS WORTH MORE THAN MOST EDGES AT SMALL SIZE

    A venue sells lots in steps of 0.01 and nothing forces a policy for climbing
    them, so the default is the worst one: hold 0.01 for ever. On the JPY asia
    trio from EUR300 that default scores E[log] 0.188; adding one lot per EUR100
    of equity scores 0.331. Seventy-six percent more compounding from a rule, not
    an edge — no new data, no new sleeve, no extra drawdown tolerance.

    WHY IT IS SOLVED RATHER THAN CHOSEN, AND WHY THE OBJECTIVE IS E[log]

    The curve has a sharp peak and a cliff just past it. Stepping every EUR50 on
    that same book still shows a median of EUR308 and a 3.1% chance of ruin — and
    those two facts are not comparable, because ruin removes ALL future
    compounding rather than one year of it. Ranked on the median it looks like a
    modest cost; ranked on E[log] it is -0.595 against +0.331, which is the
    difference between growing and being finished. Median is what makes an
    over-levered ladder look survivable; E[log] is what the account actually
    experiences.

    A LADDER IS NOT FIXED-FRACTIONAL AND THE GAP IS THE WHOLE PROBLEM

    Fixed-fractional risk falls automatically as equity falls. A lot ladder does
    not: below the first step you are stuck at one minimum lot whose risk as a
    FRACTION of equity rises as the account shrinks. That is why the cliff exists
    at all, and why the solve has to be run against the book's own trade
    distribution rather than reasoned about.

    Returns (step_equity, report). A step of 0.0 means never add — which is the
    correct answer when the minimum lot is already too large for the account.
    """
    rs = [float(r) for r in r_multiples if math.isfinite(float(r))]
    risk = [float(x) for x in risk_per_lot if math.isfinite(float(x)) and x > 0]
    if len(rs) < 100 or len(risk) != len(rs):
        return 0.0, (f"{len(rs)} resolved trades with {len(risk)} risk figures; "
                     f"a ladder solved on this is noise. Staying at one minimum "
                     f"lot is not a recommendation, it is the absence of one.")
    if equity <= 0:
        raise ValueError(f"equity {equity} must be positive")
    import random as _random
    shift = 0.5 * (sum(rs) / len(rs))         # half-edge, as everywhere else
    adj = [r - shift for r in rs]
    n = len(adj)
    floor = min_lot_risk if min_lot_risk is not None else 2.0

    def run(step: float) -> tuple:
        rng = _random.Random(seed)
        ruin, logs = 0, []
        for _ in range(paths):
            eq, dead = equity, False
            for _ in range(n):
                k = rng.randrange(n)
                lots = 1 if step <= 0 else max(1, min(max_lots, int(eq // step)))
                eq += adj[k] * risk[k] * lots
                if eq <= risk[k] * floor:
                    dead = True
                    break
            ruin += dead
            # A DEAD PATH IS NOT A SMALL NUMBER. Booking it at a floor keeps the
            # average finite while preserving the thing that matters: ruin costs
            # every future doubling, not one bad year.
            logs.append(math.log(max(eq, 1e-9) / equity) if not dead
                        else math.log(1e-9 / equity))
        return ruin / paths, sum(logs) / len(logs)

    results = []
    for st in (0.0,) + tuple(float(s) for s in steps):
        p, g = run(st)
        results.append((st, p, g))
    base = next(t for t in results if t[0] == 0.0)

    # TIES GO TO THE SIMPLER POLICY, and this is not cosmetic. A step LARGER
    # than the account never fires — at 300 equity a 600 step gives
    # int(300//600) = 0 lots, floored back to one — so it scores identically to
    # holding while being reported as a ladder. This module's own test caught
    # that: a book whose ticket was too large for the account came back
    # recommending "+1 lot per 600", which is advice to do exactly nothing,
    # dressed as advice to do something.
    tol = 1e-9 + 1e-3 * abs(base[2])
    best = max(results, key=lambda t: t[2])
    if best[2] - base[2] <= tol:
        best = base
    lines = [f"LOT LADDER  solved on {n} trades from {equity:,.0f}",
             f"{'step':<22}{'P(ruin)':>10}{'E[log]':>10}"]
    for st, p, g in results:
        mark = "  <- best" if st == best[0] else ""
        name = "hold one lot for ever" if st == 0 else f"+1 lot per {st:,.0f}"
        lines.append(f"{name:<22}{p:>9.1%}{g:>10.3f}{mark}")
    gain = (best[2] - base[2]) / abs(base[2]) if base[2] else float("nan")
    lines.append("")
    if best[0] == 0.0:
        lines.append(
            "  HOLD. No ladder beats staying at one minimum lot on this book. "
            "The smallest\n  ticket is already large against the account, so "
            "every step raises ruin\n  faster than it raises growth.")
    else:
        lines.append(f"  Add one minimum lot per {best[0]:,.0f} of equity: "
                     f"E[log] {base[2]:.3f} -> {best[2]:.3f}"
                     + (f" ({gain:+.0%})" if math.isfinite(gain) else ""))
        lines.append(
            "  This is a RULE, not an edge. It costs no data, no new sleeve "
            "and no extra\n  drawdown tolerance — only the discipline of not "
            "stepping sooner.")
    return best[0], "\n".join(lines)


def per_sleeve_heat(heat: float, expectancies: dict) -> dict:
    """Split total heat by measured edge, not evenly.

    EQUAL WEIGHTS ARE WHY BREADTH LOOKED DESTRUCTIVE. Splitting a fixed budget
    N ways forces the best sleeve to surrender size to fund the worst: a
    twelve-sleeve book cut its +0.21R gold sleeve to 0.42% in order to pay for a
    +0.01R sleeve. Weighting by expectancy keeps the good sleeve whole and gives
    the weak ones only what they earn, and a sleeve measured at or below zero
    gets nothing at all rather than a share.
    """
    pos = {k: max(float(v), 0.0) for k, v in expectancies.items()}
    tot = sum(pos.values())
    if tot <= 0:
        return {k: 0.0 for k in expectancies}
    return {k: heat * v / tot for k, v in pos.items()}


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


@dataclass
class FloorReport:
    """What the venue's minimum lot actually costs at a given equity.

    MARGIN AND RISK ARE DIFFERENT QUESTIONS AND CONFLATING THEM IS A REAL ERROR.
    Margin asks "will the broker let me open this?" — at 0.01 lots on gold with
    retail leverage the answer is yes at almost any funded balance, and anyone
    who has traded a small account knows it. Risk asks "what fraction of the
    account does the STOP cost?", and that is set by the stop distance, not by
    the lot size or the leverage.

    A desk that answers the margin question when it was asked the risk question
    sounds prudent and is simply wrong. So this reports both, separately, and
    lets the stop distance decide — because that is the variable that actually
    moves the answer.
    """
    equity: float
    stop_distance: float
    legs: int
    contract_size: float
    min_lot: float
    risk_per_leg: float
    risk_pct_per_leg: float
    total_heat_pct: float
    margin_per_leg: float
    margin_pct: float
    budget_pct: float

    @property
    def margin_binds(self) -> bool:
        return self.margin_pct > 50.0

    @property
    def within_budget(self) -> bool:
        return self.total_heat_pct <= self.budget_pct * 100.0

    def render(self) -> str:
        return (f"  stop {self.stop_distance:>5.1f}  "
                f"risk/leg {self.risk_per_leg:>6.2f} = {self.risk_pct_per_leg:>5.2f}%  "
                f"{self.legs} legs = {self.total_heat_pct:>5.2f}%  "
                f"margin {self.margin_pct:>5.1f}%  "
                f"{'OK' if self.within_budget else 'OVER BUDGET'}")


def floor_report(equity: float, stop_distance: float, legs: int = 1,
                 contract_size: float = 100.0, min_lot: float = 0.01,
                 leverage: float = 500.0, price: float = 4400.0,
                 budget: float = BASE_HEAT) -> FloorReport:
    """Risk and margin at the minimum lot, kept apart.

    `stop_distance` is in price units (dollars of gold). At 0.01 lots on a 100oz
    contract, one dollar of gold move is one dollar of P&L, so the stop distance
    IS the risk in account currency — which is why it, and not the lot size,
    decides whether a small account can carry the book.
    """
    units = min_lot * contract_size
    risk = units * stop_distance
    notional = units * price
    margin = notional / max(leverage, 1.0)
    pct = (lambda v: 100.0 * v / equity) if equity > 0 else (lambda v: float("inf"))
    return FloorReport(
        equity=equity, stop_distance=stop_distance, legs=legs,
        contract_size=contract_size, min_lot=min_lot,
        risk_per_leg=risk, risk_pct_per_leg=pct(risk),
        total_heat_pct=pct(risk * legs),
        margin_per_leg=margin, margin_pct=pct(margin * legs),
        budget_pct=budget)


def max_stop_for_budget(equity: float, legs: int = 1,
                        contract_size: float = 100.0, min_lot: float = 0.01,
                        budget: float = BASE_HEAT) -> float:
    """The widest stop that keeps `legs` legs inside the heat budget at min lot.

    The number the operator actually needs: not "can I trade this account" but
    "how tight does the stop have to be for this account to carry the book".
    """
    units = min_lot * contract_size * legs
    if units <= 0:
        return 0.0
    return budget * equity / units


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
