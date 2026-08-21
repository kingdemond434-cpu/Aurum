"""Hypotheses and the runs that judged them, welded together.

The desk keeps sealed hypotheses in one file and run results in another, with
nothing connecting them. Both are careful documents. The gap between them is
where the desk's single most expensive error lives, and it is not untidiness.

WHY THIS IS A MULTIPLICITY PROBLEM AND NOT A BOOKKEEPING ONE

`hypothesis.py` adjudicates with a BH-FDR correction, and FDR needs a
DENOMINATOR: how many things were tried. The desk's own canonical note calls
trial counting "the missing half". A run that tests an idea and is never linked
to a registered hypothesis is a trial that happened and was not counted — and
the runs most likely to go unlinked are exactly the ones that found nothing,
because nobody writes up a null. That biases the census in the one direction
guaranteed to matter: every surviving result is corrected against a trial count
smaller than the truth, so every q-value the desk has ever computed is too
small, and confidence rises precisely as the record gets less complete.

Making the link MANDATORY at write time makes the census correct by
construction. `register_run()` refuses a run that names no hypothesis. There is
no flag to skip it, because a skippable version would be skipped in exactly the
circumstances that corrupt the count.

THE SECOND FAILURE: SILENT RESURRECTION

A hypothesis is tested, rejected, and six weeks later somebody proposes it again
in slightly different words. Nothing objects. It gets a fresh seal, a fresh
sample and a fresh chance to clear the bar by luck, and the desk has quietly run
the same test twice while counting it once.

`hypothesis.content_hash()` already hashes the CLAIM rather than the evidence,
which makes an exact re-proposal detectable — so `check_resurrection()` surfaces
the invalidation note before a duplicate can be sealed. It reports rather than
blocks: a genuine re-test after new data is legitimate, and the requirement is
that it be a DECISION, made against the reason the idea died the first time,
instead of an accident.

WHAT AN INVALIDATION NOTE IS FOR

"REJECTED" is a status. It tells the next person nothing about whether to try
again. The note carries which run killed it, on what evidence, and what would
have to be different — so re-proposing becomes an argument with a record rather
than a fresh start.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

LINKAGE_VERSION = "link-2026-08-18-a"


class OrphanRun(Exception):
    """A run was offered that tests no registered hypothesis.

    Raised, never warned. A run whose hypothesis link is optional is a trial
    that will go uncounted in precisely the cases that corrupt the FDR
    denominator — the ones that found nothing.
    """


@dataclass
class Run:
    """One execution of one experiment against one or more hypotheses."""
    rid: str
    hypothesis_ids: tuple[str, ...]
    kind: str                        # backtest | replay | shadow | live | ablation
    started_utc: str
    #: Hash of the configuration the run used. Two runs of the same experiment
    #: with different parameters are two trials, and this is what distinguishes
    #: them when the prose description does not.
    config_hash: str = ""
    n_observations: int = 0
    result_summary: str = ""
    #: SUPPORTS | CONTRADICTS | INCONCLUSIVE | ABANDONED. Every one of these is a
    #: trial. ABANDONED especially: a run killed halfway because it looked
    #: unpromising is a peek at the data, and peeking is a trial.
    outcome: str = "INCONCLUSIVE"
    note: str = ""

    def to_dict(self) -> dict:
        return {"rid": self.rid, "hypothesis_ids": list(self.hypothesis_ids),
                "kind": self.kind, "started_utc": self.started_utc,
                "config_hash": self.config_hash,
                "n_observations": self.n_observations,
                "result_summary": self.result_summary, "outcome": self.outcome,
                "note": self.note}

    @staticmethod
    def from_dict(d: dict) -> "Run":
        return Run(rid=d["rid"], hypothesis_ids=tuple(d.get("hypothesis_ids", ())),
                   kind=d.get("kind", "unknown"),
                   started_utc=d.get("started_utc", ""),
                   config_hash=d.get("config_hash", ""),
                   n_observations=int(d.get("n_observations", 0)),
                   result_summary=d.get("result_summary", ""),
                   outcome=d.get("outcome", "INCONCLUSIVE"),
                   note=d.get("note", ""))


@dataclass
class Invalidation:
    """Why a hypothesis died, and what would have to change to revisit it."""
    hid: str
    content_hash: str
    killed_by_run: str
    on_utc: str
    evidence: str
    what_would_change_it: str = ""

    def render(self) -> str:
        revisit = self.what_would_change_it or (
            "NOT STATED — which means nobody knows what new evidence would be "
            "enough, so the idea will come back by accident")
        return (f"{self.hid} was rejected on {self.on_utc[:10]} by run "
                f"{self.killed_by_run}\n    evidence: {self.evidence}\n"
                f"    would revisit if: {revisit}")


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class LinkedRegistry:
    """Runs, hypotheses and the welds between them."""
    runs: dict = field(default_factory=dict)
    invalidations: dict = field(default_factory=dict)   # content_hash -> Invalidation
    #: hids this registry considers registered. Supplied by the caller from the
    #: HypothesisBook so this module does not need to own or duplicate it.
    known_hids: set = field(default_factory=set)

    # -- writing -------------------------------------------------------
    def register_hypotheses(self, hids: Iterable[str]) -> None:
        self.known_hids.update(hids)

    def register_run(self, run: Run, *, allow_unknown_hid: bool = False) -> Run:
        """The weld. Refuses an orphan."""
        if not run.hypothesis_ids:
            raise OrphanRun(
                f"run {run.rid!r} names no hypothesis. Every run is a trial and "
                f"every trial must enter the FDR denominator; an unlinked run is "
                f"an uncounted one, and the runs that go unlinked are the ones "
                f"that found nothing. Register the hypothesis first — even a "
                f"null result is evidence about a claim somebody made.")
        if not allow_unknown_hid:
            unknown = [h for h in run.hypothesis_ids if h not in self.known_hids]
            if unknown:
                raise OrphanRun(
                    f"run {run.rid!r} references unregistered hypotheses "
                    f"{unknown}. A run pointing at an id nothing defines is an "
                    f"orphan with a footnote.")
        if run.rid in self.runs:
            raise ValueError(f"run {run.rid!r} already registered; a rerun is a "
                             f"NEW trial and needs its own id")
        self.runs[run.rid] = run
        return run

    def invalidate(self, hid: str, content_hash: str, killed_by_run: str,
                   evidence: str, what_would_change_it: str = "") -> Invalidation:
        if killed_by_run not in self.runs:
            raise ValueError(
                f"cannot invalidate on run {killed_by_run!r}: no such run. A "
                f"rejection with no run behind it is an opinion.")
        inv = Invalidation(hid, content_hash, killed_by_run,
                           datetime.now(timezone.utc).isoformat(), evidence,
                           what_would_change_it)
        self.invalidations[content_hash] = inv
        return inv

    # -- reading -------------------------------------------------------
    def runs_for(self, hid: str) -> list[Run]:
        return [r for r in self.runs.values() if hid in r.hypothesis_ids]

    def orphan_hypotheses(self) -> list[str]:
        """Registered, never tested by any run.

        Not an error — a freshly sealed hypothesis is legitimately untested. It
        is a WORKLIST: these are claims the desk is carrying and has not paid
        for, and one that stays here indefinitely is a belief nobody is checking.
        """
        tested = {h for r in self.runs.values() for h in r.hypothesis_ids}
        return sorted(self.known_hids - tested)

    def unlinked_runs(self) -> list[str]:
        """Impossible through `register_run`. Reachable by loading a legacy file,
        which is exactly when you need to know."""
        return sorted(r.rid for r in self.runs.values() if not r.hypothesis_ids)

    def check_resurrection(self, content_hash: str) -> Optional[Invalidation]:
        """Has this exact claim already been killed?

        Reports rather than blocks. A genuine re-test after new data is
        legitimate; the requirement is that it be a decision made against the
        reason the idea died, not an accident.
        """
        return self.invalidations.get(content_hash)

    # -- the census ----------------------------------------------------
    def trial_census(self) -> dict:
        """THE DENOMINATOR. What FDR has to be corrected against.

        Every run counts, including ABANDONED ones. A run killed halfway because
        it looked unpromising is a peek at the data, and a peek is a trial —
        excluding it is the same selection that makes a strategy backtest look
        good.
        """
        by_outcome: dict[str, int] = {}
        for r in self.runs.values():
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
        per_h = {h: len(self.runs_for(h)) for h in sorted(self.known_hids)}
        # Distinct configurations, because one experiment re-run with a
        # different parameter is a second trial however similar the prose.
        configs = {r.config_hash for r in self.runs.values() if r.config_hash}
        return {
            "version": LINKAGE_VERSION,
            "runs_total": len(self.runs),
            "by_outcome": by_outcome,
            "hypotheses_known": len(self.known_hids),
            "hypotheses_untested": len(self.orphan_hypotheses()),
            "runs_per_hypothesis": per_h,
            "distinct_configs": len(configs),
            "trials_for_fdr": len(self.runs),
            "note": ("Every run is a trial, ABANDONED included — a run stopped "
                     "because it looked unpromising is a peek, and a peek is a "
                     "trial. Correcting against a smaller number is how every "
                     "q-value on this desk ends up too small."),
        }

    def audit(self) -> tuple[bool, str]:
        """Is the record internally consistent enough to compute an FDR from?"""
        problems = []
        u = self.unlinked_runs()
        if u:
            problems.append(f"{len(u)} run(s) with no hypothesis: {u[:5]}")
        dangling = sorted({h for r in self.runs.values() for h in r.hypothesis_ids}
                          - self.known_hids)
        if dangling:
            problems.append(f"{len(dangling)} run(s) reference unknown hypotheses: "
                            f"{dangling[:5]}")
        no_reason = [i.hid for i in self.invalidations.values()
                     if not i.what_would_change_it]
        if no_reason:
            problems.append(
                f"{len(no_reason)} invalidation(s) with no stated revisit "
                f"condition {no_reason[:5]} — nobody knows what evidence would "
                f"be enough, so the idea will come back by accident")
        if not problems:
            return True, (f"{len(self.runs)} runs, {len(self.known_hids)} "
                          f"hypotheses, every run welded to a claim")
        return False, "; ".join(problems)

    # -- persistence ---------------------------------------------------
    def to_json(self, indent: int = 2) -> str:
        return json.dumps({
            "version": LINKAGE_VERSION,
            "known_hids": sorted(self.known_hids),
            "runs": [r.to_dict() for r in self.runs.values()],
            "invalidations": [i.__dict__ for i in self.invalidations.values()],
        }, indent=indent)

    def save(self, path: Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a torn write here loses the trial census, and the census is the
        # only thing standing between this desk and an over-confident q-value.
        tmp = p.with_suffix(".tmp")
        tmp.write_text(self.to_json())
        tmp.replace(p)

    @staticmethod
    def load(path: Path) -> "LinkedRegistry":
        p = Path(path)
        if not p.exists():
            return LinkedRegistry()
        d = json.loads(p.read_text(encoding='utf-8'))
        reg = LinkedRegistry(known_hids=set(d.get("known_hids", ())))
        for rd in d.get("runs", ()):
            # Loaded WITHOUT the orphan check, deliberately: a legacy file with
            # unlinked runs must be readable so `audit()` can report it. Refusing
            # to load is how a bad record becomes an invisible record.
            r = Run.from_dict(rd)
            reg.runs[r.rid] = r
        for i in d.get("invalidations", ()):
            reg.invalidations[i["content_hash"]] = Invalidation(**i)
        return reg


def render(reg: LinkedRegistry) -> str:
    c = reg.trial_census()
    ok, why = reg.audit()
    lines = [f"HYPOTHESIS-RUN LINKAGE  ({LINKAGE_VERSION})",
             f"  runs                 {c['runs_total']}",
             f"  distinct configs     {c['distinct_configs']}",
             f"  hypotheses           {c['hypotheses_known']}"
             f"  ({c['hypotheses_untested']} never tested)",
             f"  by outcome           {c['by_outcome']}",
             f"  TRIALS FOR FDR       {c['trials_for_fdr']}",
             "",
             f"  audit: {'OK' if ok else 'PROBLEMS'} — {why}"]
    orphans = reg.orphan_hypotheses()
    if orphans:
        lines += ["", f"  untested claims ({len(orphans)}): {', '.join(orphans[:8])}",
                  "    Carried but unpaid-for. Not an error; a worklist."]
    if reg.invalidations:
        lines += ["", "  invalidations:"]
        lines += [f"    {i.render()}" for i in reg.invalidations.values()]
    return "\n".join(lines)
