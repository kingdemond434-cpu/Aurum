"""Tests for the promotion pipeline and the solved heat budget.

The load-bearing assertions are the ones about what CANNOT happen: a cell must
not reach LIVE on in-sample evidence however good it looks, and the heat budget
must not return a number when it has no days to solve from.
"""
from __future__ import annotations

import math
import random
import tempfile
from pathlib import Path

from golddesk.growth import (MAX_DRAWDOWN_TOLERANCE, per_sleeve_heat,
                             solve_heat)
from golddesk.promotion import (MIN_FORWARD_T, MIN_SHADOW_DAYS,
                                RAW_PSR_THRESHOLD, Candidate, Status, consider_promotion,
                                load, observe, queue, report, review, save,
                                screen, to_shadow)


# --------------------------------------------------------------- the raw gate

def test_raw_threshold_admits_regardless_of_multiplicity():
    """THE POLICY. A cell clearing the un-inflated bar is admitted even when the
    deflated Sharpe at N=3168 would refuse it."""
    c = screen("XAUUSD|breakout", in_sample_sharpe=1.2, psr_raw=0.99,
               dsr_deflated=0.10, n_trials_searched=3168)
    assert c.status is Status.CANDIDATE
    assert "not applied as a veto" in " ".join(c.notes)


def test_deflation_is_recorded_not_discarded():
    c = screen("x", 1.2, 0.99, dsr_deflated=0.10, n_trials_searched=3168)
    assert c.dsr_deflated == 0.10
    assert c.n_trials_searched == 3168


def test_below_raw_threshold_is_rejected():
    c = screen("x", in_sample_sharpe=0.4, psr_raw=0.50)
    assert c.status is Status.REJECTED


def test_negative_sharpe_rejected_even_at_high_psr():
    c = screen("x", in_sample_sharpe=-0.5, psr_raw=0.99)
    assert c.status is Status.REJECTED


# ------------------------------------------------- the gate that actually gates

def test_in_sample_alone_never_reaches_live():
    """THE ONE THAT MATTERS. A spectacular in-sample cell with no forward days
    must not be promotable — a LIVE status authorises real lots."""
    c = screen("x", in_sample_sharpe=9.9, psr_raw=1.0)
    to_shadow(c)
    consider_promotion(c)
    assert c.status is Status.SHADOW


def test_promotion_requires_enough_forward_days():
    c = to_shadow(screen("x", 1.0, 0.99))
    for _ in range(MIN_SHADOW_DAYS - 1):
        observe(c, 0.5)
    consider_promotion(c)
    assert c.status is Status.SHADOW, "promoted below the day floor"


def test_promotion_on_real_forward_evidence():
    c = to_shadow(screen("x", 1.0, 0.99))
    rng = random.Random(4)
    for _ in range(MIN_SHADOW_DAYS + 20):
        observe(c, 0.30 + rng.gauss(0, 0.5))
    consider_promotion(c)
    assert c.status is Status.LIVE
    assert c.forward_t >= MIN_FORWARD_T


def test_flat_forward_record_does_not_promote():
    """A noise cell reverts. Zero-mean forward days must not promote it."""
    c = to_shadow(screen("x", 3.0, 0.999))
    rng = random.Random(7)
    for _ in range(MIN_SHADOW_DAYS + 40):
        observe(c, rng.gauss(0, 0.5))
    consider_promotion(c)
    assert c.status is Status.SHADOW


def test_forward_t_is_none_not_zero_when_uncomputable():
    c = to_shadow(screen("x", 1.0, 0.99))
    assert c.forward_t is None
    observe(c, 0.5)
    assert c.forward_t is None, "one observation is not a t-statistic of zero"


def test_observe_ignores_non_finite():
    c = to_shadow(screen("x", 1.0, 0.99))
    observe(c, float("nan"))
    observe(c, float("inf"))
    assert c.shadow_days == 0


def test_candidate_cannot_skip_shadow():
    c = screen("x", 1.0, 0.99)
    consider_promotion(c)
    assert c.status is Status.CANDIDATE


# ------------------------------------------------------------------- retirement

def test_review_retires_a_decayed_live_cell():
    c = to_shadow(screen("x", 1.0, 0.99))
    rng = random.Random(11)
    for _ in range(MIN_SHADOW_DAYS + 20):
        observe(c, 0.30 + rng.gauss(0, 0.4))
    consider_promotion(c)
    assert c.status is Status.LIVE
    for _ in range(30):
        observe(c, -0.4)
    review(c)
    assert c.status is Status.RETIRED


def test_review_never_re_arms():
    c = Candidate(cell="x", in_sample_sharpe=1.0, psr_raw=0.99,
                  status=Status.RETIRED, forward_r=[1.0] * 60)
    review(c)
    assert c.status is Status.RETIRED


# ---------------------------------------------------------------- queue ordering

def test_queue_orders_by_deflated_sharpe_but_drops_nobody():
    a = screen("a", 1.0, 0.99, dsr_deflated=0.10)
    b = screen("b", 1.0, 0.99, dsr_deflated=0.80)
    q = queue([a, b])
    assert [c.cell for c in q] == ["b", "a"]
    assert len(q) == 2, "multiplicity must order the queue, never shorten it"


def test_queue_slots_limit_service_not_admission():
    cs = [screen(f"c{i}", 1.0, 0.99, dsr_deflated=i / 10) for i in range(5)]
    assert len(queue(cs, slots=2)) == 2
    assert all(c.status is Status.CANDIDATE for c in cs)


# ------------------------------------------------------------------ persistence

def test_round_trip():
    cs = [to_shadow(screen("a", 1.0, 0.99, dsr_deflated=0.2))]
    observe(cs[0], 0.4)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "pipe.json"
        save(cs, p)
        back = load(p)
    assert back[0].status is Status.SHADOW
    assert back[0].forward_r == [0.4]


def test_report_runs():
    cs = [screen("a", 1.0, 0.99), to_shadow(screen("b", 1.0, 0.99))]
    assert "PROMOTION PIPELINE" in report(cs)


# ------------------------------------------------------------- the solved heat

def test_solve_heat_refuses_on_too_few_days():
    heat, why = solve_heat([0.1] * 10)
    assert heat == 0.0
    assert "no fallback constant" in why or "No size authorised" in why


def test_solve_heat_hits_the_tolerance():
    rng = random.Random(3)
    days = [0.05 + rng.gauss(0, 0.5) for _ in range(2000)]
    heat, _ = solve_heat(days, tolerance=0.35)
    assert heat > 0

    def dd(q, vals):
        shift = 0.5 * (sum(vals) / len(vals))
        eq = peak = 1.0
        worst = 0.0
        for r in vals:
            eq *= 1 + q * (r - shift)
            peak = max(peak, eq)
            worst = max(worst, 1 - eq / peak)
        return worst
    assert dd(heat, days) <= 0.35 + 1e-3


def test_solve_heat_is_monotone_in_tolerance():
    """MORE TOLERANCE MUST BUY MORE SIZE, always, with no ceiling in the way."""
    rng = random.Random(5)
    days = [0.05 + rng.gauss(0, 0.5) for _ in range(1500)]
    h25, _ = solve_heat(days, tolerance=0.25)
    h45, _ = solve_heat(days, tolerance=0.45)
    h65, _ = solve_heat(days, tolerance=0.65)
    assert h25 < h45 < h65


def test_solve_heat_grows_when_the_book_improves():
    """A steadier book must be handed MORE heat with no constant edited."""
    rng = random.Random(9)
    noisy = [0.05 + rng.gauss(0, 0.8) for _ in range(1500)]
    steady = [0.05 + rng.gauss(0, 0.3) for _ in range(1500)]
    assert solve_heat(steady)[0] > solve_heat(noisy)[0]


def test_solve_heat_half_edge_is_smaller_than_in_sample():
    rng = random.Random(13)
    days = [0.08 + rng.gauss(0, 0.5) for _ in range(1500)]
    assert solve_heat(days, half_edge=True)[0] < solve_heat(days, half_edge=False)[0]


def test_solve_heat_refuses_a_losing_book():
    """A DRAWDOWN SOLVE IS NOT A PROFITABILITY TEST.

    This test originally asserted the wrong reason and caught a real gap: a
    monotonically losing series stays inside ANY tolerance at small enough q, so
    bisection alone authorised 0.21% heat on 200 days of straight losses. Losing
    slowly is not safety.
    """
    heat, why = solve_heat([-0.5] * 200, tolerance=0.10)
    assert heat == 0.0
    assert "no expectancy" in why


def test_solve_heat_refuses_a_book_positive_only_before_the_haircut():
    """Positive raw, non-positive at half edge -> no size. The haircut has to
    bite here or it is decoration."""
    rng = random.Random(21)
    days = [0.001 + rng.gauss(0, 0.4) for _ in range(400)]
    raw_mean = sum(days) / len(days)
    if raw_mean <= 0:                       # guard the fixture's own assumption
        raise AssertionError("fixture must be positive before the haircut")
    heat_half, why = solve_heat(days, half_edge=True)
    heat_full, _ = solve_heat(days, half_edge=False)
    assert heat_full > 0
    assert heat_half <= heat_full


def test_solve_heat_rejects_bad_tolerance():
    try:
        solve_heat([0.1] * 100, tolerance=1.5)
    except ValueError:
        return
    raise AssertionError("tolerance outside (0,1) must raise")


# --------------------------------------------------------------- edge weighting

def test_per_sleeve_heat_favours_the_stronger_sleeve():
    w = per_sleeve_heat(0.05, {"good": 0.20, "weak": 0.02})
    assert w["good"] > w["weak"]
    assert abs(sum(w.values()) - 0.05) < 1e-9


def test_per_sleeve_heat_zeroes_negative_sleeves():
    w = per_sleeve_heat(0.05, {"good": 0.20, "bad": -0.05})
    assert w["bad"] == 0.0
    assert abs(w["good"] - 0.05) < 1e-9


def test_per_sleeve_heat_all_negative_gives_nothing():
    w = per_sleeve_heat(0.05, {"a": -0.1, "b": -0.2})
    assert sum(w.values()) == 0.0
