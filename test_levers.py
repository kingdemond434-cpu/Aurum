"""Sizing is a multiplier on edge, not a source of it. Most of these tests are
about the module refusing to pretend otherwise.
"""
from __future__ import annotations

import pytest

from golddesk.levers import (
    IMPLAUSIBLE_GROWTH, analyse, growth, keff, max_safe_q)


# --------------------------------------------------- sizing cannot create edge

def test_growth_peaks_and_then_falls_in_q():
    """Past the peak, more size is LESS money — not riskier money, less."""
    g = [growth(q / 1000, 0.18, 500) for q in range(5, 400, 5)]
    peak = max(range(len(g)), key=lambda i: g[i])
    assert 0 < peak < len(g) - 1, "no interior optimum; the curve is not a curve"
    assert g[-1] < g[peak]


def test_a_zero_edge_book_cannot_be_sized_into_profit():
    """The whole point. No heat cap makes a coin flip pay."""
    for q in (0.005, 0.02, 0.05, 0.10):
        assert growth(q, 0.0, 500) <= 0


def test_a_negative_edge_gets_worse_with_size():
    small = growth(0.005, -0.05, 500)
    large = growth(0.05, -0.05, 500)
    assert large < small < 0


def test_ruin_is_negative_infinity_not_a_small_number():
    assert growth(1.5, 0.2, 100) == float("-inf")


# ------------------------------------------------------------- breadth

def test_keff_saturates_at_one_over_rho():
    """The ceiling nobody can engineer away: sleeves beyond it buy frequency and
    no information."""
    assert keff(500, 0.165) < 6.2
    assert keff(12, 0.165) == pytest.approx(4.26, abs=0.05)


def test_identical_sleeves_are_one_bet():
    assert keff(12, 1.0) == pytest.approx(1.0)


def test_independent_sleeves_are_n_bets():
    assert keff(12, 0.0) == pytest.approx(12.0)


# ------------------------------------------------------------- the levers

def test_adding_a_correlated_sleeve_pays_less_than_lowering_correlation_when_saturated():
    """At twelve sleeves against a 6.1 ceiling, the twelfth is nearly free of
    information and a real diversifier is worth more."""
    r = analyse(n_sleeves=12, mu=0.179, n_per_year=1407, rho=0.165)
    by = {l.name: l for l in r.levers}
    assert by["+1 sleeve (same family)"].delta < by["rho sleeve correlation"].delta


def test_adding_a_sleeve_pays_MOST_when_breadth_is_scarce():
    """At three sleeves k_eff is 2.26 against the same ceiling, so the fourth
    still carries real information."""
    r = analyse(n_sleeves=3, mu=0.159, n_per_year=666, rho=0.165)
    best = r.ranked()[0]
    assert best.name.startswith("+1 sleeve")


def test_every_lever_is_nudged_by_the_same_relative_amount():
    """Nudging q by an absolute 1% and rho by an absolute 0.1 compares a small
    change to a huge one and ranks whichever happened to be larger."""
    r = analyse(n_sleeves=12, mu=0.179, n_per_year=1407, rho=0.165, nudge=0.10)
    for l in r.levers:
        if l.name.startswith(("q ", "n ", "mu")):
            assert l.nudged == pytest.approx(l.current * 1.10, rel=1e-6)


def test_lowering_correlation_is_the_improvement_direction():
    r = analyse(n_sleeves=12, mu=0.179, n_per_year=1407, rho=0.165)
    rho = next(l for l in r.levers if l.name.startswith("rho"))
    assert rho.nudged < rho.current


def test_an_exhausted_size_lever_is_named_as_such():
    """Raising the heat cap past the optimum reduces growth, and the lever
    everybody reaches for first is the one most often already spent."""
    r = analyse(n_sleeves=1, mu=0.18, n_per_year=500, rho=0.0, base_heat=0.60)
    q = next(l for l in r.levers if l.name.startswith("q "))
    assert q.exhausted
    assert "already past its optimum" in r.binding


# --------------------------------------------- the ranking outlives a bad input

def test_an_implausible_level_is_flagged_rather_than_reported_as_a_forecast():
    r = analyse(n_sleeves=12, mu=0.179, n_per_year=1407, rho=0.165)
    if r.base_growth > IMPLAUSIBLE_GROWTH:
        assert "NOT a forecast" in r.render()
        assert "Read the order, discard the percentages" in r.render()


def test_inflating_the_expectancy_does_not_change_the_ranking():
    """Every lever is evaluated against the same mu, so an overstated one lifts
    them together. That is why the order is usable when the levels are not."""
    a = analyse(n_sleeves=12, mu=0.179, n_per_year=1407, rho=0.165)
    b = analyse(n_sleeves=12, mu=0.179 * 1.5, n_per_year=1407, rho=0.165)
    assert [l.name for l in a.ranked()] == [l.name for l in b.ranked()]


# ------------------------------------------------------ the defensible size

def test_the_half_edge_optimum_is_below_the_measured_one():
    """The sleeves in the ledger are the ones that survived selection, so the
    peak computed from them sits to the right of the true peak."""
    qf, qh, _ = max_safe_q(0.179, 1407)
    assert qh < qf


def test_the_current_heat_cap_is_compared_against_that_ceiling():
    from golddesk.growth import heat_budget
    _, qh, _ = max_safe_q(0.179, 1407)
    h, _ = heat_budget(keff(12, 0.165))
    assert h / 12 < qh, "the heat cap should sit under the half-edge ceiling"
