"""Sizing is where an optimistic estimate turns into a real loss, so most of
these tests check that the module refuses rather than that it computes.
"""
from __future__ import annotations

import math

import pytest

from golddesk.growth import (
    BASE_HEAT, MAX_DRAWDOWN_TOLERANCE, Recommendation, daily_returns,
    effective_bets, half_edge_check, heat_budget, kelly_optimum, log_growth,
    mean_pairwise_corr, measure_k_eff, min_equity_for, recommend,
    risk_per_trade, saturation, worst_drawdown_r)


# ------------------------------------------------------ nothing is hardcoded

def test_q_moves_with_the_stated_tolerance():
    """The constitution's requirement: change the tolerance, every number moves."""
    a = risk_per_trade(0.20, 30.0)
    b = risk_per_trade(0.35, 30.0)
    assert b > a


def test_q_moves_with_the_measured_drawdown():
    assert risk_per_trade(0.35, 20.0) > risk_per_trade(0.35, 40.0)


def test_q_spends_exactly_the_tolerance_over_the_drawdown():
    q, dd, tol = risk_per_trade(0.35, 33.7), 33.7, 0.35
    assert abs((1 - (1 - q) ** dd) - tol) < 1e-9


def test_an_impossible_tolerance_is_refused():
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            risk_per_trade(bad, 30.0)


def test_a_book_with_no_measured_drawdown_gets_no_size():
    """Not a drawdown-free book — a book nobody has watched long enough."""
    with pytest.raises(ValueError):
        risk_per_trade(0.35, 0.0)


# --------------------------------------------------------------- the drawdown

def test_the_drawdown_is_peak_to_trough_not_worst_trade():
    assert worst_drawdown_r([1, -1, -1, -1, 3]) == 3.0


def test_a_monotone_winner_has_no_drawdown():
    assert worst_drawdown_r([1, 1, 1]) == 0.0


def test_drawdown_ignores_non_finite_rows():
    assert worst_drawdown_r([1, float("nan"), -2]) == 2.0


# ------------------------------------------------------ growth, not arithmetic

def test_log_growth_is_geometric_not_arithmetic():
    """A book can have positive mean R and shrink. That gap is the whole point."""
    rs = [1.0, -1.0] * 50
    assert sum(rs) == 0
    assert log_growth(0.5, rs) < 0


def test_growth_falls_past_the_optimum():
    """More size past the peak is LESS money, not merely riskier money."""
    rs = [2.0, -1.0] * 100
    q_star, g_star = kelly_optimum(rs)
    assert log_growth(q_star * 1.8, rs) < g_star


def test_ruin_reads_as_negative_infinity_not_a_small_number():
    assert log_growth(0.9, [-2.0]) == float("-inf")


def test_kelly_finds_the_known_optimum_of_a_simple_book():
    """p=0.5 at +2R/-1R has Kelly 0.25 by the closed form."""
    q, _ = kelly_optimum([2.0, -1.0] * 200)
    assert 0.20 < q < 0.30


def test_no_resolved_trades_means_no_optimum():
    assert kelly_optimum([]) == (0.0, 0.0)


# ---------------------------------------------------------- the half-edge guard

def test_a_size_past_the_half_edge_optimum_is_flagged():
    """Sizing at the measured peak bets that an in-sample number is exact."""
    rs = [2.0, -1.0] * 200
    q_full, _ = kelly_optimum(rs)
    assert not half_edge_check(q_full, rs).safe


def test_a_conservative_size_survives_the_edge_being_halved():
    rs = [2.0, -1.0] * 200
    assert half_edge_check(0.02, rs).safe


def test_the_flag_explains_that_more_size_is_less_money():
    rs = [2.0, -1.0] * 200
    q_full, _ = kelly_optimum(rs)
    assert "LESS money" in half_edge_check(q_full, rs).why


def test_no_evidence_is_not_safe():
    assert not half_edge_check(0.01, []).safe


def test_halving_the_edge_is_a_location_shift_not_a_rescale():
    """THE BUG THIS TEST EXISTS FOR. Scaling every R by 0.5 halves the losses
    too, which is the same edge at half the volatility — its Kelly optimum is
    HIGHER, so the guard approved full Kelly on a book it was meant to reject.
    A loss is -1R by construction; degradation lives in the wins."""
    from golddesk.growth import halve_edge
    rs = [2.0, -1.0] * 200
    h = halve_edge(rs)
    mean = sum(rs) / len(rs)
    assert abs((sum(h) / len(h)) - mean / 2) < 1e-12, "expected value not halved"
    rescaled = [r * 0.5 for r in rs]
    assert kelly_optimum(rescaled)[0] > kelly_optimum(rs)[0], (
        "a rescale should RAISE the optimum — that is why it is the wrong model")
    assert kelly_optimum(h)[0] < kelly_optimum(rs)[0], (
        "a genuine edge halving must lower the optimum")


def test_the_loss_scale_survives_the_degradation():
    """If the worst case got smaller, the book was not degraded."""
    from golddesk.growth import halve_edge
    rs = [2.0, -1.0] * 50
    assert min(halve_edge(rs)) < min(rs), "losses got easier under 'degradation'"


# ------------------------------------------------------------ effective breadth

def test_identical_mechanisms_are_one_bet():
    assert effective_bets(5, 1.0) == pytest.approx(1.0)


def test_independent_mechanisms_are_n_bets():
    assert effective_bets(5, 0.0) == pytest.approx(5.0)


def test_k_eff_saturates_at_one_over_rho():
    """The ceiling nobody can engineer away: past it, breadth buys no heat."""
    assert saturation(0.165) == pytest.approx(6.06, abs=0.01)
    assert effective_bets(500, 0.165) < 6.2


def test_saturation_is_undefined_at_zero_correlation():
    assert saturation(0.0) is None


def test_absent_days_are_absent_not_zero():
    """Writing 0.0 for a day a mechanism did not trade deflates every
    correlation and manufactures diversification that does not exist."""
    rows = [{"mechanism": "a", "ts": "2026-01-01", "realised_r": 1.0},
            {"mechanism": "b", "ts": "2026-01-02", "realised_r": 1.0}]
    s = daily_returns(rows)
    assert "2026-01-02" not in s["a"] and "2026-01-01" not in s["b"]


def test_same_day_trades_in_one_mechanism_are_summed():
    rows = [{"mechanism": "a", "ts": "2026-01-01", "realised_r": 1.0},
            {"mechanism": "a", "ts": "2026-01-01", "realised_r": -0.5}]
    assert daily_returns(rows)["a"]["2026-01-01"] == 0.5


def test_a_thin_overlap_contributes_nothing_rather_than_a_convenient_zero():
    rows = ([{"mechanism": "a", "ts": f"2026-01-{d:02d}", "realised_r": 1.0}
             for d in range(1, 6)]
            + [{"mechanism": "b", "ts": f"2026-01-{d:02d}", "realised_r": 1.0}
               for d in range(1, 6)])
    k, why = measure_k_eff(rows)
    assert k is None and "overlapping trading days" in why


def test_the_upper_bound_is_used_not_the_point_estimate():
    """Correlations rise in exactly the regime where the budget would be spent."""
    import random
    rng = random.Random(3)
    s = {"a": {}, "b": {}}
    for d in range(1, 61):
        x = rng.gauss(0, 1)
        s["a"][f"2026-03-{d:02d}"] = x
        s["b"][f"2026-03-{d:02d}"] = x * 0.3 + rng.gauss(0, 1)
    rho_upper, pairs, overlap = mean_pairwise_corr(s)
    raw = _raw_corr([s["a"][k] for k in sorted(s["a"])],
                    [s["b"][k] for k in sorted(s["b"])])
    assert rho_upper > raw, "the point estimate was used"


def _raw_corr(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


def test_one_mechanism_is_unmeasurable():
    k, why = measure_k_eff([{"mechanism": "a", "ts": "2026-01-01", "realised_r": 1.0}])
    assert k is None and "needs two" in why


# ------------------------------------------------------------- the heat ladder

def test_unmeasured_breadth_stays_at_base():
    """THE FAILURE THAT MATTERS. Not-yet-measured must never read as
    independent — that is how a correlated book sizes like a diversified one."""
    h, why = heat_budget(None)
    assert h == BASE_HEAT
    assert "not evidence of independence" in why


def test_earned_breadth_widens_the_budget():
    """The compounding engine: five independent mechanisms are safer at 6% than
    three correlated ones at 4%."""
    h4, _ = heat_budget(4.0)
    assert h4 == pytest.approx(BASE_HEAT * 2)
    assert h4 > heat_budget(1.0)[0]


def test_the_ladder_is_not_dead_code():
    """On the MT5 desk nothing supplied k_eff, so the budget returned base on
    every call for the life of the desk — a constant with extra steps that
    permanently capped compounding."""
    assert heat_budget(9.0)[0] == pytest.approx(BASE_HEAT * 3)


# ------------------------------------------------------------- expressibility

def test_the_minimum_equity_for_a_recommendation_is_reported():
    """A recommendation the account cannot express is not conservative: the lot
    floor forces a LARGER realised risk than intended."""
    e = min_equity_for(q=0.0127, r_per_trade_price=6.0, contract_size=100.0)
    assert e == pytest.approx(0.01 * 100 * 6.0 / 0.0127)


def test_no_price_means_no_expressibility_claim():
    assert min_equity_for(0.0127, 0.0, 100.0) is None


# ------------------------------------------------------------ the whole thing

def test_a_book_with_no_drawdown_gets_no_recommendation():
    rec = recommend([1.0, 1.0, 1.0])
    assert rec.q == 0.0 and not rec.actionable
    assert "watched long enough" in rec.why[0]


def test_an_empty_book_gets_no_recommendation():
    assert not recommend([]).actionable


def test_the_recommendation_retreats_when_the_edge_cannot_support_the_tolerance():
    """The tolerance is not always the binding constraint. When the edge is, the
    module says so rather than quietly serving the number asked for."""
    rs = [0.3, -1.0] * 60                 # a poor book: tolerance implies too much
    rec = recommend(rs, tolerance=0.35)
    assert rec.q <= rec.check.q_optimum_half_edge + 1e-12
    assert any("the edge is" in w for w in rec.why)


def test_a_good_book_gets_a_derived_size_that_survives_the_half_edge_test():
    rs = [2.0, -1.0] * 100 + [-1.0] * 20
    rec = recommend(rs, tolerance=0.35)
    assert rec.q > 0 and rec.check.safe


def test_every_number_moves_with_the_tolerance():
    rs = [2.0, -1.0] * 100 + [-1.0] * 20
    a = recommend(rs, tolerance=0.15)
    b = recommend(rs, tolerance=0.35)
    assert b.q > a.q, "the tolerance did not reach the recommendation"


def test_the_render_says_the_size_was_derived_not_chosen():
    rs = [2.0, -1.0] * 100 + [-1.0] * 20
    txt = recommend(rs, tolerance=0.35).render()
    assert "DERIVED, not chosen" in txt
    assert "measured, not assumed" in txt


def test_breadth_reaches_the_recommendation():
    rs = [2.0, -1.0] * 100 + [-1.0] * 20
    rows = []
    import random
    rng = random.Random(5)
    for m in ("a", "b", "c"):
        for d in range(1, 41):
            rows.append({"mechanism": m, "ts": f"2026-05-{d:02d}",
                         "realised_r": rng.gauss(0, 1)})
    rec = recommend(rs, rows=rows, tolerance=0.35)
    assert rec.k_eff is not None and rec.heat > BASE_HEAT
