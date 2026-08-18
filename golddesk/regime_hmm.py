"""A learned regime detector, as a CHALLENGER — and the contest that judges it.

The desk's incumbent regime label is a set of hand-chosen thresholds in
`features.py`: direction from swing structure, health from an ATR ratio, and a
volatility bucket. It has never been benchmarked against an alternative, so
nobody knows whether it is a good partition of gold's history or merely the
first one anybody wrote down. That is the gap this closes.

THE LEAK IN ESSENTIALLY EVERY PUBLIC HMM REGIME DETECTOR

The standard recipe is `model.fit(X)` then `model.predict(X)`, and `predict` is
Viterbi — the most likely state SEQUENCE given the WHOLE series. It assigns
today's regime using tomorrow's observations. The resulting labels are gorgeous:
crisp blocks that switch exactly at the turn, because the algorithm was allowed
to see the turn. Backtest on them and the regime model looks like clairvoyance,
which it is.

So this module separates the two uses of the forward-backward machinery, and the
separation is the whole reason it is worth writing rather than importing:

    FITTING may smooth.   Baum-Welch on the training window uses all of it. That
                          window is over; using its future is not lookahead.
    INFERENCE MAY NOT.    `filter_states()` is the forward pass alone —
                          P(state_t | observations up to and including t). It is
                          what the desk could actually have known at t, and it
                          is the only inference path exposed.

`smooth_states()` exists, is named for what it does, and is documented as
research-only. Deleting it would not remove the temptation, only the ability to
measure how much better the smoothed labels look — which is itself the useful
demonstration.

WHAT THE CONTEST MEASURES, AND WHY IT IS TWO NUMBERS AND NOT ONE

A regime labelling earns its place by making the forward distribution CONDITIONAL
— knowing the regime should tell you something you did not otherwise know. There
are two different somethings and they have completely different value:

    VOLATILITY SEPARATION   do the regimes have different forward variance?
                            Easy. Volatility clusters, any method finds this,
                            and it is genuinely useful — for SIZING.
    DIRECTION SEPARATION    do the regimes have different forward MEAN return?
                            Hard, usually absent, and the only thing that
                            justifies a regime label changing a trade decision.

Reporting one blended score is how a desk comes to believe its regime model
predicts direction when all it has ever done is detect that gold got choppy.
They are reported separately and never combined.

THE NULL IS IN THE CONTEST

A random labelling with the same number of states and the same marginal
frequencies runs alongside. Any partition of a fat-tailed series will separate
realised variance somewhat, purely by sorting noise, so "beats nothing" is the
bar and the shuffled control is what defines it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

REGIME_HMM_VERSION = "hmm-2026-08-18-a"

#: Three states is the literature's default for equities and metals — quiet
#: trend, choppy, stress. Not tuned: it is the challenger's declared prior, and
#: `n_states` is an argument so the contest can be run at other values without
#: anyone quietly picking the best one afterwards and reporting it as the design.
DEFAULT_STATES = 3

#: EM stops when the log-likelihood improves by less than this per observation.
TOL = 1e-6
MAX_ITER = 200

#: Below this many training rows the fit is noise with a decimal point.
MIN_TRAIN = 200


def _logsumexp(a: np.ndarray, axis=None):
    """Log-sum-exp with the max shifted out.

    The shift is not a nicety. An unscaled forward recursion underflows to zero
    within a few hundred bars, and the failure is silent: the posteriors come
    back as NaN or as a uniform distribution that looks like honest uncertainty.
    """
    if axis is None:
        m = float(np.max(a))
        return m + float(np.log(np.sum(np.exp(a - m))))
    m = np.max(a, axis=axis, keepdims=True)
    return np.squeeze(m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True)),
                      axis=axis)


@dataclass
class GaussianHMM:
    """Diagonal-covariance Gaussian emissions. Deliberately small.

    Diagonal rather than full covariance: with three states and a few hundred
    training rows, a full covariance per state is more parameters than the data
    can identify, and the failure mode is a state collapsing onto a handful of
    observations with a near-singular covariance and an enormous likelihood.
    """
    n_states: int = DEFAULT_STATES
    start: np.ndarray = field(default=None, repr=False)
    trans: np.ndarray = field(default=None, repr=False)
    means: np.ndarray = field(default=None, repr=False)
    var: np.ndarray = field(default=None, repr=False)
    n_train: int = 0
    loglik: float = float("nan")
    iters: int = 0

    # -- emissions -----------------------------------------------------
    def _log_emission(self, x: np.ndarray) -> np.ndarray:
        """(T, K) log N(x_t | mu_k, diag(var_k))."""
        d = x.shape[1]
        out = np.empty((len(x), self.n_states))
        for k in range(self.n_states):
            v = self.var[k]
            diff = x - self.means[k]
            out[:, k] = -0.5 * (d * math.log(2 * math.pi)
                                + np.log(v).sum()
                                + (diff * diff / v).sum(axis=1))
        return out

    # -- the causal pass -----------------------------------------------
    def filter_states(self, x: np.ndarray) -> np.ndarray:
        """P(state_t | observations 1..t). THE ONLY INFERENCE THE DESK MAY USE.

        Forward pass alone. Every row uses that row and the ones before it and
        nothing else, which is the definition of what was knowable at t.
        """
        x = np.atleast_2d(np.asarray(x, dtype=float))
        le = self._log_emission(x)
        T, K = le.shape
        log_a = np.empty((T, K))
        log_a[0] = np.log(self.start + 1e-300) + le[0]
        log_trans = np.log(self.trans + 1e-300)
        for t in range(1, T):
            log_a[t] = le[t] + _logsumexp(log_a[t - 1][:, None] + log_trans, axis=0)
        return np.exp(log_a - _logsumexp(log_a, axis=1)[:, None])

    def smooth_states(self, x: np.ndarray) -> np.ndarray:
        """P(state_t | the WHOLE series). RESEARCH ONLY — NEVER A LIVE LABEL.

        This uses observations after t. Labels from it switch exactly at turning
        points because the algorithm saw the turn, and any backtest built on
        them measures clairvoyance. It is exposed under a name that says so, so
        the size of the difference can be MEASURED rather than merely warned
        about — see `smoothing_advantage()`.
        """
        x = np.atleast_2d(np.asarray(x, dtype=float))
        le = self._log_emission(x)
        T, K = le.shape
        log_trans = np.log(self.trans + 1e-300)
        log_a = np.empty((T, K))
        log_a[0] = np.log(self.start + 1e-300) + le[0]
        for t in range(1, T):
            log_a[t] = le[t] + _logsumexp(log_a[t - 1][:, None] + log_trans, axis=0)
        log_b = np.zeros((T, K))
        for t in range(T - 2, -1, -1):
            log_b[t] = _logsumexp(log_trans + le[t + 1] + log_b[t + 1], axis=1)
        g = log_a + log_b
        return np.exp(g - _logsumexp(g, axis=1)[:, None])

    def labels(self, x: np.ndarray) -> np.ndarray:
        """Causal hard labels. argmax of the FILTERED posterior."""
        return self.filter_states(x).argmax(axis=1)


def fit_hmm(x: Sequence[Sequence[float]], n_states: int = DEFAULT_STATES,
            seed: int = 0, max_iter: int = MAX_ITER) -> Optional[GaussianHMM]:
    """Baum-Welch on the training window. None when the sample cannot support it.

    Smoothing HERE is legitimate: the training window is over, and using its own
    future to estimate parameters is not lookahead as long as those parameters
    are only ever applied to data after it.
    """
    x = np.atleast_2d(np.asarray(x, dtype=float))
    x = x[np.isfinite(x).all(axis=1)]
    T, d = x.shape
    if T < MIN_TRAIN or n_states < 2:
        return None

    rng = np.random.default_rng(seed)
    m = GaussianHMM(n_states=n_states)
    # Initialise means at quantiles of the first column so the states start
    # ordered by the variable the desk cares most about, rather than at random
    # points that can leave a state with no assigned mass and a zero variance.
    q = np.quantile(x[:, 0], np.linspace(0.15, 0.85, n_states))
    m.means = np.tile(x.mean(axis=0), (n_states, 1))
    m.means[:, 0] = q
    m.var = np.tile(np.maximum(x.var(axis=0), 1e-8), (n_states, 1))
    m.start = np.full(n_states, 1.0 / n_states)
    m.trans = np.full((n_states, n_states), 0.1 / max(1, n_states - 1))
    np.fill_diagonal(m.trans, 0.9)
    m.trans /= m.trans.sum(axis=1, keepdims=True)

    prev = -np.inf
    for it in range(max_iter):
        le = m._log_emission(x)
        log_trans = np.log(m.trans + 1e-300)
        log_a = np.empty((T, n_states))
        log_a[0] = np.log(m.start + 1e-300) + le[0]
        for t in range(1, T):
            log_a[t] = le[t] + _logsumexp(log_a[t - 1][:, None] + log_trans, axis=0)
        ll = _logsumexp(log_a[-1])
        log_b = np.zeros((T, n_states))
        for t in range(T - 2, -1, -1):
            log_b[t] = _logsumexp(log_trans + le[t + 1] + log_b[t + 1], axis=1)

        g = log_a + log_b
        gamma = np.exp(g - _logsumexp(g, axis=1)[:, None])
        xi = np.zeros((n_states, n_states))
        for t in range(T - 1):
            lx = (log_a[t][:, None] + log_trans + le[t + 1] + log_b[t + 1])
            xi += np.exp(lx - _logsumexp(lx))

        m.start = gamma[0] / gamma[0].sum()
        m.trans = xi / np.maximum(xi.sum(axis=1, keepdims=True), 1e-300)
        w = gamma.sum(axis=0)
        m.means = (gamma.T @ x) / np.maximum(w[:, None], 1e-300)
        for k in range(n_states):
            diff = x - m.means[k]
            # VARIANCE FLOOR. Without it a state collapses onto a couple of
            # observations, its variance goes to zero, its likelihood goes to
            # infinity and the fit is destroyed in a way that looks like
            # spectacular convergence.
            m.var[k] = np.maximum(
                (gamma[:, k][:, None] * diff * diff).sum(axis=0) / max(w[k], 1e-300),
                1e-8)

        m.iters, m.loglik = it + 1, float(ll)
        if abs(ll - prev) / T < TOL:
            break
        prev = ll
    m.n_train = T
    return m


def features(returns: Sequence[float], window: int = 20) -> np.ndarray:
    """The observation vector: return, and a CAUSAL local volatility.

    The volatility uses a trailing window ending at t, never centred. A centred
    window is the same lookahead as Viterbi wearing different clothes, and it is
    far easier to write by accident.
    """
    r = np.asarray(returns, dtype=float)
    vol = np.empty(len(r))
    for t in range(len(r)):
        lo = max(0, t - window + 1)
        seg = r[lo:t + 1]
        vol[t] = seg.std() if len(seg) > 1 else 0.0
    return np.column_stack([r, vol])


# --------------------------------------------------------------- the contest

@dataclass
class Separation:
    """How much a labelling conditions the forward distribution."""
    name: str
    n: int
    n_states: int
    direction_f: float          # between/within variance ratio on forward MEAN
    volatility_f: float         # same, on forward absolute move
    per_state_mean: dict = field(default_factory=dict)
    why: str = ""

    def render(self) -> str:
        return (f"  {self.name:<22} n={self.n:<6} states={self.n_states}  "
                f"direction F={self.direction_f:6.3f}   "
                f"volatility F={self.volatility_f:6.3f}")


def _f_ratio(values: np.ndarray, labels: np.ndarray) -> float:
    """Between-group over within-group variance. One-way ANOVA F, unadjusted.

    Used as a comparative statistic only — the same measure applied to every
    entrant including the null, so its absolute value never has to mean anything
    and no distributional assumption has to hold.
    """
    groups = [values[labels == k] for k in np.unique(labels)]
    groups = [g for g in groups if len(g) >= 2]
    if len(groups) < 2:
        return 0.0
    n = sum(len(g) for g in groups)
    grand = np.concatenate(groups).mean()
    between = sum(len(g) * (g.mean() - grand) ** 2 for g in groups) / (len(groups) - 1)
    within = sum(((g - g.mean()) ** 2).sum() for g in groups) / max(1, n - len(groups))
    return float(between / within) if within > 0 else 0.0


def separation(name: str, labels: Sequence[int],
               forward: Sequence[float]) -> Separation:
    """Score one labelling. Direction and volatility, never combined."""
    lab = np.asarray(labels)
    fwd = np.asarray(forward, dtype=float)
    n = min(len(lab), len(fwd))
    lab, fwd = lab[:n], fwd[:n]
    ok = np.isfinite(fwd)
    lab, fwd = lab[ok], fwd[ok]
    if len(lab) < 2:
        return Separation(name, 0, 0, 0.0, 0.0,
                          why="nothing to score")
    return Separation(
        name, len(lab), len(np.unique(lab)),
        direction_f=_f_ratio(fwd, lab),
        volatility_f=_f_ratio(np.abs(fwd), lab),
        per_state_mean={int(k): float(fwd[lab == k].mean())
                        for k in np.unique(lab)},
        why="direction is the forward MEAN; volatility is the forward ABSOLUTE "
            "move. A labelling can win one and lose the other, and treating "
            "them as one number is how a vol detector gets believed about "
            "direction.")


#: Permutations behind the null, and the quantile of that distribution an
#: entrant must clear. 200 draws resolve a 95th percentile well enough for a
#: verdict and cost milliseconds.
N_SHUFFLES = 200
NULL_Q = 0.95


def shuffled_null(labels: Sequence[int], forward: Sequence[float],
                  seed: int = 0, n_shuffles: int = N_SHUFFLES,
                  q: float = NULL_Q) -> Separation:
    """THE FLOOR — a permutation DISTRIBUTION, not one draw.

    Any partition of a fat-tailed series separates realised variance somewhat by
    sorting noise, so an entrant has to be compared against how well chance does.
    But ONE shuffle is itself a random draw from the same distribution as a
    worthless entrant, so "beat the null" would be decided by a coin flip and
    roughly half of all pure-noise labellings would pass. This module's own test
    caught exactly that.

    So the null is the `q`-th percentile over `n_shuffles` permutations: an
    entrant must beat 95% of random labellings with its own marginal state
    frequencies. That is a permutation test, and the thing a single shuffle only
    resembled.
    """
    rng = np.random.default_rng(seed)
    lab = np.asarray(labels).copy()
    ds, vs = [], []
    for _ in range(n_shuffles):
        rng.shuffle(lab)
        s = separation("_", lab, forward)
        ds.append(s.direction_f)
        vs.append(s.volatility_f)
    base = separation("NULL", lab, forward)
    return Separation(
        f"NULL(shuffled, p{int(q * 100)} of {n_shuffles})", base.n, base.n_states,
        direction_f=float(np.quantile(ds, q)),
        volatility_f=float(np.quantile(vs, q)),
        why=(f"{q:.0%} of {n_shuffles} random labellings with the same marginal "
             f"frequencies score below this. One shuffle would be a coin flip."))


def smoothing_advantage(model: GaussianHMM, x: np.ndarray,
                        forward: Sequence[float]) -> dict:
    """How much better the ILLEGAL labels look. The demonstration, not a feature.

    Reported so the gap between filtered and smoothed labelling is a number the
    desk has seen, rather than a caveat it has read.
    """
    f = separation("filtered (causal)", model.labels(x), forward)
    s = separation("smoothed (LOOKAHEAD)", model.smooth_states(x).argmax(axis=1),
                   forward)
    return {
        "filtered": f, "smoothed": s,
        "direction_inflation": s.direction_f - f.direction_f,
        "volatility_inflation": s.volatility_f - f.volatility_f,
        "note": ("Smoothed labels use observations after the bar they label. "
                 "The difference above is the size of the lie a Viterbi-labelled "
                 "backtest tells. Filtered is the only labelling the desk may use."),
    }


def contest(returns: Sequence[float], incumbent_labels: Sequence[int],
            forward: Sequence[float], train: int = 500,
            n_states: int = DEFAULT_STATES, seed: int = 0) -> dict:
    """Incumbent rule labels versus a learned HMM, paired on identical bars.

    Paired is not optional. Scoring the incumbent on one span and the challenger
    on another measures which span was kinder — gold does not repeat, and the
    difference would be dominated by whatever the market did.
    """
    r = np.asarray(returns, dtype=float)
    inc = np.asarray(incumbent_labels)
    fwd = np.asarray(forward, dtype=float)
    n = min(len(r), len(inc), len(fwd))
    r, inc, fwd = r[:n], inc[:n], fwd[:n]
    if n <= train + 50:
        return {"verdict": f"too short: {n} rows with train={train}",
                "entrants": []}

    x = features(r)
    model = fit_hmm(x[:train], n_states=n_states, seed=seed)
    if model is None:
        return {"verdict": f"HMM not fitted: {train} training rows, "
                           f"{MIN_TRAIN} required", "entrants": []}

    # OUT OF SAMPLE ONLY, and the HMM is filtered over the full prefix so its
    # state belief at each test bar reflects the path that actually led there —
    # restarting the filter at the split boundary would hand the challenger a
    # cold, uninformative prior it never has in production.
    xs, fs = slice(train, n), slice(train, n)
    hmm_lab = model.labels(x)[xs]
    inc_lab, fwd_te = inc[xs], fwd[fs]

    ent = [separation("incumbent (rules)", inc_lab, fwd_te),
           separation(f"HMM ({n_states} states)", hmm_lab, fwd_te)]
    null = shuffled_null(hmm_lab, fwd_te, seed=seed)

    beat_dir = [e.name for e in ent if e.direction_f > null.direction_f]
    beat_vol = [e.name for e in ent if e.volatility_f > null.volatility_f]
    best_dir = max(ent, key=lambda e: e.direction_f)
    best_vol = max(ent, key=lambda e: e.volatility_f)

    return {
        "version": REGIME_HMM_VERSION,
        "n_test": len(inc_lab),
        "entrants": ent,
        "null": null,
        "smoothing": smoothing_advantage(model, x[xs], fwd_te),
        "beat_null_on_direction": beat_dir,
        "beat_null_on_volatility": beat_vol,
        "best_direction": best_dir.name if beat_dir else None,
        "best_volatility": best_vol.name if beat_vol else None,
        "verdict": (
            "NEITHER labelling separates forward direction better than shuffled "
            "labels. Regime here is a volatility statement, and must not be "
            "allowed to argue about direction."
            if not beat_dir else
            f"{best_dir.name} separates forward direction best. One test, "
            f"uncorrected — seal it before it changes a decision."),
    }


def render(result: dict) -> str:
    if not result.get("entrants"):
        return result.get("verdict", "no contest")
    lines = [f"REGIME CONTEST  ({result['version']})  n_test={result['n_test']}", ""]
    lines += [e.render() for e in result["entrants"]]
    lines.append(result["null"].render())
    lines.append("")
    sm = result["smoothing"]
    lines.append(f"  lookahead check: smoothing inflates direction F by "
                 f"{sm['direction_inflation']:+.3f}, volatility F by "
                 f"{sm['volatility_inflation']:+.3f}")
    lines.append(f"  {sm['note']}")
    lines.append("")
    lines.append(f"  {result['verdict']}")
    return "\n".join(lines)
