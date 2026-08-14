"""Frozen A/B/C/D evaluation — does each layer earn its place economically?

The question is never "is the AI impressive". It is: on the same market states,
the same cost model and the same management, does adding a layer increase net
risk-adjusted value? A layer that does not is removed.

    A  deterministic baseline          (old Aurum's own rules)
    B  A + contextual AI analyst       (does the read add anything?)
    C  B + empirical edge router       (or was it just cohort filtering?)
    D  C + adaptive management         (does management add on top of entry?)

Arm C exists specifically to answer the uncomfortable question: if C beats A
but B does not, the value was never in the language model — it was in the
evidence-based cohort filter, and the model is an expensive narrator.

Because every arm is replayed over IDENTICAL states, comparisons are PAIRED.
Paired bootstrap on per-state differences is materially more powerful than
comparing two independent return series, and it isolates the layer instead of
the week.

USAGE ORDER — this is not optional:

    1. write the Preregistration and call .freeze()          <- before results
    2. run the arms over the locked holdout                  <- once
    3. call compare() and apply the preregistered rule
    4. only then tune anything

Tuning a policy and then writing the spec produces a number with no meaning.
freeze() stamps a hash so a later edit to the spec is detectable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Preregistration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Preregistration:
    """Written BEFORE any result is seen. Hashed so edits are detectable."""
    hypothesis: str
    arms: tuple[str, ...]
    primary_metric: str                 # the one that decides. Exactly one.
    secondary_metrics: tuple[str, ...]
    holdout_start: str                  # ISO date — untouched until step 2
    holdout_end: str
    min_ess: float                      # charter bar
    fdr_q: float                        # BH-FDR level
    trials_declared: int                # honest count...
    trials_inflation: float             # ...multiplied, per the red team's W3.1
    promote_rule: str
    demote_rule: str
    frozen_at: Optional[str] = None

    @property
    def effective_trials(self) -> int:
        """A solo researcher cannot count informal trials. Inflate deliberately."""
        return int(self.trials_declared * self.trials_inflation)

    def content_hash(self) -> str:
        d = {k: v for k, v in asdict(self).items() if k != "frozen_at"}
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()

    def freeze(self, path: Path) -> str:
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"{path} already frozen — do not overwrite a spec")
        stamped = Preregistration(**{**asdict(self),
                                     "frozen_at": datetime.now(timezone.utc).isoformat()})
        payload = {"spec": asdict(stamped), "hash": stamped.content_hash()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
        return payload["hash"]

    @staticmethod
    def verify(path: Path) -> tuple[bool, str]:
        raw = json.loads(Path(path).read_text())
        spec = Preregistration(**raw["spec"])
        ok = spec.content_hash() == raw["hash"]
        return ok, ("intact" if ok else "SPEC EDITED AFTER FREEZING — results void")


# --------------------------------------------------------------------------
# Per-state outcomes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StateOutcome:
    """One arm's result on one market state. Arms share state_id."""
    state_id: str
    ts: datetime
    acted: bool                # did this arm take the trade?
    net_r: float               # 0.0 when it did not act
    best_achievable_r: float   # MFE — the ceiling, from the ledger
    cohort: str = "all"


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def ess(xs: Sequence[float], max_lag: int = 20) -> float:
    """Effective sample size, autocorrelation-adjusted.

    ESS = n / (1 + 2*sum(rho_k)). Raw row count flatters a desk whose trades
    overlap in time; this is the number the charter's ESS >= 30 bar refers to.
    """
    n = len(xs)
    if n < 3:
        return float(n)
    mean = statistics.fmean(xs)
    var = statistics.pvariance(xs)
    if var <= 0:
        return float(n)
    total = 0.0
    for k in range(1, min(max_lag, n - 1) + 1):
        cov = sum((xs[i] - mean) * (xs[i + k] - mean) for i in range(n - k)) / n
        rho = cov / var
        if rho <= 0:            # standard initial-positive-sequence truncation
            break
        total += rho
    return max(1.0, n / (1.0 + 2.0 * total))


def max_drawdown_r(xs: Sequence[float]) -> float:
    peak = cum = 0.0
    worst = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def top_k_share(xs: Sequence[float], k: int = 3) -> float:
    """The desk's own concentration check: is the edge three lucky trades?"""
    total = sum(xs)
    if abs(total) < 1e-12:
        return 0.0
    return sum(sorted(xs, reverse=True)[:k]) / total


def paired_bootstrap(deltas: Sequence[float], iters: int = 10000,
                     seed: int = 0) -> tuple[float, float, float]:
    """(mean, lo95, hi95) on per-state differences. Paired = same states."""
    if not deltas:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(iters):
        means.append(sum(deltas[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (statistics.fmean(deltas),
            means[int(0.025 * iters)],
            means[int(0.975 * iters)])


def paired_p_value(deltas: Sequence[float], iters: int = 10000,
                   seed: int = 0) -> float:
    """Two-sided permutation test on sign flips. No normality assumption."""
    if not deltas:
        return 1.0
    obs = abs(statistics.fmean(deltas))
    rng = random.Random(seed)
    hits = 0
    for _ in range(iters):
        flipped = statistics.fmean([d if rng.random() < 0.5 else -d for d in deltas])
        if abs(flipped) >= obs:
            hits += 1
    return (hits + 1) / (iters + 1)


def bh_fdr(pvals: Sequence[float], q: float, n_trials: Optional[int] = None
           ) -> list[bool]:
    """Benjamini-Hochberg. n_trials lets you penalise for UNREPORTED tests."""
    m = n_trials or len(pvals)
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    passed = [False] * len(pvals)
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= (rank / m) * q:
            for j in order[:rank]:
                passed[j] = True
    return passed


# --------------------------------------------------------------------------
# Arm metrics
# --------------------------------------------------------------------------

@dataclass
class ArmMetrics:
    arm: str
    n_states: int
    n_acted: int
    selectivity: float          # acted / states — activity, not virtue
    net_r: float
    mean_r_per_trade: float
    net_r_per_day: float        # frequency-adjusted: the objective's real target
    win_rate: float
    ess: float
    max_dd_r: float
    tail_5pct_r: float
    top3_share: float
    ex_top3_net_r: float
    capture_rate: float         # net_r / sum(best_achievable) on acted states
    forgone_r: float            # MFE left on the table by not acting

    def row(self) -> str:
        return (f"  {self.arm:<6}{self.n_acted:>6}{self.selectivity:>8.1%}"
                f"{self.net_r:>10.1f}{self.mean_r_per_trade:>9.3f}"
                f"{self.net_r_per_day:>10.2f}{self.win_rate:>8.0%}"
                f"{self.ess:>8.0f}{self.max_dd_r:>9.1f}{self.top3_share:>9.0%}"
                f"{self.capture_rate:>9.0%}")


def metrics(arm: str, outcomes: Sequence[StateOutcome]) -> ArmMetrics:
    acted = [o for o in outcomes if o.acted]
    rs = [o.net_r for o in acted]
    days = max(1.0, (max(o.ts for o in outcomes) - min(o.ts for o in outcomes)).days or 1)
    achievable = sum(max(0.0, o.best_achievable_r) for o in acted)
    forgone = sum(max(0.0, o.best_achievable_r) for o in outcomes if not o.acted)
    srt = sorted(rs)
    tail = statistics.fmean(srt[:max(1, len(srt) // 20)]) if srt else 0.0
    return ArmMetrics(
        arm=arm,
        n_states=len(outcomes),
        n_acted=len(acted),
        selectivity=len(acted) / len(outcomes) if outcomes else 0.0,
        net_r=sum(rs),
        mean_r_per_trade=statistics.fmean(rs) if rs else 0.0,
        net_r_per_day=sum(rs) / days,
        win_rate=(sum(r > 0 for r in rs) / len(rs)) if rs else 0.0,
        ess=ess(rs),
        max_dd_r=max_drawdown_r(rs),
        tail_5pct_r=tail,
        top3_share=top_k_share(rs),
        ex_top3_net_r=sum(sorted(rs, reverse=True)[3:]) if len(rs) > 3 else 0.0,
        capture_rate=(sum(rs) / achievable) if achievable > 0 else 0.0,
        forgone_r=forgone,
    )


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------

@dataclass
class Comparison:
    label: str                 # "B vs A"
    mean_delta_r: float
    ci_lo: float
    ci_hi: float
    p_value: float
    n_paired: int
    ess_paired: float
    survives_fdr: bool = False

    def row(self) -> str:
        sig = "YES" if self.survives_fdr else "no"
        return (f"  {self.label:<10}{self.mean_delta_r:>+10.4f}"
                f"  [{self.ci_lo:+.4f}, {self.ci_hi:+.4f}]"
                f"{self.p_value:>9.4f}{self.ess_paired:>9.0f}{sig:>8}")


def compare(arms: dict[str, Sequence[StateOutcome]], prereg: Preregistration
            ) -> tuple[list[ArmMetrics], list[Comparison], list[str]]:
    """Paired ladder comparison with multiplicity control. Returns verdicts."""
    names = list(prereg.arms)
    mets = [metrics(a, arms[a]) for a in names]

    # Pair by state_id across consecutive rungs of the ladder.
    comps: list[Comparison] = []
    for lo, hi in zip(names, names[1:]):
        a_map = {o.state_id: o.net_r for o in arms[lo]}
        b_map = {o.state_id: o.net_r for o in arms[hi]}
        shared = sorted(set(a_map) & set(b_map))
        deltas = [b_map[s] - a_map[s] for s in shared]
        nz = [d for d in deltas if abs(d) > 1e-12]
        mean, lo95, hi95 = paired_bootstrap(deltas)
        comps.append(Comparison(
            label=f"{hi} vs {lo}", mean_delta_r=mean, ci_lo=lo95, ci_hi=hi95,
            p_value=paired_p_value(deltas), n_paired=len(shared),
            ess_paired=ess(deltas),
        ))

    # Also compare every arm against the baseline, not just its neighbour.
    base = names[0]
    for other in names[2:]:
        a_map = {o.state_id: o.net_r for o in arms[base]}
        b_map = {o.state_id: o.net_r for o in arms[other]}
        shared = sorted(set(a_map) & set(b_map))
        deltas = [b_map[s] - a_map[s] for s in shared]
        mean, lo95, hi95 = paired_bootstrap(deltas)
        comps.append(Comparison(f"{other} vs {base}", mean, lo95, hi95,
                                paired_p_value(deltas), len(shared), ess(deltas)))

    flags = bh_fdr([c.p_value for c in comps], prereg.fdr_q,
                   n_trials=prereg.effective_trials)
    for c, f in zip(comps, flags):
        c.survives_fdr = f

    verdicts = _verdicts(mets, comps, prereg)
    return mets, comps, verdicts


def _verdicts(mets: Sequence[ArmMetrics], comps: Sequence[Comparison],
              prereg: Preregistration) -> list[str]:
    """Apply the preregistered rule. No judgement calls at this stage."""
    out: list[str] = []
    by_label = {c.label: c for c in comps}
    by_arm = {m.arm: m for m in mets}
    names = list(prereg.arms)

    for lo, hi in zip(names, names[1:]):
        c = by_label.get(f"{hi} vs {lo}")
        m = by_arm[hi]
        if c is None:
            continue
        if m.ess < prereg.min_ess:
            out.append(f"{hi}: UNDETERMINED — ESS {m.ess:.0f} below {prereg.min_ess:.0f}")
        elif not c.survives_fdr:
            out.append(f"{hi}: DOES NOT EARN ITS PLACE — "
                       f"{c.mean_delta_r:+.4f}R/state over {lo}, p={c.p_value:.3f} "
                       f"fails FDR at {prereg.effective_trials} effective trials. "
                       f"{prereg.demote_rule}")
        elif c.ci_lo <= 0:
            out.append(f"{hi}: WEAK — CI includes zero [{c.ci_lo:+.4f}, {c.ci_hi:+.4f}]")
        else:
            out.append(f"{hi}: EARNS ITS PLACE — {c.mean_delta_r:+.4f}R/state over {lo}, "
                       f"CI [{c.ci_lo:+.4f}, {c.ci_hi:+.4f}], ESS {m.ess:.0f}")
    return out


def report(mets: Sequence[ArmMetrics], comps: Sequence[Comparison],
           verdicts: Sequence[str]) -> str:
    lines = ["ARMS",
             f"  {'arm':<6}{'acted':>6}{'select':>8}{'net R':>10}{'R/trade':>9}"
             f"{'R/day':>10}{'win':>8}{'ESS':>8}{'maxDD':>9}{'top3':>9}{'capture':>9}"]
    lines += [m.row() for m in mets]
    lines += ["", "PAIRED DELTAS (same states, same costs, same management)",
              f"  {'pair':<10}{'mean dR':>10}{'  95% CI':<22}{'p':>9}{'ESS':>9}{'FDR':>8}"]
    lines += [c.row() for c in comps]
    lines += ["", "VERDICTS"] + [f"  {v}" for v in verdicts]
    return "\n".join(lines)
