"""Sealed hypotheses — discovery may propose, only independent data may enforce.

THE DEFECT THIS EXISTS TO FIX

`adapt._evidence_for()` built a rule's warrant by taking the trades that matched
it, splitting them in half, calling the first half "discovery" and the second
half "confirmation", and promoting the rule to ENFORCING — a permanent veto over
future trades — when both halves agreed.

That is not confirmation. The rule was *found* by looking at the whole sample,
including the second half. Splitting a set you already mined tells you the
finding is internally consistent, which a spurious finding also is. The desk was
granting veto authority on the strength of its own data-mining.

THE LIFECYCLE THAT REPLACES IT

    DISCOVERED   found by mining data up to some instant. Zero authority.
                 It may not refuse a single trade.
    SEALED       the statement is frozen and hashed together with the exact
                 timestamp beyond which it had seen nothing. From this moment
                 the hypothesis cannot be edited, only judged.
    CONFIRMING   accumulating outcomes that occurred STRICTLY AFTER the seal.
                 These are out-of-sample by construction of time, not by
                 partition of a mined set.
    ENFORCING    post-seal evidence cleared the frozen standard AND survived
                 multiple-testing correction across every sealed hypothesis.
                 Authority is granted with an expiry.
    LAPSED       the expiry passed. Back to CONFIRMING; it must re-earn the
                 veto on data it has never been judged on.
    REJECTED     post-seal evidence contradicted the discovery. Kept forever —
                 a graveyard of dead hypotheses is how the trials count stays
                 honest.

Editing a sealed hypothesis changes its hash and invalidates its evidence. That
is the point: you cannot tune a rule and keep the confirmation it earned before
the tuning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import statistics
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from .evaluation import bh_fdr, ess, paired_p_value
from .opportunity import resolved_outcomes

log = logging.getLogger(__name__)

HYPOTHESIS_VERSION = "hyp-2026-08-14-a"

# The frozen standard. Raising these mid-flight to make a result pass is the
# exact failure the preregistration machinery exists to prevent, so they are
# recorded on every hypothesis at seal time and the stored copy is what judges
# it — not whatever this module says later.
MIN_POST_SEAL_N = 25        # outcomes observed after the seal
MIN_ESS = 20.0              # autocorrelation-adjusted effective sample size
MIN_QUARTERS_AGREEING = 3   # of 4 — the effect is not one quarter's accident
FDR_Q = 0.10
ENFORCE_TTL_DAYS = 180


class Stage(str, Enum):
    DISCOVERED = "DISCOVERED"
    SEALED = "SEALED"
    CONFIRMING = "CONFIRMING"
    ENFORCING = "ENFORCING"
    LAPSED = "LAPSED"
    REJECTED = "REJECTED"


@dataclass
class Hypothesis:
    """A frozen claim about the market, plus everything needed to judge it."""

    hid: str
    statement: str                 # human-readable claim
    selector: dict                 # the exact cohort match, as data
    predicted_sign: int            # -1 = this cohort loses money, +1 = makes it
    discovered_on: str             # ISO date
    seal_ts: str                   # outcomes at or before this are DISCOVERY
    discovery_n: int
    discovery_mean_r: float
    stage: str = Stage.DISCOVERED.value

    # frozen standard, copied at seal so later edits cannot loosen it
    std_min_n: int = MIN_POST_SEAL_N
    std_min_ess: float = MIN_ESS
    std_min_quarters: int = MIN_QUARTERS_AGREEING
    std_fdr_q: float = FDR_Q

    # post-seal accumulation
    post_n: int = 0
    post_mean_r: float = 0.0
    post_ess: float = 0.0
    post_quarters_agreeing: int = 0
    post_p: float = 1.0

    enforcing_since: Optional[str] = None
    expires: Optional[str] = None
    note: str = ""

    # -- identity ---------------------------------------------------------
    def content_hash(self) -> str:
        """Hash of the CLAIM, not of the evidence.

        Deliberately excludes post_* fields: evidence accrues without changing
        identity, but altering the statement, the selector, the predicted sign,
        the seal instant or the standard produces a different hypothesis whose
        prior confirmation does not transfer.
        """
        payload = json.dumps({
            "statement": self.statement, "selector": self.selector,
            "predicted_sign": self.predicted_sign, "seal_ts": self.seal_ts,
            "std": [self.std_min_n, self.std_min_ess,
                    self.std_min_quarters, self.std_fdr_q]},
            sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def matches(self, ctx: dict, decision: dict) -> bool:
        """Does one ledger row fall inside this hypothesis's cohort?"""
        for k, v in self.selector.items():
            got = decision.get(k, ctx.get(k))
            if got != v:
                return False
        return True

    # -- judgement --------------------------------------------------------
    def clears_standard(self) -> tuple[bool, str]:
        """Judged ONLY on post-seal evidence, against the STORED standard."""
        if self.post_n < self.std_min_n:
            return False, f"post-seal n={self.post_n} < {self.std_min_n}"
        if self.post_ess < self.std_min_ess:
            return False, f"post-seal ESS={self.post_ess:.1f} < {self.std_min_ess}"
        if self.post_quarters_agreeing < self.std_min_quarters:
            return False, (f"only {self.post_quarters_agreeing}/4 quarters agree "
                           f"(need {self.std_min_quarters})")
        got_sign = 1 if self.post_mean_r > 0 else -1
        if got_sign != self.predicted_sign:
            return False, (f"post-seal sign {got_sign:+d} contradicts discovery "
                           f"{self.predicted_sign:+d}")
        return True, (f"post-seal {self.post_mean_r:+.4f}R over n={self.post_n} "
                      f"(ESS {self.post_ess:.0f}), {self.post_quarters_agreeing}/4 "
                      f"quarters, p={self.post_p:.4f}")

    def render(self) -> str:
        ok, why = self.clears_standard() if self.post_n else (False, "no post-seal data yet")
        exp = f" expires {self.expires}" if self.expires else ""
        return (f"[{self.stage:<10}] {self.hid} ({self.content_hash()}){exp}\n"
                f"    {self.statement}\n"
                f"    sealed {self.seal_ts[:10]} · discovery n={self.discovery_n} "
                f"{self.discovery_mean_r:+.4f}R\n"
                f"    post-seal: {why}")


class HypothesisBook:
    """Every hypothesis the desk has ever sealed, including the dead ones.

    Kept whole and on disk. The rejected ones are load-bearing: they are the
    denominator that keeps the multiple-testing correction honest. Delete them
    and every surviving rule starts looking significant.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, Hypothesis] = {}
        self.load()

    def load(self) -> "HypothesisBook":
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self.items = {k: Hypothesis(**v) for k, v in raw.get("items", {}).items()}
            except (json.JSONDecodeError, OSError, TypeError) as e:
                log.error("hypothesis book unreadable (%s) — starting empty", e)
                self.items = {}
        return self

    def _write(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"version": HYPOTHESIS_VERSION,
                           "items": {k: asdict(v) for k, v in self.items.items()}},
                          fh, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- lifecycle --------------------------------------------------------
    def seal(self, h: Hypothesis) -> Hypothesis:
        """Freeze a discovery. After this the claim cannot be edited in place."""
        existing = self.items.get(h.hid)
        if existing and existing.content_hash() != h.content_hash():
            raise ValueError(
                f"{h.hid} already sealed with a different claim "
                f"({existing.content_hash()} != {h.content_hash()}). Seal it under "
                f"a new id — a re-tuned rule does not inherit confirmation.")
        if existing:
            return existing
        h.stage = Stage.SEALED.value
        self.items[h.hid] = h
        self._write()
        log.info("sealed hypothesis %s (%s): %s", h.hid, h.content_hash(), h.statement)
        return h

    def accrue(self, rows: Sequence[dict]) -> None:
        """Fold resolved outcomes into every sealed hypothesis they match.

        Only rows strictly AFTER a hypothesis's seal instant count toward its
        confirmation. Rows at or before it are discovery and are ignored here,
        which is what makes the evidence independent rather than re-mined.
        """
        for h in self.items.values():
            if h.stage in (Stage.DISCOVERED.value, Stage.REJECTED.value):
                continue
            seal = datetime.fromisoformat(h.seal_ts)
            rs: list[float] = []
            for o in resolved_outcomes(rows):
                t0 = o.get("t0")
                if not t0:
                    continue
                when = datetime.fromisoformat(t0) if isinstance(t0, str) else t0
                if when <= seal:
                    continue                     # discovery era — not evidence
                if not h.matches(o["context"], o):
                    continue
                rs.append(o["realised_r"])
            if not rs:
                continue
            quarters = [rs[i::4] for i in range(4)]
            mean = statistics.fmean(rs)
            agree = sum(1 for q in quarters
                        if q and (statistics.fmean(q) > 0) == (mean > 0))
            h.post_n = len(rs)
            h.post_mean_r = round(mean, 4)
            h.post_ess = round(ess(rs), 2)
            h.post_quarters_agreeing = agree
            h.post_p = round(paired_p_value(rs), 4)
            if h.stage == Stage.SEALED.value:
                h.stage = Stage.CONFIRMING.value
        self._write()

    def adjudicate(self, *, on: Optional[date] = None) -> list[tuple[str, str, str]]:
        """Promote, lapse and reject — the only place authority changes.

        Multiple-testing correction runs across EVERY hypothesis under
        confirmation simultaneously, so a rule cannot slip through by being
        judged alone on a day when its neighbours were quiet.
        """
        day = on or datetime.now(timezone.utc).date()
        moves: list[tuple[str, str, str]] = []

        # expiry first: authority lapses on the calendar, not on a cycle running
        for h in self.items.values():
            if h.stage == Stage.ENFORCING.value and h.expires:
                if day > date.fromisoformat(h.expires):
                    h.stage, h.enforcing_since, h.expires = Stage.LAPSED.value, None, None
                    h.post_n = 0          # must be re-earned on data not yet seen
                    h.post_mean_r = h.post_ess = 0.0
                    h.post_quarters_agreeing, h.post_p = 0, 1.0
                    moves.append((h.hid, Stage.LAPSED.value,
                                  "warrant expired — veto surrendered, must re-prove"))

        candidates = [h for h in self.items.values()
                      if h.stage in (Stage.CONFIRMING.value, Stage.LAPSED.value)
                      and h.post_n > 0]
        if candidates:
            # trials count includes the graveyard: every hypothesis ever sealed
            # was a shot on goal, and pretending otherwise inflates significance
            n_trials = max(len(self.items), len(candidates))
            flags = bh_fdr([h.post_p for h in candidates], FDR_Q, n_trials=n_trials)
            for h, survived in zip(candidates, flags):
                ok, why = h.clears_standard()
                if ok and survived:
                    h.stage = Stage.ENFORCING.value
                    h.enforcing_since = day.isoformat()
                    h.expires = (day + timedelta(days=ENFORCE_TTL_DAYS)).isoformat()
                    moves.append((h.hid, Stage.ENFORCING.value, why))
                elif h.post_n >= h.std_min_n and not ok and "contradicts" in why:
                    h.stage = Stage.REJECTED.value
                    h.note = why
                    moves.append((h.hid, Stage.REJECTED.value, why))
                elif ok and not survived:
                    moves.append((h.hid, h.stage,
                                  f"clears its own standard but not FDR across "
                                  f"{n_trials} trials — stays advisory"))
        self._write()
        return moves

    # -- consumption ------------------------------------------------------
    def enforcing(self, *, on: Optional[date] = None) -> list[Hypothesis]:
        day = on or datetime.now(timezone.utc).date()
        return [h for h in self.items.values()
                if h.stage == Stage.ENFORCING.value
                and (not h.expires or day <= date.fromisoformat(h.expires))]

    def veto(self, ctx: dict, decision: dict, *,
             on: Optional[date] = None) -> Optional[Hypothesis]:
        """The only path by which a discovered rule may refuse a live trade."""
        for h in self.enforcing(on=on):
            if h.predicted_sign < 0 and h.matches(ctx, decision):
                return h
        return None

    def render(self) -> str:
        if not self.items:
            return "HYPOTHESIS BOOK: empty — nothing discovered, nothing enforcing"
        by = sorted(self.items.values(), key=lambda h: (h.stage, h.hid))
        counts: dict[str, int] = {}
        for h in by:
            counts[h.stage] = counts.get(h.stage, 0) + 1
        head = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return "\n".join([f"HYPOTHESIS BOOK ({HYPOTHESIS_VERSION})  {head}"]
                         + [h.render() for h in by])
