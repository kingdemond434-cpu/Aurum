"""Lowering a trial count makes every threshold easier, so this module is a
tempting place to manufacture passes. Most of these tests guard that direction.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from golddesk.deflation import (
    CLONE_RHO, TrialCensus, census_from_registry, deflated_sharpe,
    effective_trials, expected_max_z, moments, report, sharpe_std_error)

RNG = np.random.default_rng(19)


def cols(n, t=250, rho=0.0):
    """n return series with a common factor of the given weight."""
    common = RNG.normal(size=t)
    return [rho * common + math.sqrt(max(0.0, 1 - rho ** 2)) * RNG.normal(size=t)
            for _ in range(n)]


# ------------------------------------------------------- the effective count

def test_identical_trials_collapse_to_one_search():
    c = RNG.normal(size=300)
    census = effective_trials([c.copy() for _ in range(10)])
    assert census.n_effective < 2.5


def test_independent_trials_stay_independent():
    census = effective_trials(cols(10, rho=0.0))
    assert census.n_effective > 7.0


def test_a_parameter_sweeps_block_structure_is_seen():
    """Two tight clusters of six, not twelve mildly related cells. A mean
    correlation collapses that difference; the participation ratio does not."""
    a, b = RNG.normal(size=300), RNG.normal(size=300)
    series = ([a + 0.01 * RNG.normal(size=300) for _ in range(6)]
              + [b + 0.01 * RNG.normal(size=300) for _ in range(6)])
    census = effective_trials(series)
    assert 2.0 <= census.n_effective <= 3.0, census.n_effective


def test_n_eff_can_never_exceed_the_search_performed():
    census = effective_trials(cols(4, rho=0.0))
    assert census.n_effective <= census.n_raw


def test_n_eff_is_floored_at_two():
    c = RNG.normal(size=300)
    assert effective_trials([c.copy() for _ in range(50)]).n_effective >= 2.0


def test_an_unbuildable_matrix_leaves_the_count_alone():
    """FAILS CLOSED. Absence of a deduplication is never permission to assume
    one."""
    census = effective_trials([RNG.normal(size=5) for _ in range(6)])
    assert census.n_effective == 6.0 and census.method == "unmeasurable"
    assert "no deduplication is assumed" in census.why


def test_unaligned_columns_are_refused_rather_than_truncated():
    """Truncating to the shortest column silently realigns everything to a
    different set of days and reports a correlation structure that never
    existed."""
    census = effective_trials([RNG.normal(size=300), RNG.normal(size=200)])
    assert census.method == "unaligned" and census.n_effective == 2.0
    assert "never existed" in census.why


def test_constant_columns_do_not_read_as_correlated():
    census = effective_trials([np.ones(300), np.ones(300), np.ones(300)])
    assert census.n_effective == 3.0


def test_a_single_trial_needs_no_deduplication():
    assert effective_trials([RNG.normal(size=300)]).n_effective == 1.0


def test_clone_pairs_are_reported_but_never_move_the_count():
    """The listing threshold must not be able to change a gate."""
    c = RNG.normal(size=300)
    census = effective_trials([c, c + 1e-9 * RNG.normal(size=300), RNG.normal(size=300)])
    assert census.clone_pairs
    assert census.method == "participation_ratio"


# ------------------------------------------------------------------- the weld

class _Reg:
    def __init__(self, n): self._n = n
    def trial_census(self): return {"trials_for_fdr": self._n}


def test_the_raw_count_comes_from_the_run_registry():
    """Not from memory: nobody remembers the tests that found nothing."""
    assert census_from_registry(_Reg(2464)).n_raw == 2464


def test_without_return_series_no_deduplication_is_assumed():
    c = census_from_registry(_Reg(2464))
    assert c.n_effective == 2464.0
    assert "no deduplication was attempted" in c.why


def test_measured_duplication_shrinks_the_registered_count():
    c0 = RNG.normal(size=300)
    c = census_from_registry(_Reg(100), [c0.copy() for _ in range(10)])
    assert c.n_raw == 100 and c.n_effective < 60


def test_deduplication_can_never_take_the_count_below_two():
    c0 = RNG.normal(size=300)
    assert census_from_registry(_Reg(3), [c0.copy() for _ in range(20)]).n_effective >= 2.0


def test_deduplication_can_never_raise_the_registered_count():
    c = census_from_registry(_Reg(8), cols(20, rho=0.0))
    assert c.n_effective <= 8.0


# ------------------------------------------------------------- the standard error

def test_fat_tails_widen_the_standard_error():
    """THE POINT OF CARRYING SKEW AND KURTOSIS. For a book that wins small and
    often and loses large and rarely, the naive 1/sqrt(T) is too small and every
    t-statistic comes out too big."""
    naive = sharpe_std_error(0.1, 500, skew=0.0, kurt=3.0)
    fat = sharpe_std_error(0.1, 500, skew=-1.5, kurt=9.0)
    assert fat > naive


def test_negative_skew_at_a_positive_sharpe_widens_the_error():
    assert (sharpe_std_error(0.3, 500, skew=-2.0, kurt=3.0)
            > sharpe_std_error(0.3, 500, skew=+2.0, kurt=3.0))


def test_the_normal_case_reduces_to_the_textbook_formula():
    assert sharpe_std_error(0.0, 501, 0.0, 3.0) == pytest.approx(1 / math.sqrt(500))


def test_one_observation_has_infinite_error():
    assert sharpe_std_error(0.5, 1) == float("inf")


def test_moments_are_not_annualised():
    """Annualisation is a choice about periodicity that would inflate every
    downstream threshold from inside a constant."""
    r = RNG.normal(loc=0.1, scale=1.0, size=1000)
    sr, _, _, _ = moments(r)
    assert 0.0 < sr < 0.3


def test_a_constant_series_has_no_sharpe():
    assert moments([1.0] * 50)[0] == 0.0


# ---------------------------------------------------------------- the threshold

def test_more_trials_raise_the_bar():
    assert expected_max_z(2464) > expected_max_z(100) > expected_max_z(5)


def test_the_threshold_accepts_a_fractional_count():
    assert expected_max_z(6.1) > expected_max_z(6.0)


def test_a_noise_book_fails_against_a_large_search():
    """The verdict this desk has actually reached, and it must stay reachable."""
    d = deflated_sharpe(RNG.normal(size=400),
                        TrialCensus(2464, 2464.0, "test"))
    assert not d.passes
    assert "best of" in d.why


def test_a_strong_book_passes_against_a_small_search():
    d = deflated_sharpe(RNG.normal(loc=0.35, scale=1.0, size=600),
                        TrialCensus(5, 5.0, "test"))
    assert d.passes


def test_the_same_book_can_fail_purely_because_the_search_was_wider():
    r = RNG.normal(loc=0.16, scale=1.0, size=500)
    narrow = deflated_sharpe(r, TrialCensus(3, 3.0, "t"))
    wide = deflated_sharpe(r, TrialCensus(5000, 5000.0, "t"))
    assert narrow.passes and not wide.passes


def test_both_thresholds_are_always_reported():
    """Showing only the deduplicated one would let this module quietly relax
    every gate it touches."""
    d = deflated_sharpe(RNG.normal(size=300), TrialCensus(100, 12.0, "t"))
    assert d.sr0_raw > d.sr0_effective > 0


def test_a_failure_says_more_variants_will_not_help():
    d = deflated_sharpe(RNG.normal(size=400), TrialCensus(2464, 900.0, "t"))
    assert "RAISES this bar" in d.why and "not more variants" in d.why


def test_a_fat_tailed_book_is_flagged_in_the_render():
    r = np.concatenate([RNG.normal(0.05, 0.3, 480), RNG.normal(-3, 1, 20)])
    d = deflated_sharpe(r, TrialCensus(10, 10.0, "t"))
    assert "fat-tailed" in d.render()


def test_too_few_observations_yields_no_pass():
    d = deflated_sharpe([0.1], TrialCensus(5, 5.0, "t"))
    assert not d.passes and "too few observations" in d.why


def test_the_report_insists_both_counts_stay_visible():
    txt = report(RNG.normal(size=300), TrialCensus(100, 12.0, "t"))
    assert "has to be visible" in txt
