"""Scoring a probabilistic gold model, and pricing the search that produced it.

Two things the desk's existing evaluation cannot do, both needed the moment
anything emits a probability rather than a trade.

  1. PROBABILITY QUALITY, separately from economic value. A model can be
     profitable and badly calibrated, or beautifully calibrated and useless.
     Net R answers neither question. Brier, log loss, calibration error and
     sharpness do, and they answer different ones.

  2. THE PRICE OF A SEARCH. "Let the machine discover interactions" is the most
     expensive sentence in quantitative research, and the cost is computable
     before any code runs. `interaction_budget()` returns the number of
     hypotheses a k-way search actually tests and the effect size required to
     survive multiple-testing correction at that count. On a sample the size of
     gold's, the answer is usually that the search cannot succeed — which is
     worth knowing in an afternoon rather than a quarter.

A NOTE ON STATED PRECISION

Reporting "bull 66%" asserts two significant figures. Whether the sample
supports two significant figures is an arithmetic question with an answer, and
it is usually no. `reportable_precision()` computes it. A probability quoted
more precisely than its standard error is not a forecast, it is a decimal point
doing rhetorical work — and downstream, a model reading it will condition on the
digits as though they were real.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

EPS = 1e-12


# --------------------------------------------------------------------------
# Proper scoring rules
# --------------------------------------------------------------------------

def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of a probability. Lower is better; 0.25 = coin flip."""
    return statistics.fmean((p - o) ** 2 for p, o in zip(probs, outcomes))


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Penalises confident wrongness hard, which is the failure that costs money."""
    tot = 0.0
    for p, o in zip(probs, outcomes):
        q = min(max(p, EPS), 1 - EPS)
        tot += -(o * math.log(q) + (1 - o) * math.log(1 - q))
    return tot / max(len(probs), 1)


def base_rate_scores(outcomes: Sequence[int]) -> tuple[float, float]:
    """What a model that only knows the base rate would score. THE benchmark.

    Beating 0.25 Brier is not a result: it only means the outcome is not 50/50.
    The honest comparison is against always predicting the sample's own base
    rate, and a great many published models do not beat it.
    """
    if not outcomes:
        return float("nan"), float("nan")
    p = statistics.fmean(outcomes)
    return brier([p] * len(outcomes), outcomes), log_loss([p] * len(outcomes), outcomes)


@dataclass
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    observed: float

    @property
    def gap(self) -> float:
        return self.observed - self.mean_pred


def calibration(probs: Sequence[float], outcomes: Sequence[int],
                bins: int = 10) -> list[CalibrationBin]:
    """Does 70% mean 70%? Binned reliability."""
    out: list[CalibrationBin] = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs)
               if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        out.append(CalibrationBin(
            lo, hi, len(idx),
            statistics.fmean(probs[i] for i in idx),
            statistics.fmean(outcomes[i] for i in idx)))
    return out


def expected_calibration_error(probs: Sequence[float],
                               outcomes: Sequence[int], bins: int = 10) -> float:
    cs = calibration(probs, outcomes, bins)
    n = sum(c.n for c in cs) or 1
    return sum(c.n * abs(c.gap) for c in cs) / n


def sharpness(probs: Sequence[float]) -> float:
    """Spread of the forecasts themselves.

    A model that always says 50% is perfectly calibrated and completely useless.
    Calibration without sharpness is a thermometer that reports the seasonal
    average, so the two must always be read together.
    """
    return statistics.pstdev(probs) if len(probs) > 1 else 0.0


# --------------------------------------------------------------------------
# How precisely may a probability be stated?
# --------------------------------------------------------------------------

@dataclass
class Precision:
    p: float
    n: float
    se: float
    ci95: tuple
    reportable: str
    verdict: str

    def render(self) -> str:
        return (f"  p={self.p:.3f} on n={self.n:.0f} -> SE {self.se:.3f}, "
                f"95% CI [{self.ci95[0]:.2f}, {self.ci95[1]:.2f}]\n"
                f"  honest statement: {self.reportable}   ({self.verdict})")


def reportable_precision(p: float, n: float) -> Precision:
    """How many digits of a probability the sample actually supports.

    "bull 66%" claims two significant figures. On 433 weekly observations the
    standard error of a probability near 0.66 is about 0.023, so the 95%
    interval spans roughly 9 percentage points and the second digit is noise.
    Condition that estimate on a regime and n falls to ~80, the interval widens
    past 20 points, and the number should be read as "probably above even" and
    nothing finer.

    This matters more than it sounds. A downstream reader — human or model —
    conditions on the digits presented. Quoting 66% rather than "roughly
    two-thirds, wide error" invites a confidence the data does not contain.
    """
    p = min(max(p, EPS), 1 - EPS)
    se = math.sqrt(p * (1 - p) / max(n, 1))
    lo, hi = max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)
    width = hi - lo
    if width < 0.02:
        rep, v = f"{p * 100:.1f}%", "two decimals defensible"
    elif width < 0.10:
        rep, v = f"{round(p * 100):.0f}%", "whole percent defensible"
    elif width < 0.25:
        rep, v = f"~{round(p * 20) * 5:.0f}%", "nearest 5% at best"
    else:
        direction = "above even" if p > 0.55 else ("below even" if p < 0.45 else "near even")
        rep, v = direction, "NO numeric probability is supportable at this n"
    return Precision(p, n, se, (lo, hi), rep, v)


# --------------------------------------------------------------------------
# The price of an interaction search
# --------------------------------------------------------------------------

@dataclass
class SearchBudget:
    n_features: int
    max_order: int
    hypotheses: int
    ess: float
    required_p: float
    required_t: float
    required_ic: float
    verdict: str

    def render(self) -> str:
        return (f"  {self.n_features} features, interactions to order {self.max_order}"
                f"  ->  {self.hypotheses:,} hypotheses\n"
                f"  strictest BH threshold p <= {self.required_p:.2e}  "
                f"(t ~ {self.required_t:.1f} on ESS {self.ess:.0f})\n"
                f"  required information coefficient: |IC| >= {self.required_ic:.3f}\n"
                f"  {self.verdict}")


def interaction_budget(n_features: int, max_order: int, ess: float,
                       q: float = 0.10, typical_macro_ic: float = 0.05
                       ) -> SearchBudget:
    """What a k-way interaction search costs, before writing any of it.

    The count of hypotheses is combinatorial and the correction is linear in it,
    so the effect size required to survive rises fast while the sample stays the
    same size. This converts "let the machine find interactions" into a number
    you can compare against what macro effects actually look like.

    `typical_macro_ic` is the yardstick, not a claim about your data: published
    macro-to-price information coefficients generally sit in the 0.02-0.05 band.
    An interaction search demanding several times that is not going to find a
    real effect; it is going to find the best of many accidents.
    """
    h = sum(math.comb(n_features, k) for k in range(1, max_order + 1))
    # Benjamini-Hochberg: the most significant hypothesis must clear q/m.
    required_p = q / max(h, 1)
    # two-sided normal approximation for the t needed at that p
    t = _z_for_two_sided(required_p)
    denom = max(ess - 2, 1)
    ic = t / math.sqrt(denom + t * t)          # invert t = IC*sqrt(n-2)/sqrt(1-IC^2)
    if ic <= typical_macro_ic:
        v = "FEASIBLE — the required effect is within the range macro effects occupy"
    elif ic <= typical_macro_ic * 2:
        v = ("MARGINAL — requires roughly double a typical macro effect; possible "
             "but expect nothing to survive")
    else:
        v = (f"INFEASIBLE — demands ~{ic / typical_macro_ic:.0f}x a typical macro "
             f"effect. Anything this search reports will be the best of "
             f"{h:,} accidents, not a finding. Reduce the order, cut the feature "
             f"count, or get more independent observations")
    return SearchBudget(n_features, max_order, h, ess, required_p, t, ic, v)


def _z_for_two_sided(p: float) -> float:
    """Normal quantile for a two-sided p, by bisection. No scipy dependency."""
    target = 1.0 - p / 2.0
    lo, hi = 0.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------
# The dataset survival test — base vs base + new data, same states
# --------------------------------------------------------------------------

@dataclass
class DatasetVerdict:
    name: str
    n_paired: int
    d_brier: float                  # negative is better
    d_log_loss: float
    d_ece: float
    d_sharpness: float
    beats_base_rate: bool
    verdict: str

    def render(self) -> str:
        return (f"  {self.name:<28} n={self.n_paired:<5} "
                f"dBrier={self.d_brier:+.5f}  dLogLoss={self.d_log_loss:+.5f}  "
                f"dECE={self.d_ece:+.4f}  dSharp={self.d_sharpness:+.4f}\n"
                f"  {'':<28} {self.verdict}")


def dataset_survival(name: str, base_probs: Sequence[float],
                     augmented_probs: Sequence[float],
                     outcomes: Sequence[int]) -> DatasetVerdict:
    """Does adding this dataset improve the FORECAST, on identical timestamps?

    The comparison must be paired on the same states and the same outcomes, or
    it measures which period each model happened to see. Improvement is required
    on probability quality here; whether that converts into money is a separate
    question answered by the economic ablation, and a dataset must clear both.
    """
    n = min(len(base_probs), len(augmented_probs), len(outcomes))
    b, a, o = base_probs[:n], augmented_probs[:n], outcomes[:n]
    d_brier = brier(a, o) - brier(b, o)
    d_ll = log_loss(a, o) - log_loss(b, o)
    d_ece = expected_calibration_error(a, o) - expected_calibration_error(b, o)
    d_sharp = sharpness(a) - sharpness(b)
    base_b, _ = base_rate_scores(o)
    beats = brier(a, o) < base_b

    if not beats:
        v = ("REJECT — the augmented model does not beat predicting the base "
             "rate, so the dataset cannot be rescuing anything")
    elif d_brier < 0 and d_ll < 0:
        v = "SURVIVES probability scoring — now test whether it converts to net R"
    elif d_brier < 0 or d_ll < 0:
        v = ("SPLIT — the two proper scoring rules disagree, which usually means "
             "a few confident calls carried it. Inspect the tail before believing it")
    else:
        v = "REJECT — no improvement in probability quality. Delete the dataset"
    return DatasetVerdict(name, n, d_brier, d_ll, d_ece, d_sharp, beats, v)
