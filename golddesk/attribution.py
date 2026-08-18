"""What is actually driving gold right now — decomposed, not asserted.

`crossmarket.py` states WHAT gold is priced against and the sign each driver is
expected to push. It stops there: it can say "the dollar is up 0.4% and real
yields are up 6bp", and it cannot say how much of gold's −1.2% those two
account for, or whether anything is left over.

The leftover is the entire point. A gold move fully explained by its drivers is
gold behaving like a dollar-denominated zero-coupon asset, which is arithmetic
rather than information. A move that is NOT explained is the desk's own
question: something is bidding gold that is not the dollar, not real yields and
not fear, and that is either a flow the desk cannot see or a regime turning.

THE TRAP THIS MODULE IS BUILT AROUND

Attribution is trivially easy to do in a way that is always right and never
useful. Regress today's gold return on today's driver returns using a window
that INCLUDES today, and the decomposition will fit beautifully — it is a
restatement of the sample, and the residual is whatever the fit could not
absorb rather than a fact about the market. That number explains everything and
predicts nothing, and it will look like a triumph.

So the betas are estimated STRICTLY BEFORE the window being attributed, and
`explained_fraction` is therefore an out-of-sample number that CAN come out
negative — a driver model doing worse than no model at all. A negative reading
is reported as a negative number, because it is the honest answer and it is
also the interesting one.

AND THE TEST THAT DECIDES WHETHER ANY OF THIS IS TRADEABLE

The audit's question, verbatim: does a driver decomposition change any decision
the desk makes, or merely label what already happened? Attribution that is only
explanatory is not tradeable. `residual_predicts_forward()` is that test, and
it is written to be able to return NO. It measures whether an unusually large
unexplained move predicts the next period's return — continuation (someone
knows something) or reversal (an overreaction). If neither, the decomposition
is commentary, and the desk should be told so rather than shown a chart.

WHY RIDGE AND NOT PLAIN OLS

The drivers are heavily collinear by construction: a stronger dollar and higher
real yields are largely the same macro impulse. Plain OLS on collinear inputs
produces enormous offsetting betas that fit the training window and invert
sample to sample, which would show up here as a decomposition that swings wildly
from day to day for no economic reason. A small ridge penalty buys stability at
a known, stated cost: contributions are shrunk toward zero, so the residual is
BIASED UPWARDS. That direction is deliberate — it makes the module understate
how much it explains rather than overstate it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from golddesk.crossmarket import BY_KEY, DRIVERS

ATTRIBUTION_VERSION = "attrib-2026-08-18-a"

#: Fewer training observations than this and the betas are noise wearing a
#: decimal point. Five drivers need considerably more than five rows.
MIN_TRAIN = 60

#: Ridge penalty, on standardised inputs. Small enough to leave real signal,
#: large enough to stop collinear drivers producing +40/−40 offsetting betas.
RIDGE_LAMBDA = 1.0

#: |z| beyond which a residual counts as "unexplained enough to be interesting".
#: The predictive test is run at this threshold and reports the count, so a
#: verdict resting on four observations is visibly resting on four observations.
RESIDUAL_Z = 1.5


@dataclass(frozen=True)
class Contribution:
    key: str
    label: str
    beta: float
    driver_move: float          # the driver's own standardised move
    contribution: float         # in the same units as the gold return
    expected_sign: int
    #: True when the FITTED beta disagrees with the sign crossmarket.py declares.
    #: Not an error — a real and reportable event. The dollar-gold link genuinely
    #: inverts in a debasement panic, and a model that silently re-fits the sign
    #: absorbs exactly the regime change the desk most wants to be told about.
    sign_violation: bool = False

    def render(self) -> str:
        flag = "  <-- SIGN INVERTED vs declared" if self.sign_violation else ""
        return (f"    {self.label:<26} {self.contribution:+8.4f}   "
                f"beta {self.beta:+6.3f}{flag}")


@dataclass
class Attribution:
    """One period's gold move, split into driver contributions and a residual."""
    actual: float
    explained: float
    residual: float
    contributions: tuple[Contribution, ...]
    n_train: int
    explained_fraction: Optional[float] = None   # OOS R^2 over the attributed window
    residual_z: Optional[float] = None
    why: str = ""

    @property
    def dominant(self) -> Optional[Contribution]:
        """The single largest contributor by magnitude, or None if the residual
        beats all of them — which is the answer "nothing you can see"."""
        if not self.contributions:
            return None
        top = max(self.contributions, key=lambda c: abs(c.contribution))
        return top if abs(top.contribution) >= abs(self.residual) else None

    @property
    def verdict(self) -> str:
        d = self.dominant
        if d is None:
            return ("UNEXPLAINED — the residual is larger than any single driver. "
                    "Something is moving gold that the dollar, real yields and "
                    "fear do not account for.")
        return f"{d.label} dominates ({d.contribution:+.4f} of {self.actual:+.4f})"

    def render(self) -> str:
        lines = [f"GOLD MOVE {self.actual:+.4f}",
                 f"  explained {self.explained:+.4f}   "
                 f"residual {self.residual:+.4f}"
                 + (f"  (z {self.residual_z:+.2f})" if self.residual_z is not None else "")]
        if self.explained_fraction is not None:
            lines.append(f"  out-of-sample explained fraction "
                         f"{self.explained_fraction:+.3f}"
                         + ("   NEGATIVE: the driver model is worse than no model"
                            if self.explained_fraction < 0 else ""))
        lines += [c.render() for c in
                  sorted(self.contributions, key=lambda c: -abs(c.contribution))]
        lines.append(f"  {self.verdict}")
        if self.why:
            lines.append(f"  {self.why}")
        return "\n".join(lines)


@dataclass
class BetaFit:
    keys: tuple[str, ...]
    betas: np.ndarray
    mu: np.ndarray
    sd: np.ndarray
    y_mu: float
    n: int

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Standardised with TRAINING moments. Using the attribution window's own
        mean and scale is leakage that looks like nothing and shrinks every
        residual — the exact number this module exists to report."""
        return ((x - self.mu) / self.sd) @ self.betas + self.y_mu


def fit_betas(y: Sequence[float], x: Sequence[Sequence[float]],
              keys: Sequence[str], lam: float = RIDGE_LAMBDA) -> Optional[BetaFit]:
    """Ridge betas of gold on its drivers. None when the sample cannot support it.

    None rather than a fit over twelve rows: a decomposition with no warrant is
    more dangerous than no decomposition, because it prints numbers.
    """
    ya, xa = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    if xa.ndim != 2 or len(ya) != len(xa):
        raise ValueError(f"shape mismatch: y={ya.shape} x={xa.shape}")
    ok = np.isfinite(ya) & np.isfinite(xa).all(axis=1)
    ya, xa = ya[ok], xa[ok]
    if len(ya) < MIN_TRAIN:
        return None
    mu, sd = xa.mean(axis=0), xa.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)          # a constant driver contributes nothing
    xs = (xa - mu) / sd
    y_mu = float(ya.mean())
    k = xs.shape[1]
    betas = np.linalg.solve(xs.T @ xs + lam * np.eye(k), xs.T @ (ya - y_mu))
    return BetaFit(tuple(keys), betas, mu, sd, y_mu, len(ya))


def attribute(actual: float, driver_values: dict, fit: BetaFit,
              residual_sd: Optional[float] = None) -> Attribution:
    """Split one realised gold move into contributions plus a residual."""
    contribs: list[Contribution] = []
    explained = fit.y_mu
    for i, key in enumerate(fit.keys):
        raw = driver_values.get(key)
        if raw is None or not math.isfinite(float(raw)):
            # An absent driver contributes NOTHING and its share falls into the
            # residual. It must not be imputed to the training mean: that would
            # quietly assert "the dollar did its average thing today", which is a
            # claim about a number nobody observed.
            continue
        z = (float(raw) - fit.mu[i]) / fit.sd[i]
        c = float(fit.betas[i] * z)
        d = BY_KEY.get(key)
        exp_sign = d.expected_sign if d else 0
        # The violation test is on the BETA, not on the contribution. A negative
        # contribution from a positive beta only means the driver fell.
        violated = bool(exp_sign) and (
            (fit.betas[i] > 0 and exp_sign < 0) or (fit.betas[i] < 0 and exp_sign > 0))
        contribs.append(Contribution(key, d.label if d else key, float(fit.betas[i]),
                                     z, c, exp_sign, violated))
        explained += c
    residual = actual - explained
    z = (residual / residual_sd) if residual_sd else None
    missing = [k for k in fit.keys if driver_values.get(k) is None]
    why = (f"{len(missing)} driver(s) unobserved ({', '.join(missing)}); their "
           f"share is IN the residual, not imputed." if missing else "")
    return Attribution(actual=actual, explained=explained, residual=residual,
                       contributions=tuple(contribs), n_train=fit.n,
                       residual_z=z, why=why)


def rolling_attribution(y: Sequence[float], x: Sequence[Sequence[float]],
                        keys: Sequence[str], train: int = 250,
                        lam: float = RIDGE_LAMBDA) -> list[Attribution]:
    """Attribute each period using betas fit ONLY on the periods before it.

    The walk-forward is the honesty. Fitting once over the whole history and
    decomposing the same history produces a beautiful, circular result.
    """
    ya, xa = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    out: list[Attribution] = []
    resid_hist: list[float] = []
    for t in range(train, len(ya)):
        fit = fit_betas(ya[:t], xa[:t], keys, lam)
        if fit is None:
            continue
        # Residual scale from PAST residuals only, so "unusually large" is
        # judged against what was known, not against the full sample's spread.
        sd = float(np.std(resid_hist)) if len(resid_hist) >= 30 else None
        a = attribute(float(ya[t]), dict(zip(keys, xa[t])), fit, residual_sd=sd)
        out.append(a)
        resid_hist.append(a.residual)
    return out


def explained_fraction(attrs: Sequence[Attribution]) -> Optional[float]:
    """Out-of-sample R². CAN be negative, and a negative number is reported.

    1 − SSE/SST against the mean of the ACTUALS over the same window. Below zero
    means the driver model did worse than predicting the average, which is a
    real and reportable outcome rather than a bug to clamp away.
    """
    if len(attrs) < 2:
        return None
    a = np.array([x.actual for x in attrs])
    r = np.array([x.residual for x in attrs])
    sst = float(((a - a.mean()) ** 2).sum())
    if sst == 0:
        return None
    return 1.0 - float((r ** 2).sum()) / sst


# ------------------------------------------------------ is any of it tradeable?

@dataclass
class PredictiveTest:
    """Whether an unexplained move says anything about the NEXT one."""
    n: int
    mean_forward_after_large_residual: Optional[float]
    mean_forward_baseline: Optional[float]
    direction: str                  # CONTINUATION | REVERSAL | NONE | INSUFFICIENT
    t_stat: Optional[float]
    why: str

    @property
    def tradeable(self) -> bool:
        return self.direction in ("CONTINUATION", "REVERSAL")

    def render(self) -> str:
        if not self.tradeable:
            return (f"NOT TRADEABLE — {self.why}\n"
                    f"  The decomposition explains the past. It does not change "
                    f"a decision, and should be read as commentary.")
        return (f"{self.direction} after large unexplained moves (n={self.n}, "
                f"t={self.t_stat:+.2f})\n  mean forward "
                f"{self.mean_forward_after_large_residual:+.5f} vs baseline "
                f"{self.mean_forward_baseline:+.5f}\n  {self.why}")


def residual_predicts_forward(attrs: Sequence[Attribution],
                              forward: Sequence[float],
                              z_threshold: float = RESIDUAL_Z,
                              min_n: int = 30) -> PredictiveTest:
    """THE TEST THAT DECIDES WHETHER ATTRIBUTION IS WORTH ANYTHING.

    Signed by residual direction: a large POSITIVE unexplained move followed by
    further gains is continuation; followed by losses, reversal. Taking the raw
    forward return without that sign would let a rising market masquerade as a
    result, since gold drifts up and every cohort would inherit the drift.

    Written to be able to return NONE, which on this desk's record is the most
    likely answer and is a finding, not a failure.
    """
    n_in = min(len(attrs), len(forward))
    a, f = list(attrs)[:n_in], list(forward)[:n_in]
    picked = [(x, fw) for x, fw in zip(a, f)
              if x.residual_z is not None and abs(x.residual_z) >= z_threshold
              and math.isfinite(fw)]
    base = [fw for x, fw in zip(a, f) if math.isfinite(fw)]
    if len(picked) < min_n:
        return PredictiveTest(
            len(picked), None, None, "INSUFFICIENT", None,
            f"only {len(picked)} periods with |residual z| >= {z_threshold}; "
            f"{min_n} required. No verdict either way.")

    signed = np.array([fw * (1.0 if x.residual > 0 else -1.0) for x, fw in picked])
    b = np.array(base, dtype=float)
    m, sd = float(signed.mean()), float(signed.std(ddof=1))
    t = m / (sd / math.sqrt(len(signed))) if sd > 0 else 0.0

    # |t| >= 2 as the bar, stated rather than tuned. This is one test on one
    # series and it is not corrected for the many the desk has already run —
    # which is precisely why the result belongs in the hypothesis registry
    # before it is allowed to change a decision.
    if abs(t) < 2.0:
        return PredictiveTest(
            len(signed), m, float(b.mean()), "NONE", t,
            f"signed forward return after an unexplained move is {m:+.5f} "
            f"(t={t:+.2f}); indistinguishable from noise. The decomposition "
            f"labels the past and does not predict the next period.")
    return PredictiveTest(
        len(signed), m, float(b.mean()),
        "CONTINUATION" if m > 0 else "REVERSAL", t,
        "Uncorrected for the multiplicity this desk has already accumulated. "
        "Seal it as a hypothesis and confirm it forward before it changes a "
        "decision — see golddesk/hypothesis.py.")


def report(attrs: Sequence[Attribution],
           test: Optional[PredictiveTest] = None) -> str:
    if not attrs:
        return "no attributions — the training window was never satisfied."
    ef = explained_fraction(attrs)
    lines = [f"DRIVER ATTRIBUTION  ({ATTRIBUTION_VERSION})",
             f"  periods attributed        {len(attrs)}",
             f"  out-of-sample explained   "
             + ("n/a" if ef is None else f"{ef:+.3f}")]
    if ef is not None and ef < 0:
        lines.append("    NEGATIVE — the driver model predicts gold worse than "
                     "its own mean does. Report it, do not clamp it.")
    inverted = sorted({c.label for a in attrs for c in a.contributions
                       if c.sign_violation})
    if inverted:
        lines.append(f"  SIGN INVERSIONS           {', '.join(inverted)}")
        lines.append("    A fitted beta against the declared sign is a regime "
                     "statement, not a fitting artefact to absorb.")
    unexplained = sum(1 for a in attrs if a.dominant is None)
    lines.append(f"  residual-dominated        {unexplained}/{len(attrs)}")
    lines.append("")
    lines.append(test.render() if test else
                 "no predictive test run — attribution is unproven as tradeable.")
    return "\n".join(lines)
