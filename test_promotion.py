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

import pytest

from golddesk.growth import (MAX_DRAWDOWN_TOLERANCE, per_sleeve_heat,
                             solve_heat)
from golddesk.promotion import (MIN_FORWARD_T, MIN_VERDICT_TRADES, VERDICT_MIN_TRADES,
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
    for _ in range(MIN_VERDICT_TRADES - 1):
        observe(c, 0.5)
    consider_promotion(c)
    assert c.status is Status.SHADOW, "promoted below the day floor"


def test_promotion_on_real_forward_evidence():
    c = to_shadow(screen("x", 1.0, 0.99))
    rng = random.Random(4)
    for _ in range(VERDICT_MIN_TRADES + 20):
        observe(c, 0.30 + rng.gauss(0, 0.5))
    consider_promotion(c)
    assert c.status is Status.LIVE
    assert c.forward_t >= MIN_FORWARD_T


def test_flat_forward_record_does_not_promote():
    """A noise cell reverts. Zero-mean forward days must not promote it."""
    c = to_shadow(screen("x", 3.0, 0.999))
    rng = random.Random(7)
    for _ in range(VERDICT_MIN_TRADES + 40):
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
    for _ in range(VERDICT_MIN_TRADES + 20):
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


# ------------------------------------------------- the marginal-growth gate
#
# These are the tests that separate "this sleeve makes money" from "the book
# compounds faster for holding it". Every one supplies DATES, because without
# them the growth test is not applicable and the gate silently degrades to the
# significance test it is meant to sit behind.

from golddesk.promotion import (GROWTH_TOLERANCE, live_series_of,  # noqa: E402
                                marginal_growth, promote_book)


def _days(n):
    return [f"2026-01-{i + 1:04d}" for i in range(n)]


def _shadow(cell, seq, dates):
    c = to_shadow(screen(cell, 1.0, 0.99))
    for r, d in zip(seq, dates):
        observe(c, r, day=d)
    return c


def test_a_redundant_edge_is_held_even_though_it_is_real():
    """THE POINT OF THE GATE. A duplicate of the live book has a real forward
    edge and adds nothing, so it must not be promoted."""
    rng = random.Random(3)
    n = 300
    dates = _days(n)
    base = [0.35 + rng.gauss(0, 0.4) for _ in range(n)]
    live = _shadow("LIVE|fam", base, dates)
    live.status = Status.LIVE
    # a near-copy: same days, same returns plus a whisper of noise
    dup = _shadow("DUP|fam", [b + rng.gauss(0, 0.02) for b in base], dates)
    promote_book([live, dup])
    assert dup.status is Status.SHADOW
    assert any("does not compound the book" in n for n in dup.notes)


def test_an_uncorrelated_edge_of_the_same_size_is_promoted():
    """The control for the test above: same edge, independent, must go live."""
    rng = random.Random(4)
    n = 300
    dates = _days(n)
    live = _shadow("LIVE|fam", [0.35 + rng.gauss(0, 0.4) for _ in range(n)], dates)
    live.status = Status.LIVE
    indep = _shadow("INDEP|fam",
                    [0.35 + rng.gauss(0, 0.4) for _ in range(n)], dates)
    promote_book([live, indep])
    assert indep.status is Status.LIVE


def test_marginal_growth_is_negative_for_a_duplicate():
    rng = random.Random(5)
    n = 300
    dates = _days(n)
    base = [0.35 + rng.gauss(0, 0.4) for _ in range(n)]
    live = _shadow("L|f", base, dates)
    live.status = Status.LIVE
    dup = _shadow("D|f", [b + rng.gauss(0, 0.02) for b in base], dates)
    g = marginal_growth(dup, live_series_of([live]))
    assert g is not None and g <= 0


def test_first_sleeve_into_an_empty_book_needs_no_growth_test():
    """Nothing to be correlated with — not applicable, not failed."""
    rng = random.Random(6)
    n = 300
    c = _shadow("FIRST|f", [0.35 + rng.gauss(0, 0.4) for _ in range(n)], _days(n))
    promote_book([c])
    assert c.status is Status.LIVE


def test_growth_unknowable_without_dates_falls_back_not_wrong():
    """A caller that omits dates gets the weaker test, never a bogus alignment."""
    rng = random.Random(7)
    n = 300
    live = to_shadow(screen("L|f", 1.0, 0.99))
    cand = to_shadow(screen("C|f", 1.0, 0.99))
    for _ in range(n):
        observe(live, 0.35 + rng.gauss(0, 0.4))
        observe(cand, 0.35 + rng.gauss(0, 0.4))
    live.status = Status.LIVE
    assert marginal_growth(cand, live_series_of([live])) is None
    promote_book([live, cand])
    assert cand.status is Status.LIVE


def test_two_mutually_redundant_candidates_do_not_both_go_live():
    """The baseline must refresh after every move.

    Scoring once and executing a whole list lets two copies of the same idea
    both clear the same stale baseline. Whichever wins may ADD or REPLACE; what
    must not happen is both ending up live, because the second adds nothing the
    first has not already brought.
    """
    rng = random.Random(8)
    n = 300
    dates = _days(n)
    live = _shadow("L|f", [0.30 + rng.gauss(0, 0.5) for _ in range(n)], dates)
    live.status = Status.LIVE
    twin = [0.40 + rng.gauss(0, 0.4) for _ in range(n)]
    a = _shadow("A|f", twin, dates)
    b = _shadow("B|f", [t + rng.gauss(0, 0.02) for t in twin], dates)
    promote_book([live, a, b])
    assert not (a.status is Status.LIVE and b.status is Status.LIVE)


def test_a_better_edge_replaces_a_worse_correlated_incumbent():
    """REPLACEMENT IS A FIRST-CLASS MOVE. A pipeline that can only append holds
    the worse of two correlated edges forever because it arrived first."""
    rng = random.Random(12)
    n = 320
    dates = _days(n)
    weak = [0.12 + rng.gauss(0, 0.45) for _ in range(n)]
    incumbent = _shadow("WEAK|f", weak, dates)
    incumbent.status = Status.LIVE
    # same shape, materially better mean -> cannot coexist, should take over
    better = _shadow("BETTER|f", [w + 0.28 for w in weak], dates)
    promote_book([incumbent, better])
    assert better.status is Status.LIVE
    assert incumbent.status is Status.RETIRED
    assert any("REPLACED by" in x for x in incumbent.notes)


def test_every_positive_gain_is_taken_however_small():
    """"Add everything that boosts growth, even a little." Three independent
    edges must all reach live rather than stopping at the first."""
    rng = random.Random(14)
    n = 320
    dates = _days(n)
    cells = [_shadow(f"IND{i}|f",
                     [0.30 + rng.gauss(0, 0.45) for _ in range(n)], dates)
             for i in range(3)]
    cells[0].status = Status.LIVE
    promote_book(cells)
    assert sum(1 for c in cells if c.status is Status.LIVE) == 3


def test_require_growth_false_restores_significance_only():
    rng = random.Random(9)
    n = 300
    dates = _days(n)
    base = [0.35 + rng.gauss(0, 0.4) for _ in range(n)]
    live = _shadow("L|f", base, dates)
    live.status = Status.LIVE
    dup = _shadow("D|f", [x + rng.gauss(0, 0.02) for x in base], dates)
    promote_book([live, dup], require_growth=False)
    assert dup.status is Status.LIVE


# ------------------------------------------------- the cadence is TRADES, not days
#
# shadow_forward.py measured what a calendar clock costs: a cell firing ~80 times
# a year makes about THREE fills in fourteen days, and a verdict on three fills
# kills a genuinely good edge 36% of the time. These encode that lesson so a
# future "simplification" back to a day count fails loudly.

from golddesk.promotion import (VERDICT_MIN_DAYS,  # noqa: E402
                                eligible_for_verdict)


def test_a_slow_sleeve_is_not_judged_on_three_fills():
    """THE LESSON. Fourteen days of a slow sleeve is three trades, and three
    trades is not evidence in either direction."""
    c = to_shadow(screen("slow", 1.0, 0.99))
    for i in range(VERDICT_MIN_DAYS + 6):        # clock trigger satisfied
        observe(c, 0.4, day=f"2026-02-{i + 1:02d}", n_trades=0)
    observe(c, 0.4, day="2026-03-01", n_trades=3)
    assert not eligible_for_verdict(c), "judged a sleeve on three fills"
    consider_promotion(c)
    assert c.status is Status.SHADOW


def test_the_same_sleeve_is_judged_once_it_has_fills():
    c = to_shadow(screen("slow", 1.0, 0.99))
    rng = random.Random(31)
    for i in range(30):
        observe(c, 0.35 + rng.gauss(0, 0.4), day=f"2026-04-{i + 1:02d}",
                n_trades=2)
    assert c.forward_trades >= MIN_VERDICT_TRADES
    assert eligible_for_verdict(c)
    consider_promotion(c)
    assert c.status is Status.LIVE


def test_fifty_fills_triggers_even_inside_fourteen_days():
    """A fast sleeve should not wait out a calendar clock it does not need."""
    c = to_shadow(screen("fast", 1.0, 0.99))
    rng = random.Random(32)
    for i in range(5):                            # five days, ten fills each
        observe(c, 0.35 + rng.gauss(0, 0.35), day=f"2026-05-{i + 1:02d}",
                n_trades=10)
    assert c.forward_trades >= VERDICT_MIN_TRADES
    assert len(c.forward_r) < VERDICT_MIN_DAYS
    assert eligible_for_verdict(c)


def test_the_trade_floor_outranks_the_clock_in_both_directions():
    c = to_shadow(screen("x", 1.0, 0.99))
    for i in range(90):
        observe(c, 0.4, day=f"2026-06-{i + 1:03d}", n_trades=0)
    assert c.forward_trades == 0
    assert not eligible_for_verdict(c), "ninety days of no fills is not evidence"


def test_the_book_series_is_weighted_the_way_the_book_trades():
    """THE MEASUREMENT MUST MATCH THE ALLOCATION.

    series_of once equal-weighted while per_sleeve_heat allocated by measured
    expectancy, so every promotion decision was scored against a portfolio the
    desk does not hold. The gap is small while sleeve edges are similar and
    widens exactly when they are not — the case where the decision matters.
    """
    from golddesk.promotion import series_of
    dates = _days(200)
    strong = _shadow("STRONG|f", [1.0] * 200, dates)
    weak = _shadow("WEAK|f", [0.0] * 200, dates)
    eq = series_of([strong, weak], edge_weighted=False)
    ew = series_of([strong, weak], edge_weighted=True)
    assert eq[dates[0]] == pytest.approx(0.5), "equal weights halve the strong one"
    assert ew[dates[0]] == pytest.approx(1.0), (
        "edge weights must give the zero-mean sleeve no size, matching what "
        "per_sleeve_heat would allocate")


def test_a_negative_sleeve_gets_no_weight_in_the_book_series():
    from golddesk.promotion import series_of
    dates = _days(200)
    good = _shadow("G|f", [0.5] * 200, dates)
    bad = _shadow("B|f", [-0.5] * 200, dates)
    s = series_of([good, bad], edge_weighted=True)
    assert s[dates[0]] == pytest.approx(0.5)


def test_no_positive_means_falls_back_rather_than_returning_nothing():
    """Absence of a basis for preference is not evidence of equality, but a
    zero-weight book has no series at all — so equal weights are the right
    fallback here."""
    from golddesk.promotion import series_of
    dates = _days(200)
    a = _shadow("A|f", [-0.1] * 200, dates)
    b = _shadow("B|f", [-0.3] * 200, dates)
    s = series_of([a, b], edge_weighted=True)
    assert s and s[dates[0]] == pytest.approx(-0.2)
