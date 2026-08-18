"""Asked "what will this earn", a number implies a precision nobody has. These
tests are about the ladder refusing to collapse into one.
"""
from __future__ import annotations

import pytest

from golddesk.projection import (
    ASIA_GOLD_MEASURED, Sleeve, deflate_expectancy, project)


def sleeve(exp=0.5, tpy=100, n=1000, forward=False):
    return Sleeve("s", exp_r=exp, trades_per_year=tpy, max_dd_r=-10.0,
                  n_measured=n, forward=forward)


# --------------------------------------------------------------- the ladder

def test_every_rung_is_reported_not_a_single_number():
    p = project([sleeve()], q=0.0127, n_trials=100)
    labels = [r.label for r in p.rungs]
    assert labels == ["IN-SAMPLE", "HALF-EDGE", "DEFLATED", "FORWARD"]


def test_the_forward_rung_is_zero_because_nothing_has_filled():
    """Zero because the desk has never had a fill, not because the edge is
    zero — and the output must say which."""
    p = project([sleeve(forward=False)], q=0.0127, n_trials=100)
    fwd = next(r for r in p.rungs if r.label == "FORWARD")
    assert fwd.exp_r == 0.0
    assert "never had a fill" in fwd.why


def test_a_backtest_is_never_reported_as_a_track_record():
    p = project([sleeve(forward=False)], q=0.0127, n_trials=100)
    assert "NO SLEEVE HERE HAS FORWARD EVIDENCE" in p.render()


def test_forward_evidence_when_it_exists_is_used():
    p = project([sleeve(exp=0.3, forward=True)], q=0.0127, n_trials=100)
    fwd = next(r for r in p.rungs if r.label == "FORWARD")
    assert fwd.exp_r == pytest.approx(0.3)


def test_the_half_edge_rung_halves_the_expectancy():
    p = project([sleeve(exp=0.6)], q=0.0127, n_trials=100)
    a = next(r for r in p.rungs if r.label == "IN-SAMPLE").exp_r
    b = next(r for r in p.rungs if r.label == "HALF-EDGE").exp_r
    assert b == pytest.approx(a / 2)


# ------------------------------------------------------------- the deflation

def test_more_trials_deflate_the_expectancy_further():
    assert (deflate_expectancy(0.5, 1000, 10_000)
            < deflate_expectancy(0.5, 1000, 100)
            < deflate_expectancy(0.5, 1000, 2))


def test_a_larger_sample_resists_deflation():
    """The correction is a standard error, so more trades survive more trials."""
    assert (deflate_expectancy(0.5, 10_000, 2464)
            > deflate_expectancy(0.5, 100, 2464))


def test_a_thin_result_against_a_wide_search_can_deflate_below_zero():
    """The outcome that matters, and it must be reachable."""
    assert deflate_expectancy(0.10, 60, 5000) < 0


def test_the_deflation_gap_is_named_as_the_multiplicity_problem():
    p = project([sleeve()], q=0.0127, n_trials=2464)
    assert any("size of the multiplicity problem" in u for u in p.unpriced)


# ------------------------------------------------------------- compounding

def test_returns_compound_rather_than_accrue_linearly():
    """Each trade risks q of CURRENT equity. Over hundreds of trades the
    difference is the entire result — linear flatters a winner and hides how
    fast a loser dies."""
    comp = project([sleeve(exp=0.5, tpy=200)], q=0.02, n_trials=2)
    lin = project([sleeve(exp=0.5, tpy=200)], q=0.02, n_trials=2,
                  compounding=False)
    c = next(r for r in comp.rungs if r.label == "IN-SAMPLE").annual_return
    l = next(r for r in lin.rungs if r.label == "IN-SAMPLE").annual_return
    assert c > l


def test_a_losing_book_compounds_downward():
    p = project([sleeve(exp=-0.2, tpy=200)], q=0.02, n_trials=2)
    assert next(r for r in p.rungs if r.label == "IN-SAMPLE").annual_return < 0


# ------------------------------------------------------- it cannot flatter itself

def test_the_projection_does_not_choose_its_own_sizing():
    """A projection that picks q can produce any answer the author wants."""
    import inspect
    sig = inspect.signature(project)
    assert sig.parameters["q"].default is inspect.Parameter.empty


def test_slippage_is_named_as_unpriced_rather_than_guessed():
    p = project([sleeve()], q=0.0127, n_trials=100)
    assert any("UNMEASURED" in u for u in p.unpriced)


def test_no_sleeves_projects_nothing():
    assert "nothing to project" in project([], q=0.0127, n_trials=10).unpriced[0]


# ------------------------------------------------------------- the real book

def test_the_armed_gold_book_is_recorded_as_in_sample():
    """It came from the sweep that selected it, with thresholds fitted on the
    same data. That is why the ladder exists."""
    assert all(not s.forward for s in ASIA_GOLD_MEASURED)
    p = project(ASIA_GOLD_MEASURED, q=0.0127, n_trials=2464)
    ins = next(r for r in p.rungs if r.label == "IN-SAMPLE")
    assert 0.5 < ins.exp_r < 0.65        # blended ~ +0.574R


def test_an_implausible_projection_is_the_input_failing_not_the_arithmetic():
    """A book returning five-figure annual percentages is telling you the
    expectancy is not real. The tool must not smooth that away."""
    p = project(ASIA_GOLD_MEASURED, q=0.13, n_trials=1)
    ins = next(r for r in p.rungs if r.label == "IN-SAMPLE")
    assert ins.annual_return > 100.0, "the runaway must remain visible"
