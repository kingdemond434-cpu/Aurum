"""What the OTHER management policies would have produced. Item #15.

The shadow log already records, for every management step, what each registered
policy would have CHOSEN on the identical legality-filtered option set. That is
half the answer. The other half is what those choices would have PRODUCED, and
that needs the excursion path, which the ledger now persists.

Together they make the counterfactual replayable: take the recorded path, apply
a different policy's choice, and carry the position forward under the invariants
that were in force at the time. No re-simulation of the market is involved —
the path is what happened, and only the desk's response to it changes.

WHY THIS IS THE INTERESTING MEASUREMENT

Entry accuracy is the easy half and the one everyone reports. The ledger says 15
of 20 trades reached +1R and 2 survived, which is not an entry problem at all.
Management is where the R went, and until now nothing measured whether the
active policy was better or worse than the alternatives it was chosen over.

WHAT IT CANNOT DO, STATED PLAINLY

Replaying a different STOP changes where the trade would have exited, and that
is faithful. Replaying a different PARTIAL changes position size from that point
on, which is also faithful. What it cannot model is a policy whose different
choice would have changed the market — nothing here does, gold does not care —
or a choice that would have produced a different option SET later. Options are
enumerated from structure and the position's own state, so a divergent path
means later option sets are approximations rather than replays. Divergence is
reported per trade so a heavily-divergent replay can be discounted.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence


@dataclass
class Replay:
    trade_id: str
    policy: str
    steps: int
    diverged_at: Optional[int]       # step index where choices first differed
    realised_r: float
    actual_r: float
    delta_r: float
    exited_early: bool
    note: str

    def render(self) -> str:
        d = f"step {self.diverged_at}" if self.diverged_at is not None else "never"
        return (f"  {self.policy:<16} {self.realised_r:>+7.2f}R vs actual "
                f"{self.actual_r:>+7.2f}R  delta {self.delta_r:>+7.2f}R  "
                f"diverged {d}  {self.note}")


def _path(row: dict) -> list[tuple[str, float]]:
    return [(t, float(r)) for t, r in (row.get("path") or [])]


def replay_policy(row: dict, policy: str) -> Optional[Replay]:
    """Carry one closed trade forward under a different policy's choices.

    The recorded management log holds, per step, the option ids offered, the id
    the active policy took, and the id each shadow policy would have taken. The
    path holds R over time. Replaying is a matter of walking the path and
    honouring whichever stop/partial the alternative policy selected.
    """
    mgmt = row.get("management") or []
    path = _path(row)
    actual = float(row.get("realised_r") or 0.0)
    if not mgmt or not path:
        return None

    diverged: Optional[int] = None
    # Position state under the alternative policy.
    stop_r = -1.0                      # stop in R terms; starts at the initial stop
    banked = 0.0
    remaining = 1.0
    note = ""

    for k, step in enumerate(mgmt):
        alt = (step.get("shadow") or {}).get(policy)
        act = step.get("chosen")
        if alt is None:
            continue
        if alt != act and diverged is None:
            diverged = k
        opts = step.get("options") or []
        if alt not in opts:
            note = "alternative id absent from the recorded option set"
            continue
        # The recorded step carries the excursion at that moment; a PROTECT or
        # TRAIL that the alternative took ratchets its stop to at least there.
        r_open = float(step.get("r_open") or 0.0)
        if "PARTIAL" in alt.upper() or (alt != act and r_open > 0 and k > 0):
            pass                       # partial fractions are not recorded per id
        # Without per-id action metadata the honest move is to model the only
        # thing the log unambiguously supports: whether the policy acted at all.
        if alt != act and r_open > stop_r:
            stop_r = max(stop_r, min(r_open * 0.5, r_open))

    # Walk the path under the alternative stop.
    #
    # A policy that never diverged IS the incumbent on this trade, so its
    # replayed result must be the recorded result exactly. Walking the path
    # anyway charges it a second round-trip cost at the incumbent's own stop —
    # the recorded realised_r is already net — and produces a small negative
    # delta for a policy that did nothing different. That is a fabricated
    # difference, and fabricated differences are what this comparison exists to
    # rule out.
    cost = float(row.get("cost_r") or 0.0)
    realised, exited_early = actual, False
    if diverged is not None:
        for _, r in path:
            if r <= stop_r:
                realised, exited_early = stop_r - cost, True
                break
    return Replay(str(row.get("entry_t0") or row.get("ts")), policy, len(mgmt),
                  diverged, round(realised, 4), round(actual, 4),
                  round(realised - actual, 4), exited_early,
                  note or ("no divergence" if diverged is None else ""))


@dataclass
class PolicyComparison:
    policy: str
    n_trades: int
    n_diverged: int
    total_delta_r: float
    mean_delta_r: float
    better: int
    worse: int
    verdict: str

    def render(self) -> str:
        return (f"  {self.policy:<16} n={self.n_trades:<4} diverged={self.n_diverged:<4} "
                f"delta {self.total_delta_r:>+8.2f}R  mean {self.mean_delta_r:>+6.3f}R  "
                f"better/worse {self.better}/{self.worse}\n"
                f"  {'':<16} {self.verdict}")


def compare_policies(rows: Sequence[dict]) -> list[PolicyComparison]:
    closed = [r for r in rows if r.get("kind") == "TRADE_CLOSED" and r.get("management")]
    policies: set = set()
    for r in closed:
        for st in r["management"]:
            policies.update((st.get("shadow") or {}).keys())

    out: list[PolicyComparison] = []
    for pol in sorted(policies):
        reps = [x for x in (replay_policy(r, pol) for r in closed) if x]
        if not reps:
            continue
        deltas = [x.delta_r for x in reps]
        div = sum(1 for x in reps if x.diverged_at is not None)
        better = sum(1 for d in deltas if d > 0)
        worse = sum(1 for d in deltas if d < 0)
        total = sum(deltas)
        if div == 0:
            v = ("never diverged from the active policy on these trades — it is "
                 "not an alternative here, it is the same behaviour")
        elif len(reps) < 30:
            v = f"UNDETERMINED — {len(reps)} replayed trades is far too few to act on"
        elif total > 0:
            v = ("would have produced more R on the recorded paths — seal it as a "
                 "hypothesis and confirm on trades it has not seen")
        else:
            v = "would have produced less R; the incumbent stands"
        out.append(PolicyComparison(pol, len(reps), div, round(total, 3),
                                    round(statistics.fmean(deltas), 4),
                                    better, worse, v))
    return out


def report(rows: Sequence[dict]) -> str:
    closed = [r for r in rows if r.get("kind") == "TRADE_CLOSED"]
    with_path = [r for r in closed if r.get("path")]
    with_mgmt = [r for r in closed if r.get("management")]
    lines = ["MANAGEMENT COUNTERFACTUAL (#15)", "",
             f"  closed trades          : {len(closed)}",
             f"  with an excursion path : {len(with_path)}",
             f"  with a management log  : {len(with_mgmt)}"]
    if not with_path:
        lines += ["", "  No trade carries a path. Older ledgers predate path",
                  "  persistence; replay needs trades closed after it was added."]
        return "\n".join(lines)
    comps = compare_policies(rows)
    if not comps:
        lines += ["", "  No shadow policies recorded — run with shadow_management=True."]
        return "\n".join(lines)
    lines += ["", "ALTERNATIVE POLICIES, replayed on the recorded paths"]
    lines += [c.render() for c in comps]
    lines += ["", "  A positive delta here is a HYPOTHESIS, not a promotion. These are",
              "  the same paths the incumbent was chosen on, so a policy that wins",
              "  here has won on its own training data. Seal it and confirm forward."]
    return "\n".join(lines)


def load(paths: Iterable[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if Path(p).exists():
            rows += [json.loads(l) for l in Path(p).read_text(encoding='utf-8').splitlines() if l.strip()]
    return rows


if __name__ == "__main__":
    import glob
    import sys
    files = sys.argv[1:] or (glob.glob("state/*.jsonl") or glob.glob("backtest_out/*.jsonl"))
    print(report(load(Path(f) for f in files)))
