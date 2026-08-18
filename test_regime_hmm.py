"""The leak in nearly every public HMM regime detector is `predict()` — Viterbi
over the whole series, labelling today with tomorrow's data. Most of these tests
exist to prove the causal path really is causal.
"""
from __future__ import annotations

import numpy as np
import pytest

from golddesk.regime_hmm import (
    DEFAULT_STATES, MIN_TRAIN, contest, features, fit_hmm, render,
    separation, shuffled_null, smoothing_advantage)

RNG = np.random.default_rng(11)


def two_regime(n=1600, switch=0.02):
    """A series that genuinely has two volatility regimes."""
    r, state = [], 0
    for _ in range(n):
        if RNG.random() < switch:
            state = 1 - state
        r.append(RNG.normal(scale=0.3 if state == 0 else 1.6))
    return np.array(r)


# ------------------------------------------------------------ causality

def test_filtering_uses_no_future_observation():
    """THE TEST THIS MODULE EXISTS FOR. Changing the tail must not change any
    earlier label. If it does, the desk is labelling today with tomorrow."""
    r = two_regime()
    m = fit_hmm(features(r)[:800])
    x = features(r)
    a = m.filter_states(x[:1000])
    b = m.filter_states(np.vstack([x[:1000], x[:200] * 50.0]))[:1000]
    assert np.allclose(a, b), "a future observation changed a past belief"


def test_smoothing_does_use_the_future_which_is_why_it_is_banned():
    """Stated as a positive assertion so the difference is demonstrated rather
    than asserted in a comment."""
    r = two_regime()
    m = fit_hmm(features(r)[:800])
    x = features(r)
    a = m.smooth_states(x[:1000])
    b = m.smooth_states(np.vstack([x[:1000], x[:200] * 50.0]))[:1000]
    assert not np.allclose(a, b), "smoothing ignored the future — then it is not smoothing"


def test_the_causal_volatility_window_is_trailing_not_centred():
    """A centred window is Viterbi's leak in different clothes and far easier to
    write by accident."""
    r = np.concatenate([np.zeros(50), np.full(50, 5.0)])
    f = features(r, window=10)
    assert f[40, 1] == 0.0, "volatility at bar 40 already saw the bar-50 jump"


def test_labels_come_from_the_filtered_posterior():
    r = two_regime()
    m = fit_hmm(features(r)[:800])
    x = features(r)[:400]
    assert np.array_equal(m.labels(x), m.filter_states(x).argmax(axis=1))


# --------------------------------------------------------------- the fit

def test_a_thin_sample_is_refused():
    assert fit_hmm(features(RNG.normal(size=MIN_TRAIN - 1))) is None


def test_two_volatility_regimes_are_recovered():
    r = two_regime()
    m = fit_hmm(features(r), n_states=2)
    lo, hi = sorted(m.means[:, 1])
    assert hi > 2 * lo, f"states did not separate volatility: {m.means[:, 1]}"


def test_no_state_collapses_to_zero_variance():
    """Without a floor a state lands on two observations, its likelihood goes to
    infinity, and the fit is destroyed in a way that looks like convergence."""
    r = np.concatenate([np.zeros(400), RNG.normal(size=400)])
    m = fit_hmm(features(r), n_states=3)
    assert (m.var > 0).all() and np.isfinite(m.loglik)


def test_the_likelihood_does_not_decrease():
    """EM guarantees monotone improvement; a decrease means the M-step is wrong."""
    r = two_regime(n=800)
    lls = []
    for it in (1, 3, 10, 40):
        m = fit_hmm(features(r), n_states=2, max_iter=it)
        lls.append(m.loglik)
    assert all(b >= a - 1e-6 for a, b in zip(lls, lls[1:])), lls


def test_transition_and_start_rows_are_distributions():
    m = fit_hmm(features(two_regime()), n_states=3)
    assert np.allclose(m.trans.sum(axis=1), 1.0)
    assert np.isclose(m.start.sum(), 1.0)


def test_posteriors_are_normalised():
    m = fit_hmm(features(two_regime()), n_states=3)
    p = m.filter_states(features(two_regime(n=300)))
    assert np.allclose(p.sum(axis=1), 1.0)


def test_a_long_series_does_not_underflow():
    """Unscaled forward recursions die silently around a few hundred bars."""
    r = two_regime(n=4000)
    m = fit_hmm(features(r)[:1000], n_states=3)
    p = m.filter_states(features(r))
    assert np.isfinite(p).all() and p.sum(axis=1).min() > 0.99


# ------------------------------------------------------- the two separations

def test_direction_and_volatility_are_scored_separately():
    """A labelling that sorts volatility perfectly and says nothing about
    direction must show exactly that, not a blended score."""
    lab = np.array([0] * 500 + [1] * 500)
    fwd = np.concatenate([RNG.normal(scale=0.2, size=500),
                          RNG.normal(scale=3.0, size=500)])
    s = separation("vol only", lab, fwd)
    assert s.volatility_f > 5.0
    assert s.direction_f < 1.0


def test_a_direction_separating_labelling_is_detected():
    lab = np.array([0] * 500 + [1] * 500)
    fwd = np.concatenate([RNG.normal(loc=-1.0, size=500),
                          RNG.normal(loc=+1.0, size=500)])
    assert separation("dir", lab, fwd).direction_f > 20.0


def test_the_shuffled_null_destroys_the_relationship():
    lab = np.array([0] * 500 + [1] * 500)
    fwd = np.concatenate([RNG.normal(loc=-1.0, size=500),
                          RNG.normal(loc=+1.0, size=500)])
    assert shuffled_null(lab, fwd).direction_f < 8.0


def test_the_null_keeps_the_same_marginal_frequencies():
    lab = np.array([0] * 700 + [1] * 300)
    n = shuffled_null(lab, RNG.normal(size=1000))
    assert n.n_states == 2 and n.n == 1000


def test_the_null_is_a_distribution_not_a_single_draw():
    """ONE shuffle is itself a draw from the same distribution as a worthless
    entrant, so roughly half of all pure-noise labellings would 'beat' it. The
    contest test caught exactly that. The floor is a 95th percentile."""
    lab = RNG.integers(0, 3, size=1200)
    fwd = RNG.normal(size=1200)
    n = shuffled_null(lab, fwd)
    assert "p95" in n.name and "coin flip" in n.why
    one_draw = separation("one", RNG.permutation(lab), fwd)
    assert n.direction_f > one_draw.direction_f or n.direction_f > 0.5


def test_pure_noise_labellings_rarely_clear_the_null():
    """The property a single shuffle did not have: an entrant with no signal
    should pass only about 5% of the time, not half."""
    beats = 0
    for s in range(40):
        rng = np.random.default_rng(100 + s)
        fwd = rng.normal(size=900)
        lab = rng.integers(0, 3, size=900)
        if separation("x", lab, fwd).direction_f > shuffled_null(lab, fwd, seed=s).direction_f:
            beats += 1
    assert beats <= 8, f"{beats}/40 noise labellings cleared the null"


def test_an_empty_scoring_set_reports_rather_than_crashes():
    assert separation("x", [], []).n == 0


# ------------------------------------------------------- the lookahead gap

def test_the_smoothing_advantage_is_measured_not_merely_warned_about():
    r = two_regime()
    m = fit_hmm(features(r)[:800], n_states=2)
    x = features(r)[800:]
    fwd = np.roll(r, -1)[800:]
    adv = smoothing_advantage(m, x, fwd)
    assert "filtered" in adv and "smoothed" in adv
    assert "LOOKAHEAD" in adv["smoothed"].name
    assert isinstance(adv["volatility_inflation"], float)


# ----------------------------------------------------------------- contest

def test_the_contest_can_say_neither_beat_the_null():
    """On this desk's record the likely answer, and a finding not a failure."""
    n = 2000
    r = RNG.normal(size=n)
    fwd = np.roll(r, -1)
    inc = RNG.integers(0, 3, size=n)
    out = contest(r, inc, fwd, train=800)
    assert out["beat_null_on_direction"] == []
    assert "NEITHER" in out["verdict"]
    assert "must not be allowed to argue about direction" in out["verdict"]


def test_the_contest_is_paired_on_identical_bars():
    """Scoring the incumbent on one span and the challenger on another measures
    which span was kinder."""
    n = 2000
    r = two_regime(n)
    out = contest(r, RNG.integers(0, 3, size=n), np.roll(r, -1), train=800)
    assert all(e.n == out["n_test"] for e in out["entrants"])


def test_a_genuinely_better_incumbent_wins():
    """The contest must be able to decide FOR the rules, or it is a rigged
    demonstration of the new toy."""
    n = 2000
    r = two_regime(n)
    fwd = np.roll(r, -1)
    inc = (np.arange(n) // 500) % 2
    fwd = fwd + np.where(inc == 1, 3.0, -3.0)      # the incumbent knows direction
    out = contest(r, inc, fwd, train=800)
    assert out["best_direction"] == "incumbent (rules)"


def test_a_short_series_is_refused_rather_than_scored():
    out = contest(RNG.normal(size=100), RNG.integers(0, 2, size=100),
                  RNG.normal(size=100), train=80)
    assert out["entrants"] == [] and "too short" in out["verdict"]


def test_the_hmm_is_fitted_on_training_rows_only():
    n = 2000
    r = two_regime(n)
    out = contest(r, RNG.integers(0, 3, size=n), np.roll(r, -1), train=900)
    assert out["n_test"] == n - 900


def test_render_shows_the_lookahead_line():
    n = 2000
    r = two_regime(n)
    txt = render(contest(r, RNG.integers(0, 3, size=n), np.roll(r, -1), train=800))
    assert "lookahead check" in txt and "Filtered is the only labelling" in txt


def test_a_positive_result_says_it_is_uncorrected():
    n = 2000
    r = two_regime(n)
    fwd = np.roll(r, -1)
    inc = (np.arange(n) // 500) % 2
    fwd = fwd + np.where(inc == 1, 3.0, -3.0)
    out = contest(r, inc, fwd, train=800)
    assert "uncorrected" in out["verdict"] and "Seal" in out["verdict"].replace("seal", "Seal")
