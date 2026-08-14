"""Which brain is better, on states both of them saw. Item #7.

THE COMPARISON EVERYONE GETS WRONG

Run model A in January and model B in February, compare total R, declare a
winner. That measures which month was kinder. Gold does not repeat, the two
models never faced the same decision, and the difference is dominated by
whatever the market did — not by the models.

The only comparison that means anything is PAIRED: both models decide the SAME
state, and the difference is taken per state. That is what `state_id` and
ReplayAnalyst exist for, and this module is the analysis that consumes them.

WHAT MAKES A PAIRED COMPARISON HONEST

  1. IDENTICAL STATES. Every state both arms decided, and only those. An arm
     that skipped a state must not have that state counted for it — dropping
     the hard ones and keeping the easy ones is how an arm wins on paper.
  2. THE PAIRING IS VERIFIED, not assumed. check_pairing() fails loudly rather
     than silently comparing different populations.
  3. AGREEMENT IS REPORTED. Two arms that agree on 95% of states are not two
     arms; the interesting number is performance on the states where they
     DIFFERED, and the overall difference is diluted by everything else.
  4. THE TEST IS ON PAIRED DIFFERENCES, bootstrapped. A t-test on R assumes
     things R does not satisfy; a bootstrap on the paired difference assumes
     almost nothing and is the same tool the rest of the desk uses.
  5. TRIALS ARE COUNTED. Every arm comparison is a trial, and the multiplicity
     correction has to see all of them or the first lucky arm wins.

WHAT THIS CANNOT DO HERE

Run the arms. That needs an API key and inference spend. This is the analysis
half, testable end to end on synthetic paired ledgers, so that when the arms do
run the answer is a function call rather than a research project.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Sequence

COMPETITION_VERSION = "compete-2026-08-14-a"

# Fewer paired states than this and the comparison is not worth running.
MIN_PAIRED = 30


def state_id(symbol: str, timeframe: str, t0) -> str:
    """The canonical, ARM-INDEPENDENT identifier for a decision moment.

    Arm-independent is the whole point: if the id embedded the arm, the join
    would always be empty and the pairing would silently degrade to an unpaired
    comparison that still printed a p-value.
    """
    ts = t0 if isinstance(t0, str) else t0.isoformat()
    return f"{symbol}|{timeframe}|{ts}"


@dataclass
class Arm:
    """One competitor's decisions, keyed by state."""
    name: str
    by_state: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.by_state)


def collect(rows: Sequence[dict], *, arm_key: str = "vision",
            timeframe: str = "M15") -> dict:
    """Split a ledger into arms keyed by canonical state id.

    `arm_key` selects what counts as an arm — "vision" for the chart factorial,
    "model" or "provider" for a model competition, "management_policy" for the
    lifecycle arms.
    """
    arms: dict = {}
    for r in rows:
        dec = r.get("decision") or {}
        name = dec.get(arm_key) or r.get(arm_key)
        if not name:
            continue
        t0 = r.get("t0") or r.get("entry_t0") or r.get("ts")
        if not t0:
            continue
        sid = state_id(r.get("symbol", "XAUUSD"), timeframe, t0)
        arms.setdefault(name, Arm(name))
        # A state decided twice by one arm is a data error, not two observations.
        arms[name].by_state.setdefault(sid, r)
    return arms


@dataclass
class Pairing:
    a: str
    b: str
    shared: list
    only_a: int
    only_b: int

    @property
    def n(self) -> int:
        return len(self.shared)

    def render(self) -> str:
        return (f"  {self.a} vs {self.b}: {self.n} shared states "
                f"({self.only_a} only-{self.a}, {self.only_b} only-{self.b})")


def check_pairing(arms: dict, a: str, b: str) -> Pairing:
    """The states BOTH arms decided. Fails loudly rather than quietly.

    An arm that declined to decide a state does not get that state counted. The
    alternative — treating an absent decision as a zero — rewards an arm for
    skipping exactly the states it found hard.
    """
    if a not in arms or b not in arms:
        raise ValueError(f"unknown arm(s): {a!r}/{b!r}; have {sorted(arms)}")
    sa, sb = set(arms[a].by_state), set(arms[b].by_state)
    shared = sorted(sa & sb)
    return Pairing(a, b, shared, len(sa - sb), len(sb - sa))


def _outcome_r(row: dict):
    dec = row.get("decision") or {}
    if row.get("realised_r") is not None:
        return float(row["realised_r"])
    if dec.get("realised_r") is not None:
        return float(dec["realised_r"])
    out = row.get("outcome") or {}
    if isinstance(out, dict) and out.get("realised_r") is not None:
        return float(out["realised_r"])
    return None


def _decision_of(row: dict) -> str:
    dec = row.get("decision") or {}
    return str(dec.get("direction") or dec.get("declined") or row.get("kind") or "?")


def paired_bootstrap(diffs: Sequence[float], iters: int = 10000,
                     seed: int = 20260814) -> tuple:
    """(mean, lo, hi) of the paired difference at 95%.

    Resamples the DIFFERENCES, not the two arms independently — resampling
    independently throws away the pairing that is the entire point.
    """
    if not diffs:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(diffs)
    means = []
    for _ in range(iters):
        means.append(statistics.fmean(rng.choices(list(diffs), k=n)))
    means.sort()
    return (statistics.fmean(diffs),
            means[int(0.025 * iters)], means[int(0.975 * iters)])


@dataclass
class Verdict:
    a: str
    b: str
    n: int
    agreed: int
    mean_diff: float
    ci: tuple
    diff_on_disagreements: Optional[float]
    verdict: str

    @property
    def agreement_rate(self) -> float:
        return self.agreed / self.n if self.n else 0.0

    def render(self) -> str:
        d = ("n/a" if self.diff_on_disagreements is None
             else f"{self.diff_on_disagreements:+.3f}R")
        return (f"  {self.a} vs {self.b}\n"
                f"    paired states      {self.n}\n"
                f"    agreed on          {self.agreed} ({self.agreement_rate:.0%})\n"
                f"    mean paired diff   {self.mean_diff:+.3f}R  "
                f"95% CI [{self.ci[0]:+.3f}, {self.ci[1]:+.3f}]\n"
                f"    on disagreements   {d}\n"
                f"    {self.verdict}")


def compete(rows: Sequence[dict], a: str, b: str, *,
            arm_key: str = "vision", trials: int = 1) -> Verdict:
    """Head to head on identical states.

    `trials` is how many comparisons are being run in total, and it inflates the
    bar. Comparing eight arms pairwise is 28 trials; judging each at 95% means
    roughly a 3-in-4 chance that at least one looks significant by luck.
    """
    arms = collect(rows, arm_key=arm_key)
    p = check_pairing(arms, a, b)

    diffs, dis_diffs, agreed = [], [], 0
    for sid in p.shared:
        ra, rb = arms[a].by_state[sid], arms[b].by_state[sid]
        va, vb = _outcome_r(ra), _outcome_r(rb)
        if va is None or vb is None:
            continue
        same = _decision_of(ra) == _decision_of(rb)
        agreed += 1 if same else 0
        diffs.append(va - vb)
        if not same:
            dis_diffs.append(va - vb)

    mean, lo, hi = paired_bootstrap(diffs)
    if len(diffs) < MIN_PAIRED:
        v = (f"UNDETERMINED — {len(diffs)} resolved paired states is below the "
             f"{MIN_PAIRED} floor. No verdict is available and none should be "
             f"inferred from the sign of the mean.")
    elif lo > 0 or hi < 0:
        v = (f"{a if mean > 0 else b} is ahead on this sample. With {trials} "
             f"comparison(s) in the family the bar is higher than 95% — apply "
             f"BH-FDR across all of them before calling it a result, and seal it "
             f"as a hypothesis rather than promoting it here.")
    else:
        v = ("no separation — the interval spans zero. On paired states that is "
             "a real answer: these two arms are not distinguishable on this "
             "sample, and running the cheaper one is the rational choice.")
    return Verdict(a, b, len(diffs), agreed, mean, (lo, hi),
                   statistics.fmean(dis_diffs) if dis_diffs else None, v)


def report(rows: Sequence[dict], *, arm_key: str = "vision") -> str:
    arms = collect(rows, arm_key=arm_key)
    out = [f"MODEL COMPETITION (#7, {COMPETITION_VERSION})", "",
           f"  arm key: {arm_key}"]
    if len(arms) < 2:
        out += ["", f"  {len(arms)} arm(s) present — a competition needs two.",
                "  Running one arm and reporting its R is not a comparison, it is",
                "  a description of a market."]
        if arms:
            out += ["", "  present: " + ", ".join(
                f"{k} (n={v.n})" for k, v in sorted(arms.items()))]
        return "\n".join(out)

    names = sorted(arms)
    pairs = [(names[i], names[j]) for i in range(len(names))
             for j in range(i + 1, len(names))]
    out += ["", "PAIRING"]
    for a, b in pairs:
        out.append(check_pairing(arms, a, b).render())
    out += ["", "HEAD TO HEAD"]
    for a, b in pairs:
        out.append(compete(rows, a, b, arm_key=arm_key, trials=len(pairs)).render())
    out += ["", f"  {len(pairs)} comparison(s) in this family. Every one is a",
            "  trial, and the multiplicity correction has to see all of them —",
            "  otherwise the first arm that gets lucky wins permanently."]
    return "\n".join(out)
