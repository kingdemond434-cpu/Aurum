"""An attribution model that cannot come out negative is a restatement of its
own training sample. Most of these tests check that it can fail.
"""
from __future__ import annotations

import numpy as np
import pytest

from golddesk.attribution import (
    MIN_TRAIN, Attribution, attribute, explained_fraction, fit_betas,
    report, residual_predicts_forward, rolling_attribution)

RNG = np.random.default_rng(7)
KEYS = ("dxy", "real_yield_10y", "spx")


def driven(n=600, betas=(-0.8, -0.5, -0.2), noise=0.3):
    """Gold genuinely driven by its drivers, plus noise."""
    x = RNG.normal(size=(n, 3))
    y = x @ np.array(betas) + RNG.normal(scale=noise, size=n)
    return y, x


def undriven(n=600):
    """Gold and drivers unrelated. The honest answer is 'explains nothing'."""
    return RNG.normal(size=n), RNG.normal(size=(n, 3))


# --------------------------------------------------------------- the fit

def test_a_thin_sample_gets_no_betas_at_all():
    """A decomposition with no warrant is worse than none: it prints numbers."""
    y, x = driven(n=MIN_TRAIN - 1)
    assert fit_betas(y, x, KEYS) is None


def test_real_relationships_are_recovered():
    y, x = driven(betas=(-0.8, -0.5, -0.2), noise=0.2)
    fit = fit_betas(y, x, KEYS)
    assert fit.betas[0] < -0.4 and fit.betas[1] < -0.2


def test_ridge_keeps_collinear_drivers_from_exploding():
    """A stronger dollar and higher real yields are largely one impulse. Plain
    OLS answers with enormous offsetting betas that invert sample to sample."""
    n = 400
    a = RNG.normal(size=n)
    x = np.column_stack([a, a + RNG.normal(scale=0.01, size=n), RNG.normal(size=n)])
    y = -a + RNG.normal(scale=0.1, size=n)
    fit = fit_betas(y, x, KEYS)
    assert max(abs(fit.betas)) < 5.0, f"betas exploded: {fit.betas}"


def test_a_constant_driver_contributes_nothing_rather_than_dividing_by_zero():
    y, x = driven()
    x[:, 2] = 1.0
    fit = fit_betas(y, x, KEYS)
    assert fit is not None and np.isfinite(fit.betas).all()


def test_standardisation_uses_training_moments_only():
    """Using the attributed window's own mean and scale shrinks every residual —
    the exact number this module exists to report."""
    y, x = driven()
    fit = fit_betas(y[:300], x[:300], KEYS)
    a = fit.predict(x[300:310])
    b = fit.predict(x[300:310] + 100.0)
    assert not np.allclose(a, b), "a shifted window changed nothing — leaked moments"


# ------------------------------------------------------ it must be able to fail

def test_unrelated_drivers_explain_essentially_nothing():
    """THE TEST THAT MAKES A POSITIVE READING MEAN ANYTHING."""
    y, x = undriven()
    attrs = rolling_attribution(y, x, KEYS, train=250)
    ef = explained_fraction(attrs)
    assert ef < 0.05, f"explained {ef:+.3f} of pure noise"


def test_the_explained_fraction_can_come_out_negative():
    """Below zero means the driver model did worse than predicting the average.
    A real outcome, not a bug to clamp away."""
    n = 500
    x = RNG.normal(size=(n, 3))
    y = x @ np.array([-1.0, 0.0, 0.0])
    y[300:] = -y[300:]                      # the relationship inverts out of sample
    attrs = rolling_attribution(y, x, KEYS, train=250)
    assert explained_fraction(attrs) < 0


def test_a_genuine_relationship_is_explained_out_of_sample():
    y, x = driven(n=800, noise=0.2)
    attrs = rolling_attribution(y, x, KEYS, train=300)
    assert explained_fraction(attrs) > 0.5


def test_betas_never_see_the_period_they_attribute():
    """Fit once over the whole history and decompose the same history and the
    result is beautiful and circular."""
    y, x = driven(n=400)
    attrs = rolling_attribution(y, x, KEYS, train=300)
    assert all(a.n_train <= 399 for a in attrs)
    assert attrs[0].n_train == 300


# --------------------------------------------------------------- the residual

def test_an_absent_driver_falls_into_the_residual_and_is_not_imputed():
    """Imputing the training mean asserts 'the dollar did its average thing
    today' — a claim about a number nobody observed."""
    y, x = driven()
    fit = fit_betas(y, x, KEYS)
    a = attribute(1.0, {"dxy": 0.5, "real_yield_10y": None, "spx": 0.1}, fit)
    assert len(a.contributions) == 2
    assert "unobserved" in a.why and "real_yield_10y" in a.why


def test_the_residual_closes_the_arithmetic():
    y, x = driven()
    fit = fit_betas(y, x, KEYS)
    a = attribute(0.42, dict(zip(KEYS, [0.3, -0.2, 0.1])), fit)
    assert abs((a.explained + a.residual) - a.actual) < 1e-12


def test_a_move_the_drivers_do_not_explain_reads_as_unexplained():
    """The answer 'something is bidding gold that you cannot see' has to be
    reachable, or the module can only ever agree with itself."""
    y, x = driven(noise=0.05)
    fit = fit_betas(y, x, KEYS)
    a = attribute(9.0, dict(zip(KEYS, [0.0, 0.0, 0.0])), fit)
    assert a.dominant is None
    assert "UNEXPLAINED" in a.verdict


# ---------------------------------------------------------- the declared sign

def test_a_beta_against_the_declared_sign_is_flagged_not_absorbed():
    """The dollar-gold link genuinely inverts in a debasement panic. A model
    that silently re-fits the sign absorbs the regime change the desk most
    wants to be told about."""
    n = 400
    x = RNG.normal(size=(n, 3))
    y = x @ np.array([+1.0, 0.0, 0.0])     # gold RISES with the dollar
    fit = fit_betas(y, x, KEYS)
    a = attribute(0.5, dict(zip(KEYS, [1.0, 0.0, 0.0])), fit)
    dxy = next(c for c in a.contributions if c.key == "dxy")
    assert dxy.sign_violation
    assert "SIGN INVERTED" in dxy.render()


def test_the_violation_is_on_the_beta_not_the_contribution():
    """A negative contribution from a negative beta only means the driver rose."""
    y, x = driven(betas=(-0.8, -0.5, -0.2))
    fit = fit_betas(y, x, KEYS)
    a = attribute(0.0, dict(zip(KEYS, [2.0, 0.0, 0.0])), fit)
    dxy = next(c for c in a.contributions if c.key == "dxy")
    assert dxy.contribution < 0 and not dxy.sign_violation


# ------------------------------------------------------ is it tradeable at all?

def _attrs_with_residuals(res, sd=1.0):
    return [Attribution(actual=r, explained=0.0, residual=r, contributions=(),
                        n_train=100, residual_z=r / sd) for r in res]


def test_a_thin_cohort_returns_no_verdict_rather_than_a_weak_one():
    a = _attrs_with_residuals([3.0] * 5)
    t = residual_predicts_forward(a, [0.1] * 5)
    assert t.direction == "INSUFFICIENT" and not t.tradeable


def test_noise_is_reported_as_not_tradeable():
    """The most likely answer on this desk's record, and a finding not a
    failure."""
    res = RNG.normal(scale=2.0, size=400)
    t = residual_predicts_forward(_attrs_with_residuals(res),
                                  RNG.normal(scale=0.01, size=400))
    assert t.direction == "NONE" and not t.tradeable
    assert "does not predict" in t.why


def test_a_planted_continuation_is_found():
    res = RNG.normal(scale=2.0, size=600)
    fwd = np.sign(res) * 0.02 + RNG.normal(scale=0.005, size=600)
    t = residual_predicts_forward(_attrs_with_residuals(res), fwd)
    assert t.direction == "CONTINUATION" and t.tradeable


def test_a_planted_reversal_is_found_and_not_confused_with_continuation():
    res = RNG.normal(scale=2.0, size=600)
    fwd = -np.sign(res) * 0.02 + RNG.normal(scale=0.005, size=600)
    t = residual_predicts_forward(_attrs_with_residuals(res), fwd)
    assert t.direction == "REVERSAL" and t.tradeable


def test_drift_alone_does_not_manufacture_a_result():
    """Gold drifts up. Without signing by residual direction every cohort
    inherits the drift and looks like continuation."""
    res = RNG.normal(scale=2.0, size=600)
    fwd = np.full(600, 0.05)               # pure upward drift, no relationship
    t = residual_predicts_forward(_attrs_with_residuals(res), fwd)
    assert t.direction == "NONE", f"drift alone produced {t.direction}"


def test_a_positive_result_says_it_is_uncorrected_for_multiplicity():
    res = RNG.normal(scale=2.0, size=600)
    fwd = np.sign(res) * 0.02 + RNG.normal(scale=0.005, size=600)
    t = residual_predicts_forward(_attrs_with_residuals(res), fwd)
    assert "multiplicity" in t.why and "hypothesis" in t.why


# ------------------------------------------------------------------- reporting

def test_the_report_says_when_nothing_was_proven_tradeable():
    y, x = undriven()
    attrs = rolling_attribution(y, x, KEYS, train=250)
    assert "unproven as tradeable" in report(attrs)


def test_the_report_surfaces_a_negative_explained_fraction():
    n = 500
    x = RNG.normal(size=(n, 3))
    y = x @ np.array([-1.0, 0.0, 0.0])
    y[300:] = -y[300:]
    attrs = rolling_attribution(y, x, KEYS, train=250)
    assert "NEGATIVE" in report(attrs)


def test_an_empty_run_is_reported_not_crashed():
    assert "never satisfied" in report([])
