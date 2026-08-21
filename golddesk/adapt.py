"""Closed-loop adaptation — evidence changes behaviour without a human.

WHAT ADAPTS AUTOMATICALLY
  1. Cohort hit rates feeding the expectancy gate. Pure measurement — as the
     ledger fills, ev_gate's estimates move, and what the desk takes changes
     with them. Nothing is "decided"; the arithmetic just gets better.
  2. Hypotheses: mined from the ledger, SEALED with zero authority, granted a
     veto only after independent post-seal confirmation clears the frozen
     standard and survives multiple-testing correction. Authority expires.
  3. Policy selection among competing versions (management chooser, re-entry
     policy) by paired comparison on identical states — and the winner is
     BOUND into durable PolicyState, which is what the running desk reads.

WHAT NEVER ADAPTS
  Risk invariants, the stop ratchet, the resolution rule, and the evidence bar
  itself. A system that can lower its own standard of proof has no standard.
  These are listed in FROZEN below and the cycle refuses to touch them.

TWO DEFECTS THIS REVISION REPAIRS

  * Promotion used to be decided by splitting the very sample the rule was
    mined from into halves and checking they agreed. A spurious rule passes
    that test comfortably. Router promotion now runs through HypothesisBook,
    where confirmation may only use outcomes that occurred after the claim was
    sealed. See hypothesis.py for the lifecycle.

  * Policy adaptation used to append a Change row saying the policy had
    changed, and then not change it. Nothing read those rows. The cycle now
    writes through PolicyState, which LiveDesk consults on every decision, and
    which survives restart.

Every change is written to an audit trail with the evidence that justified it
and is individually revertible. dry_run=True shows what would change.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .evaluation import bh_fdr, ess, paired_bootstrap, paired_p_value
from .hypothesis import Hypothesis, HypothesisBook, Stage
from .opportunity import CohortStat, build_cohorts, resolved_outcomes
from .policy_state import PolicyState

log = logging.getLogger(__name__)

ADAPT_VERSION = "adapt-2026-08-14-b"

FROZEN = (
    "max_open_risk_r", "max_daily_loss_r", "correlation_haircut",
    "stop_ratchet", "resolution_rule", "min_ess", "fdr_q",
    "std_min_n", "std_min_ess", "std_min_quarters", "std_fdr_q",
)

# A policy must win by more than the noise floor to be worth a switch. This is
# a SWITCHING COST, not a performance threshold: churning the live policy has
# its own costs (loss of comparability, operational risk), so a dead heat
# should leave the incumbent alone.
MIN_EDGE_R_PER_STATE = 0.0


@dataclass
class Change:
    target: str
    field: str
    before: Any
    after: Any
    evidence: str
    applied: bool
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def render(self) -> str:
        mark = "APPLIED " if self.applied else "PROPOSED"
        return (f"  [{mark}] {self.target}.{self.field}: {self.before} -> {self.after}\n"
                f"            {self.evidence}")


@dataclass
class AdaptationReport:
    cycle_ts: str
    rows_read: int
    cohorts: dict
    changes: list
    refused: list
    hypothesis_moves: list = field(default_factory=list)

    def render(self) -> str:
        out = [f"ADAPTATION CYCLE {self.cycle_ts}  ({ADAPT_VERSION})",
               f"  ledger rows read: {self.rows_read}",
               f"  cohorts tracked : {len(self.cohorts)}"]
        for k, c in sorted(self.cohorts.items()):
            flag = "" if c.informative else "  (thin — shrunk toward prior)"
            out.append(f"    {k:<32} n={c.n:<5} hit={c.hit_rate_shrunk:.1%} "
                       f"meanR={c.mean_r:+.3f}{flag}")
        if self.hypothesis_moves:
            out.append("  hypothesis lifecycle:")
            out += [f"    {hid} -> {stage}: {why}" for hid, stage, why in self.hypothesis_moves]
        out.append(f"  changes: {sum(1 for c in self.changes if c.applied)} applied, "
                   f"{sum(1 for c in self.changes if not c.applied)} proposed")
        out += [c.render() for c in self.changes]
        if self.refused:
            out.append("  refused (frozen or insufficient evidence):")
            out += [f"    {r}" for r in self.refused]
        return "\n".join(out)


class Adapter:
    """One cycle = read the ledger, update what measurement permits, log it."""

    def __init__(self, trail: Path, *, policy_state: Optional[PolicyState] = None,
                 book: Optional[HypothesisBook] = None, dry_run: bool = False):
        self.trail = Path(trail)
        self.dry_run = dry_run
        self.trail.parent.mkdir(parents=True, exist_ok=True)
        self.policy_state = policy_state
        self.book = book

    # -- guard -----------------------------------------------------------
    def _refuse_if_frozen(self, field: str) -> Optional[str]:
        if field in FROZEN:
            return f"{field} is FROZEN — adaptation may not alter a risk invariant"
        return None

    # -- the cycle -------------------------------------------------------
    def run(self, rows: Sequence[dict],
            cohorts_now: Optional[dict[str, CohortStat]] = None,
            policy_results: Optional[dict[str, dict[str, Sequence[float]]]] = None,
            *, on: Optional[date] = None) -> AdaptationReport:
        """
        policy_results maps SLOT -> {policy_version: [per-state R]}. The
        sequences must be aligned: index i is the same market state under every
        policy, which is what LiveDesk's shadow log produces.
        """
        changes: list[Change] = []
        refused: list[str] = []
        moves: list = []

        # 1. Cohort statistics — recomputed every cycle, no decision involved.
        cohorts = cohorts_now or build_cohorts(rows)

        # 2. Hypotheses: accrue post-seal evidence, then adjudicate. Discovery
        #    happens elsewhere and grants nothing; only this step moves
        #    authority, and only on data the claim has never seen.
        if self.book is not None:
            self.book.accrue(rows)
            if not self.dry_run:
                moves = self.book.adjudicate(on=on)
            for hid, stage, why in moves:
                changes.append(Change(f"hypothesis.{hid}", "stage", "-", stage,
                                      why, applied=True))

        # 3. Policy selection by paired comparison on identical states, then
        #    BOUND — the cycle changes what runs, not merely what is logged.
        for slot, arms in (policy_results or {}).items():
            changes_here, refused_here = self._select_policy(slot, arms, on=on)
            changes += changes_here
            refused += refused_here

        rep = AdaptationReport(datetime.now(timezone.utc).isoformat(), len(rows),
                               cohorts, changes, refused, moves)
        self._write(rep)
        return rep

    def _select_policy(self, slot: str, arms: dict[str, Sequence[float]],
                       *, on: Optional[date] = None) -> tuple[list, list]:
        changes: list[Change] = []
        refused: list[str] = []
        if len(arms) < 2:
            return changes, refused
        incumbent = (self.policy_state.active(slot) if self.policy_state else None)
        names = list(arms)
        base = incumbent if incumbent in arms else names[0]

        pvals, cands = [], []
        for other in names:
            if other == base:
                continue
            a, b = arms[base], arms[other]
            n = min(len(a), len(b))
            if n < 10:
                refused.append(f"{slot}/{other}: only {n} paired states — not judged")
                continue
            deltas = [b[i] - a[i] for i in range(n)]
            mean, lo, hi = paired_bootstrap(deltas)
            p = paired_p_value(deltas)
            pvals.append(p)
            cands.append((other, mean, lo, hi, p, ess(deltas), n))

        if not cands:
            return changes, refused
        # trials inflated: every arm on every slot is a shot on goal, and the
        # analyst has looked at this question before
        flags = bh_fdr(pvals, 0.10, n_trials=len(pvals) * 8)
        winners = [(c, ok) for c, ok in zip(cands, flags)]
        for (other, mean, lo, hi, p, e, n), ok in winners:
            warrant = (f"paired {mean:+.4f}R/state over n={n} identical states, "
                       f"CI [{lo:+.4f},{hi:+.4f}] p={p:.4f} ESS {e:.0f}")
            if ok and lo > MIN_EDGE_R_PER_STATE:
                applied = False
                if not self.dry_run and self.policy_state is not None:
                    self.policy_state.bind(slot, other, warrant + " — survives FDR",
                                           {"mean": mean, "lo": lo, "hi": hi,
                                            "p": p, "ess": e, "n": n,
                                            "beat": base}, on=on)
                    applied = True
                changes.append(Change(f"policy.{slot}", "active", base, other,
                                      warrant + " — survives FDR", applied))
            else:
                refused.append(f"{slot}/{other}: {warrant} — does not clear FDR "
                               f"or CI includes zero; incumbent {base} retained")
        return changes, refused

    # -- discovery (proposes, never promotes) -----------------------------
    def discover(self, rows: Sequence[dict], selectors: Sequence[dict],
                 *, seal_ts: Optional[datetime] = None,
                 min_n: int = 12) -> list[Hypothesis]:
        """Mine cohorts and SEAL what looks real. Grants no authority at all.

        The sealed hypothesis records the instant beyond which it had seen
        nothing. Everything after that instant is the only evidence permitted
        to promote it later, which is the entire difference between this and
        the half-split it replaces.
        """
        if self.book is None:
            return []
        seal = seal_ts or datetime.now(timezone.utc)
        out = []
        outcomes = resolved_outcomes(rows)
        for sel in selectors:
            rs = []
            for o in outcomes:
                t0 = o.get("t0")
                when = datetime.fromisoformat(t0) if isinstance(t0, str) else t0
                if when is None or when > seal:
                    continue                       # future data is not discovery
                ctx = o["context"]
                if all(o.get(k, ctx.get(k)) == v for k, v in sel.items()):
                    rs.append(o["realised_r"])
            if len(rs) < min_n:
                continue
            mean = statistics.fmean(rs)
            hid = "-".join(f"{k}={v}" for k, v in sorted(sel.items()))
            h = Hypothesis(
                hid=hid,
                statement=(f"cohort {hid} realises {mean:+.4f}R/trade "
                           f"(discovery n={len(rs)})"),
                selector=dict(sel), predicted_sign=1 if mean > 0 else -1,
                discovered_on=seal.date().isoformat(), seal_ts=seal.isoformat(),
                discovery_n=len(rs), discovery_mean_r=round(mean, 4))
            out.append(self.book.seal(h))
        return out

    def _write(self, rep: AdaptationReport) -> None:
        with self.trail.open("a") as fh:
            fh.write(json.dumps({
                "ts": rep.cycle_ts, "version": ADAPT_VERSION,
                "rows": rep.rows_read,
                "cohorts": {k: asdict(v) for k, v in rep.cohorts.items()},
                "changes": [asdict(c) for c in rep.changes],
                "hypothesis_moves": rep.hypothesis_moves,
                "refused": rep.refused}, default=str) + "\n")

    # -- reversibility ---------------------------------------------------
    def history(self) -> list[dict]:
        if not self.trail.exists():
            return []
        return [json.loads(l) for l in self.trail.read_text(encoding='utf-8').splitlines() if l.strip()]

    def revert_last(self) -> list[Change]:
        """Every automatic change is undoable. Adaptation without a reverse gear
        is not adaptation, it is drift."""
        hist = self.history()
        if not hist:
            return []
        undone = []
        for c in hist[-1].get("changes", []):
            if not c.get("applied"):
                continue
            target = c["target"]
            if target.startswith("policy.") and self.policy_state is not None:
                slot = target.split(".", 1)[1]
                now = self.policy_state.revert(slot)
                undone.append(Change(target, c["field"], c["after"], now,
                                     "reverted", True))
            elif target.startswith("hypothesis.") and self.book is not None:
                hid = target.split(".", 1)[1]
                h = self.book.items.get(hid)
                if h is not None and h.stage == Stage.ENFORCING.value:
                    h.stage = Stage.CONFIRMING.value
                    h.enforcing_since = h.expires = None
                    self.book._write()
                    undone.append(Change(target, "stage", Stage.ENFORCING.value,
                                         Stage.CONFIRMING.value, "reverted", True))
        return undone
