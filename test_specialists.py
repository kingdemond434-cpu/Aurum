"""A specialist can be accurate and worth nothing. These tests are mostly about
the difference between those two facts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import math
import pytest

from golddesk.snapshot import SnapshotBuilder
from golddesk.specialists import (
    MIN_CHANGED, Council, SequenceSpecialist, SpecialistRead,
    UnavailableSpecialist, marginal_value)

UTC = timezone.utc
T0 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class Bar:
    def __init__(self, t, o, h, l, c):
        self.time, self.open, self.high, self.low, self.close = t, o, h, l, c


def snap(n_bars=30):
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    bars = [Bar(T0 - timedelta(minutes=15 * (n_bars - i)),
                2000 + i, 2003 + i, 1997 + i, 2001 + i) for i in range(n_bars)]
    b.add_bars("m15", bars, "M15", count=n_bars)
    return b.build()


# ---------------------------------------------------------------- the read

def test_a_direction_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError):
        SpecialistRead("x", "MAYBE")


def test_strength_is_bounded_on_construction():
    """A learned model will emit whatever it likes. Precision is what sizing
    reads, so it is clamped where it enters, not where it is used."""
    assert SpecialistRead("x", "LONG", 4.2).strength == 1.0
    assert SpecialistRead("x", "LONG", -0.5).strength == 0.0


def test_signed_collapses_direction_and_strength():
    assert SpecialistRead("x", "SHORT", 0.5).signed == -0.5
    assert SpecialistRead("x", "FLAT", 1.0).signed == 0.0


# ----------------------------------------------------- absence is not an opinion

def test_a_missing_model_is_UNAVAILABLE_not_FLAT():
    """THE DISTINCTION THE CLASS EXISTS FOR. A missing sequence model returning
    FLAT reads downstream as 'the model sees nothing here' — an observation —
    when nobody asked anything."""
    r = UnavailableSpecialist("sequence").read(snap())
    assert not r.available
    assert "no model" in r.why


def test_the_seam_without_weights_is_unavailable():
    r = SequenceSpecialist().read(snap())
    assert not r.available and "never bundled" in r.why


def test_a_raising_model_becomes_unavailable_not_an_exception():
    """A specialist that can throw into the decision path can halt the desk, and
    no reader is worth that."""
    def boom(bars):
        raise RuntimeError("checkpoint corrupt")
    r = SequenceSpecialist(predict_fn=boom).read(snap())
    assert not r.available and "checkpoint corrupt" in r.why


def test_a_model_returning_nonsense_is_unavailable():
    r = SequenceSpecialist(predict_fn=lambda b: {"weird": 1}).read(snap())
    assert not r.available and "expected a float" in r.why


def test_a_non_finite_prediction_is_refused():
    r = SequenceSpecialist(predict_fn=lambda b: float("nan")).read(snap())
    assert not r.available


def test_too_few_bars_is_unavailable_rather_than_a_guess():
    r = SequenceSpecialist(predict_fn=lambda b: 0.5, min_bars=50).read(snap(30))
    assert not r.available and "50 required" in r.why


# ------------------------------------------------------------- the data path

def test_the_specialist_reads_through_the_snapshot():
    """A specialist with its own data path is a specialist with its own
    lookahead. The snapshot has already refused the future."""
    seen = {}
    SequenceSpecialist(predict_fn=lambda b: seen.update(n=len(b)) or 0.3).read(snap(30))
    assert seen["n"] == 30


def test_bars_reach_the_model_oldest_first():
    """A sequence model fed backwards will produce confident nonsense."""
    got = {}
    SequenceSpecialist(predict_fn=lambda b: got.update(first=b[0][3],
                                                       last=b[-1][3]) or 0.1).read(snap(30))
    assert got["first"] < got["last"]


def test_a_float_prediction_becomes_a_direction():
    assert SequenceSpecialist(predict_fn=lambda b: 0.7).read(snap()).direction == "LONG"
    assert SequenceSpecialist(predict_fn=lambda b: -0.7).read(snap()).direction == "SHORT"
    assert SequenceSpecialist(predict_fn=lambda b: 0.0).read(snap()).direction == "FLAT"


def test_a_direction_strength_pair_is_accepted():
    r = SequenceSpecialist(predict_fn=lambda b: ("short", 0.4)).read(snap())
    assert r.direction == "SHORT" and r.strength == 0.4


# ------------------------------------------------------------- no consensus

def test_the_council_has_no_method_that_collapses_the_reads():
    """Averaging correlated readers manufactures confidence out of structural
    agreement and erases the divergence that was the only useful output."""
    banned = {"consensus", "vote", "average", "aggregate", "combine", "mean"}
    assert not (banned & set(dir(Council))), "the council grew a consensus method"


def test_disagreement_is_reported_as_information():
    c = Council([_fixed("a", "LONG"), _fixed("b", "SHORT")])
    assert c.report(snap())["agreement"] == "SPLIT"


def test_agreement_is_reported_but_never_weights_anything():
    rep = Council([_fixed("a", "LONG"), _fixed("b", "LONG")]).report(snap())
    assert rep["agreement"] == "UNANIMOUS"
    assert "manufactures confidence" in rep["note"]


def test_unavailable_specialists_do_not_count_toward_agreement():
    rep = Council([_fixed("a", "LONG"), UnavailableSpecialist("b")]).report(snap())
    assert rep["available"] == 1 and rep["unavailable"] == ["b"]
    assert rep["agreement"] == "UNANIMOUS"


def test_an_empty_council_says_NONE_rather_than_agreeing_with_itself():
    assert Council([]).report(snap())["agreement"] == "NONE"


def _fixed(name, direction):
    class F:
        def __init__(self): self.name = name
        def read(self, s): return SpecialistRead(name, direction, 0.5)
    return F()


# ------------------------------------------------ what is the specialist worth?

def test_a_specialist_that_never_changes_a_decision_has_no_standing():
    """It can be right 70% of the time. The desk would have taken those trades
    anyway."""
    d = ["LONG"] * 200
    mv = marginal_value("seq", d, d, [1.0] * 200, [1.0] * 200)
    assert mv.n_changed == 0 and mv.verdict == "NO STANDING"
    assert "added nothing" in mv.why


def test_unchanged_states_are_excluded_not_counted_as_ties():
    """Including them adds identical outcomes to both arms: the difference
    cannot move, but the sample inflates and the standard error shrinks — a
    smaller p-value from observations carrying no information."""
    n = 400
    with_s = ["LONG"] * n
    without = ["LONG"] * n
    r, cf = [0.0] * n, [0.0] * n
    for i in range(0, 60):                      # only 60 states actually differ
        with_s[i], without[i] = "LONG", "FLAT"
        r[i], cf[i] = 1.0, 0.0
    mv = marginal_value("seq", with_s, without, r, cf)
    assert mv.n_changed == 60, "unchanged states leaked into the sample"
    assert mv.n_states == 400


def test_a_thin_change_set_returns_no_verdict():
    n = MIN_CHANGED - 5
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, [1.0] * n, [0.0] * n)
    assert mv.verdict == "NO STANDING"
    assert "right answer for a new specialist" in mv.why


def test_changes_that_lose_money_are_reported_negative():
    n = 100
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, [-0.5] * n, [0.0] * n)
    assert mv.verdict == "NEGATIVE"
    assert "worse than none" in mv.why


def test_the_cost_of_flipping_is_charged_to_the_specialist():
    """A specialist right 55% of the time whose changes cost more than they earn
    is a losing specialist, and only a net measurement says so."""
    n = 100
    free = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n,
                          [0.02] * n, [0.0] * n, cost_r=0.0)
    charged = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n,
                             [0.02] * n, [0.0] * n, cost_r=0.05)
    assert free.mean_r_per_change > 0 and charged.mean_r_per_change < 0
    assert charged.verdict == "NEGATIVE"


def test_a_small_positive_edge_is_unproven_rather_than_promoted():
    import random
    rng = random.Random(4)
    n = 120
    r = [0.06 + rng.gauss(0, 1.0) for _ in range(n)]
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, r, [0.0] * n)
    assert mv.verdict == "UNPROVEN" and "shadow" in mv.why


def test_a_genuine_edge_earns_standing_and_says_it_is_uncorrected():
    n = 200
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, [0.6] * n, [0.0] * n)
    assert mv.verdict == "POSITIVE" and mv.has_standing
    assert "uncorrected" in mv.why and "seal" in mv.why.lower()


def test_the_render_shows_the_change_rate_not_just_the_total():
    n = 200
    with_s = ["LONG"] * n
    without = ["FLAT"] * 50 + ["LONG"] * 150
    mv = marginal_value("seq", with_s, without, [0.6] * n, [0.0] * n)
    assert "CHANGED" in mv.render() and "25.0%" in mv.render()


# ------------------------------------------------- zero-variance verdicts
#
# Caught on a different interpreter, not by design. With every delta identical
# the sample has zero variance, and whether floating-point summation leaves a
# 1-ULP residue decided whether sd was tiny-positive or exactly zero — so the
# same input returned POSITIVE on Python 3.11 and UNPROVEN on 3.12. The old
# `t = 0.0 if sd == 0` also had the logic backwards: a specialist that improved
# EVERY decision by the same amount is the strongest possible evidence.

def test_a_perfectly_consistent_gain_is_not_called_noise():
    n = 200
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, [0.6] * n, [0.0] * n)
    assert mv.verdict == "POSITIVE", (
        "zero variance with a positive mean is the strongest evidence there is")
    assert mv.t_stat == math.inf


def test_a_perfectly_consistent_loss_is_negative_not_unproven():
    n = 200
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, [0.0] * n, [0.6] * n)
    assert mv.verdict == "NEGATIVE"


def test_the_verdict_does_not_depend_on_floating_point_luck():
    """The same question asked two ways must answer the same.

    One arm sums to exactly zero variance, the other leaves a rounding residue.
    Before the fix these returned different verdicts on the same evidence.
    """
    n = 200
    exact = marginal_value("a", ["LONG"] * n, ["FLAT"] * n, [0.6] * n, [0.0] * n)
    jitter = marginal_value("b", ["LONG"] * n, ["FLAT"] * n,
                            [0.6 + (1e-15 if i % 2 else -1e-15) for i in range(n)],
                            [0.0] * n)
    assert exact.verdict == jitter.verdict == "POSITIVE"


def test_a_dead_flat_specialist_is_still_unproven():
    """Zero mean AND zero variance is genuinely no information."""
    n = 200
    mv = marginal_value("seq", ["LONG"] * n, ["FLAT"] * n, [0.05] * n, [0.0] * n)
    assert mv.t_stat == 0.0
    assert mv.verdict in ("UNPROVEN", "NEGATIVE")
