"""A drawdown inside the expected distribution is not decay. A monitor that
cannot tell the difference retires good sleeves at exactly the rate the market
hands out bad luck.
"""
from __future__ import annotations

import random

import pytest

from golddesk.decay import (
    CUSUM_THRESHOLD, MIN_TRADES, assess, book_health, cusum_decay,
    detection_latency)


def draw(exp_r, n, seed=0):
    """R-multiples from a 2:1 bracket with the hit rate implied by `exp_r`."""
    rng = random.Random(seed)
    p = (exp_r + 1) / 3.0
    return [2.0 if rng.random() < p else -1.0 for _ in range(n)]


# ------------------------------------------- bad luck is not decay

def test_a_healthy_sleeve_reads_INTACT_despite_losing_stretches():
    """A +0.2R book with unit variance produces losing months constantly. That
    is what the distribution says it does."""
    s = assess("healthy", draw(0.212, 600, seed=1), baseline_exp_r=0.212)
    assert s.status in ("INTACT", "WATCH")
    assert "not decay" in s.why or "halfway" in s.why


def test_a_short_losing_run_does_not_trip_the_monitor():
    """The failure mode of a rolling average with a threshold under it."""
    r = draw(0.212, 300, seed=2) + [-1.0] * 12
    assert assess("blip", r, baseline_exp_r=0.212).status != "DECAYED"


def test_good_trades_reset_the_evidence_rather_than_banking_credit():
    """The cusum floors at zero, so a recovery genuinely clears the case
    against a sleeve instead of leaving it one bad week from retirement."""
    path, peak, first = cusum_decay([-1.0] * 20 + [2.0] * 40, 0.212)
    assert path[-1] == pytest.approx(0.0, abs=1e-9)
    assert peak > 0


# ------------------------------------------- a real break is caught

def test_a_genuine_halving_is_detected():
    """Enough trades after the break that the evidence can actually accumulate."""
    r = draw(0.60, 200, seed=3) + draw(0.30, 1200, seed=4)
    s = assess("decayed", r, baseline_exp_r=0.60)
    assert s.status == "DECAYED"
    assert "persistent shift" in s.why


def test_the_verdict_says_how_many_trades_ran_after_the_break():
    r = draw(0.60, 200, seed=5) + draw(0.30, 1200, seed=6)
    s = assess("decayed", r, baseline_exp_r=0.60)
    assert s.trades_since_break and s.trades_since_break > 0


def test_a_collapse_to_negative_is_caught_quickly():
    r = draw(0.60, 100, seed=7) + [-1.0] * 60
    s = assess("dead", r, baseline_exp_r=0.60)
    assert s.status == "DECAYED"


# ------------------------------------------- unmonitored is not healthy

def test_too_few_trades_is_INSUFFICIENT_not_INTACT():
    """The distinction that matters for the armed book: it is unmonitored, not
    proven healthy."""
    s = assess("new", draw(0.2, MIN_TRADES - 1), baseline_exp_r=0.2)
    assert s.status == "INSUFFICIENT"
    assert "Not the same as INTACT" in s.why


def test_the_baseline_is_the_warrant_not_the_running_mean():
    """Comparing a sleeve to its own recent average asks whether it changed,
    which every series does. Comparing it to its warrant asks whether it still
    deserves the authority it was given."""
    r = draw(0.30, 800, seed=8)
    lenient = assess("s", r, baseline_exp_r=0.30)
    strict = assess("s", r, baseline_exp_r=0.90)
    assert strict.cusum > lenient.cusum


# ------------------------------------------- the uncomfortable arithmetic

def test_a_thin_edge_takes_far_longer_to_prove_decayed():
    """The information content of the data, not a shortcoming of the method."""
    assert detection_latency(0.096) > detection_latency(0.212) > detection_latency(0.90)


def test_the_desk_book_needs_over_a_thousand_trades_to_prove_a_halving():
    """At +0.159R this is the number that decides whether monitoring is a
    strategy: by the time decay is provable, it has been paid for."""
    assert detection_latency(0.159) > 1000


def test_a_zero_edge_has_no_detectable_halving():
    assert detection_latency(0.0) is None


# ------------------------------------------------------------ the bench

def test_retiring_without_replacements_is_flagged_as_shrinking_the_book():
    """A monitor that retires without a bench does not protect the book, it
    concentrates the remaining risk in fewer bets exactly when the evidence
    says edges are degrading."""
    dead = assess("a", draw(0.6, 100, seed=9) + [-1.0] * 80, baseline_exp_r=0.6)
    live = assess("b", draw(0.212, 300, seed=10), baseline_exp_r=0.212)
    h = book_health([dead, live], ready_replacements=0, min_sleeves=3)
    assert "does not protect the book, it shrinks it" in h.why


def test_a_ready_bench_means_the_slot_can_be_refilled():
    dead = assess("a", draw(0.6, 100, seed=11) + [-1.0] * 80, baseline_exp_r=0.6)
    live = assess("b", draw(0.212, 300, seed=12), baseline_exp_r=0.212)
    h = book_health([dead, live], ready_replacements=4, min_sleeves=3)
    assert "can be refilled" in h.why


def test_unmonitored_sleeves_are_surfaced_in_the_book_reading():
    thin = assess("armed_gold", draw(0.212, 10), baseline_exp_r=0.212)
    h = book_health([thin], ready_replacements=0)
    assert "Unmonitored is not healthy" in h.why
    assert h.unmonitored


def test_a_clean_book_says_so_without_drama():
    ok = [assess(f"s{i}", draw(0.212, 400, seed=20 + i), baseline_exp_r=0.212)
          for i in range(3)]
    h = book_health(ok, ready_replacements=2)
    assert "DECAYED" not in h.why
