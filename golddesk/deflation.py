"""How good does a result have to look, given how hard we searched?

The desk's stated goal is maximum frequency and maximum capture. Both are right,
and both are the exact conditions under which a research process produces
impressive numbers that are not edges: more signals means more variants tested,
and the best of many noisy trials looks excellent by construction. This module
is the arithmetic that keeps the first goal from silently becoming the second.

THE DENOMINATOR COMES FROM `linkage.py`, NOT FROM MEMORY

A deflated Sharpe threshold scales with E[max of N trials] — how high the best
of N unrelated coin-flip strategies would look on luck alone. N is therefore
load-bearing, and a desk that supplies it from recollection supplies a number
biased downward, because nobody remembers the tests that found nothing.
`linkage.trial_census()` counts every registered run including abandoned ones,
and `census_from_registry()` welds that count to this threshold. The two modules
are one mechanism split across two files.

BUT THE RAW COUNT IS ALSO WRONG, IN THE OTHER DIRECTION

E[max of N] is derived for N INDEPENDENT draws. A parameter sweep manufactures
near-copies structurally: the same mechanism at ttl=12 and ttl=13 is one search
sampled twice, not two searches. Feeding the raw count into a threshold built
for independent trials computes it for a far wider search than was really run,
and kills genuine edges by arithmetic.

So effective trials come from the participation ratio of the trial correlation
spectrum, (Σλ)²/Σλ². N identical columns give 1; N independent columns give N;
block structure — which is what a sweep actually produces — is handled without
anyone choosing a clustering threshold. It is preferred to N/(1+(N−1)ρ̄) because
a mean correlation collapses the difference between "twelve mildly related
cells" and "two tight clusters of six", and a sweep is always the second.

THE GUARD, WHICH MATTERS MORE THAN THE MEASURE

Lowering N makes every threshold easier, so this is a tempting place to
manufacture passes. The method is fixed here rather than passed in; both counts
are always reported together with the deflation at each; N_eff is floored at 2
and capped at N_raw so it can never exceed the search performed; and a
correlation matrix that cannot be built returns N_raw unchanged. Absence of a
deduplication is never permission to assume one. Lowering a trial count is a
claim requiring evidence exactly as much as raising a Sharpe.

WHY THE SHARPE STANDARD ERROR IS NOT 1/sqrt(T)

The textbook standard error assumes normal returns. A gold trend book is
negatively skewed and fat-tailed — it wins small and often and loses large and
rarely — and for exactly that shape the naive standard error is TOO SMALL, so
every t-statistic the desk computes is too big. The Bailey–López de Prado
correction carries skew and kurtosis into the estimate and is used throughout
here, because on this desk's return shape the difference is the difference
between a result and a nice-looking sample.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np

DEFLATION_VERSION = "deflate-2026-08-18-a"

#: Correlation at or above which two trials are REPORTED as the same bet. The
#: listing only — N_eff never depends on it, precisely so no threshold choice
#: anyone makes can move a gate.
CLONE_RHO = 0.95

#: Series shorter than this cannot support a correlation worth acting on.
MIN_SERIES = 30


@dataclass(frozen=True)
class TrialCensus:
    n_raw: int
    n_effective: float
    method: str
    clone_pairs: list = field(default_factory=list)
    why: str = ""

    @property
    def inflation(self) -> float:
        """How many times larger the raw count is than the search performed."""
        return self.n_raw / self.n_effective if self.n_effective > 0 else 1.0


def _corr_matrix(cols: list) -> Optional[np.ndarray]:
    if len(cols) < 2:
        return None
    m = np.column_stack(cols)
    if m.shape[0] < MIN_SERIES:
        return None
    keep = m.std(axis=0) > 0        # a constant column has no correlation, not zero
    if keep.sum() < 2:
        return None
    c = np.corrcoef(m[:, keep], rowvar=False)
    return np.nan_to_num(c, nan=0.0, posinf=0.0, neginf=0.0)


def effective_trials(series: Iterable) -> TrialCensus:
    """Independent searches actually performed, from the trial return matrix.

    Columns must already be DATE-ALIGNED — row t the same calendar day in every
    column. An unaligned matrix reports a correlation structure that never
    existed, and truncating to the shortest column silently realigns everything
    to a different set of days.
    """
    cols = [np.asarray(s, dtype=float) for s in series]
    n_raw = len(cols)
    if n_raw < 2:
        return TrialCensus(n_raw, float(max(n_raw, 1)), "n<2",
                           why="fewer than two trials; nothing to deduplicate")
    lengths = {len(c) for c in cols}
    if len(lengths) > 1:
        return TrialCensus(
            n_raw, float(n_raw), "unaligned",
            why=(f"columns have {len(lengths)} different lengths {sorted(lengths)[:4]}; "
                 f"they are not date-aligned. N_eff left at N_raw rather than "
                 f"computing a correlation structure that never existed."))

    c = _corr_matrix(cols)
    if c is None:
        # FAILS CLOSED. No correlation matrix means no evidence of duplication,
        # and absence of evidence must not become a discount on the threshold.
        return TrialCensus(n_raw, float(n_raw), "unmeasurable",
                           why=("could not build a correlation matrix (series too "
                                "short, or all constant); N_eff left at N_raw — "
                                "no deduplication is assumed"))

    ev = np.clip(np.linalg.eigvalsh(c), 0.0, None)
    denom = float((ev ** 2).sum())
    if denom <= 0:
        return TrialCensus(n_raw, float(n_raw), "degenerate",
                           why="degenerate correlation spectrum; N_eff at N_raw")
    n_eff = float(ev.sum() ** 2 / denom)
    n_eff = max(2.0, min(n_eff, float(n_raw)))   # never below 2, never above what ran

    k = c.shape[0]
    pairs = [(i, j, float(c[i, j])) for i in range(k) for j in range(i + 1, k)
             if abs(c[i, j]) >= CLONE_RHO]
    return TrialCensus(
        n_raw, n_eff, "participation_ratio", pairs,
        why=(f"participation ratio of the trial correlation spectrum: {n_raw} "
             f"cells behave as {n_eff:.1f} independent searches "
             f"({n_raw / n_eff:.1f}x inflation); {len(pairs)} pair(s) at "
             f"|rho| >= {CLONE_RHO}"))


def census_from_registry(registry, series: Iterable = ()) -> TrialCensus:
    """THE WELD. Raw count from the run registry, deduplication from the returns.

    Taking N from `linkage.trial_census()` rather than from a variable somebody
    maintains is the point: the registry counts abandoned runs and reruns at new
    parameters, which is where the honest denominator actually lives.
    """
    n_raw = int(registry.trial_census()["trials_for_fdr"])
    cols = list(series)
    if len(cols) < 2:
        return TrialCensus(n_raw, float(max(n_raw, 1)), "registry_only",
                           why=(f"{n_raw} registered runs; no return series supplied "
                                f"so no deduplication was attempted and N_eff is "
                                f"left at the full registered count"))
    ded = effective_trials(cols)
    # The registry is authoritative on HOW MANY, the spectrum on HOW SIMILAR.
    # Scaling by the measured inflation applies the second to the first without
    # letting a partial set of return series shrink the count on its own.
    n_eff = max(2.0, min(float(n_raw), n_raw / ded.inflation))
    return TrialCensus(
        n_raw, n_eff, "registry_x_participation_ratio", ded.clone_pairs,
        why=(f"{n_raw} registered runs (linkage census), deduplicated by the "
             f"{ded.inflation:.1f}x inflation measured over {len(cols)} return "
             f"series -> {n_eff:.1f} effective searches"))


# ------------------------------------------------------------------ the threshold

def expected_max_z(n: float) -> float:
    """E[max of n iid standard normals]. What the DSR threshold is built on.

    Non-integer n accepted because N_eff is continuous. Standard asymptotic with
    the Euler–Mascheroni correction, accurate to about 1% by n=10.
    """
    n = max(float(n), 2.0)
    a = math.sqrt(2.0 * math.log(n))
    return a - (math.log(math.log(n)) + math.log(4.0 * math.pi)) / (2.0 * a)


def sharpe_std_error(sr: float, n_obs: int, skew: float = 0.0,
                     kurt: float = 3.0) -> float:
    """Standard error of a Sharpe ratio under NON-NORMAL returns.

    sigma(SR) = sqrt((1 - g3*SR + (g4-1)/4 * SR^2) / (T-1))

    The 1/sqrt(T) shortcut assumes normality. A gold trend book wins small and
    often and loses large and rarely — negative skew, fat tails — and for that
    shape the naive error is TOO SMALL, so every t-statistic comes out too big.
    This is where a fat-tailed book stops flattering itself.
    """
    if n_obs < 2:
        return float("inf")
    var = (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr) / (n_obs - 1)
    return math.sqrt(max(var, 1e-12))


def moments(returns: Sequence[float]) -> tuple:
    """(sharpe, skew, kurtosis, n) from a return series. Per-observation Sharpe.

    Deliberately NOT annualised. Annualisation multiplies by sqrt(periods per
    year), which is a choice about periodicity that inflates or deflates every
    downstream threshold and hides inside a constant.
    """
    r = np.asarray([x for x in returns if math.isfinite(x)], dtype=float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0, 0.0, 3.0, len(r)
    sd = float(r.std(ddof=1))
    m = float(r.mean())
    z = (r - m) / sd
    return m / sd, float((z ** 3).mean()), float((z ** 4).mean()), len(r)


@dataclass
class Deflated:
    sr: float
    sr0_raw: float
    sr0_effective: float
    n_raw: int
    n_effective: float
    n_obs: int
    skew: float
    kurt: float
    psr: float                  # probability the true Sharpe exceeds the threshold
    passes: bool
    why: str

    def render(self) -> str:
        return "\n".join([
            f"DEFLATED SHARPE  ({DEFLATION_VERSION})",
            f"  observed SR         {self.sr:+.4f}  over {self.n_obs} observations",
            f"  skew / kurtosis     {self.skew:+.2f} / {self.kurt:.2f}"
            + ("   (fat-tailed: the naive standard error would be too small)"
               if self.kurt > 3.5 else ""),
            f"  trials              {self.n_raw} raw, {self.n_effective:.1f} effective",
            f"  threshold SR0       {self.sr0_raw:.4f} raw, "
            f"{self.sr0_effective:.4f} effective",
            f"  P(true SR > SR0)    {self.psr:.3f}",
            f"  {'PASSES' if self.passes else 'FAILS'}: {self.why}",
        ])


def deflated_sharpe(returns: Sequence[float], census: TrialCensus,
                    threshold_p: float = 0.95) -> Deflated:
    """Does this beat the best of N coin flips, on THIS book's return shape?

    Both trial counts are carried through and both thresholds reported. Showing
    only the deduplicated one would let this module quietly relax every gate it
    touches; the pair makes the size of the correction visible so a reader can
    judge whether the deduplication is doing real work.
    """
    sr, skew, kurt, n_obs = moments(returns)
    se = sharpe_std_error(sr, n_obs, skew, kurt)
    sr0_raw = se * expected_max_z(census.n_raw)
    sr0_eff = se * expected_max_z(census.n_effective)
    if n_obs < 2 or not math.isfinite(se) or se <= 0:
        return Deflated(sr, sr0_raw, sr0_eff, census.n_raw, census.n_effective,
                        n_obs, skew, kurt, 0.0, False,
                        "too few observations to estimate a standard error")
    z = (sr - sr0_eff) / se
    psr = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    passes = psr >= threshold_p
    return Deflated(
        sr, sr0_raw, sr0_eff, census.n_raw, census.n_effective, n_obs, skew,
        kurt, psr, passes,
        ("the observed Sharpe exceeds what the best of "
         f"{census.n_effective:.1f} effective trials would produce by luck, at "
         f"{threshold_p:.0%} confidence"
         if passes else
         f"cannot be distinguished from the best of {census.n_effective:.1f} "
         f"effective trials (P={psr:.3f} < {threshold_p:.0%}). More frequency "
         f"widens the search and RAISES this bar — the answer is a better edge "
         f"or more independent evidence, not more variants."))


def report(returns: Sequence[float], census: TrialCensus) -> str:
    d = deflated_sharpe(returns, census)
    lines = [d.render(), "", f"  census: {census.why}"]
    if census.clone_pairs:
        lines.append(f"  {len(census.clone_pairs)} near-duplicate trial pair(s) "
                     f"at |rho| >= {CLONE_RHO}")
    lines.append("  Both counts are shown deliberately. Lowering N makes every "
                 "threshold easier, so the correction has to be visible.")
    return "\n".join(lines)
