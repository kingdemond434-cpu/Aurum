"""What the quant desk learns about gold, arriving in Aurum without cargo-culting.

The MT5 desk researches a universe Aurum does not trade, and some of what it
finds is about GOLD rather than about that universe: how session ranges behave,
what a broker's spread does around the London fix, that a macro series revises,
that a labelling scheme leaked. Those transfer. Most of what it finds does not.

The failure mode this is built against is not losing the findings. It is the
opposite: a channel that faithfully accumulates every finding into a folder
nobody reads and nothing acts on, which feels like knowledge management and
changes no decision. Aurum already has fifteen canonical notes. Adding a
sixteenth is not learning.

SO ABSORPTION IS DEFINED BY WHAT IT CHANGES, NOT BY WHAT IT STORES

A finding is absorbed when it has been turned into a claim Aurum can be wrong
about, tested against Aurum's OWN data, and had its effect on Aurum's decisions
measured. Everything short of that is a note, and this module calls it a note.

THE RULE THAT MAKES CROSS-DESK TRANSFER SAFE

An external finding enters as a SEALED HYPOTHESIS at zero authority, whatever
its evidence grade. Not as a rule, not as a threshold, not as a prior. The
contributor brief already says it — "an E5 external finding is still only a
hypothesis here" — and this is where that stops being a policy and becomes a
type. A mechanism that worked on CADJPY is evidence about CADJPY; asserting it
about XAUUSD because the same code produced it is exactly the cargo-culting a
transfer channel exists to prevent.

WHY THE NEGATIVE RESULTS ARE THE PART THAT COMPOUNDS

A finding that fails to replicate on Aurum's data is recorded as
NON_TRANSFERABLE with the reason, and its content hash is remembered. That is
what stops the same idea being re-absorbed every cycle by a process with no
memory of having already tried it. A loop that only records successes does not
get smarter over time — it gets more confident, and re-runs the same failures
forever because nothing remembers they failed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

ABSORB_VERSION = "absorb-2026-08-18-a"

#: The contributor brief's scale. E0 marketing claim, E5 independently
#: reproduced. Carried so a loud claim and a measured result are not weighted
#: the same — absence of grading is how the loudest source wins by default.
GRADES = {
    "E0": "marketing claim",
    "E1": "anecdote, screenshot, self-report, or a model's general knowledge",
    "E2": "public backtest",
    "E3": "limited monitored live",
    "E4": "substantial monitored live",
    "E5": "independently reproduced",
}

#: Below this grade a finding is not even worth the cost of a transfer test. It
#: is recorded so the same claim is not re-evaluated next cycle, and nothing
#: else happens to it.
MIN_GRADE_TO_TEST = "E2"

#: Status vocabulary. NOTE and NON_TRANSFERABLE are terminal for this content
#: hash; only new evidence with a different hash reopens the question.
NOTE = "NOTE"
QUEUED = "QUEUED"
SEALED = "SEALED"
NON_TRANSFERABLE = "NON_TRANSFERABLE"
TRANSFERRED = "TRANSFERRED"


@dataclass(frozen=True)
class Finding:
    """One thing the quant desk learned, as it arrives."""
    statement: str
    #: Where it came from — a module, a run id, a canonical note. Needed so a
    #: finding can be argued with rather than merely believed.
    source: str
    grade: str
    #: What it was measured ON. A finding from a CADJPY sweep is evidence about
    #: CADJPY, and this field is what makes that visible instead of implicit.
    measured_on: str
    #: What would have to be true on Aurum's data for it to transfer. A finding
    #: with no stated test is a note by construction, and `queue()` says so.
    transfer_test: str = ""
    observed_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    meta: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        """Hash of the CLAIM and what it was measured on.

        Excludes the grade and the timestamp: re-grading a claim, or seeing it
        again later, does not make it a different claim. Including them would
        let the same idea back in every time somebody upgraded its evidence
        label, which is precisely the loop this is meant to close.
        """
        return hashlib.sha256(
            json.dumps({"s": self.statement.strip().lower(),
                        "m": self.measured_on.strip().lower()},
                       sort_keys=True).encode()).hexdigest()[:16]

    @property
    def grade_rank(self) -> int:
        return int(self.grade[1]) if self.grade[:1] == "E" and self.grade[1:].isdigit() else -1

    def render(self) -> str:
        return (f"{self.statement}\n"
                f"    grade {self.grade} ({GRADES.get(self.grade, 'ungraded')}), "
                f"measured on {self.measured_on}, from {self.source}")


@dataclass
class Absorbed:
    """A finding after Aurum has done something about it."""
    finding: Finding
    status: str
    reason: str
    hypothesis_id: Optional[str] = None
    decided_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def render(self) -> str:
        h = f"  -> hypothesis {self.hypothesis_id}" if self.hypothesis_id else ""
        return f"[{self.status}] {self.finding.statement[:70]}\n    {self.reason}{h}"


@dataclass
class Absorber:
    """The channel. Remembers what it has already decided about."""
    decisions: dict = field(default_factory=dict)     # content_hash -> Absorbed

    # -- intake --------------------------------------------------------
    def already_decided(self, f: Finding) -> Optional[Absorbed]:
        return self.decisions.get(f.content_hash())

    def queue(self, f: Finding) -> Absorbed:
        """Decide what to do with one finding. Never promotes anything.

        The most common outcomes are NOTE and "already decided", and both are
        successes: the first says this is not actionable, the second says the
        desk remembers trying.
        """
        prior = self.already_decided(f)
        if prior is not None:
            return Absorbed(
                f, prior.status,
                f"already decided on {prior.decided_utc[:10]}: {prior.reason} "
                f"Re-absorbing the same claim is how a loop with no memory "
                f"re-runs its own failures forever.",
                prior.hypothesis_id, prior.decided_utc)

        if f.grade_rank < 0:
            out = Absorbed(f, NOTE, f"grade {f.grade!r} is not on the E0-E5 "
                                    f"scale, so its weight is unknown; recorded, "
                                    f"not tested.")
        elif f.grade_rank < int(MIN_GRADE_TO_TEST[1]):
            out = Absorbed(f, NOTE,
                           f"grade {f.grade} ({GRADES.get(f.grade)}) is below "
                           f"{MIN_GRADE_TO_TEST}; not worth the cost of a transfer "
                           f"test. Recorded so it is not re-evaluated next cycle.")
        elif not f.transfer_test.strip():
            # THE CHECK THAT KEEPS THIS FROM BECOMING A FOLDER. A finding nobody
            # can state a test for cannot be absorbed, only filed.
            out = Absorbed(f, NOTE,
                           "no transfer test stated. A finding that names nothing "
                           "Aurum could measure to confirm or kill it is a note, "
                           "however good the evidence behind it elsewhere.")
        else:
            out = Absorbed(f, QUEUED,
                           f"grade {f.grade}, measured on {f.measured_on}, with a "
                           f"stated test. Enters as a hypothesis at ZERO authority "
                           f"— an E5 external finding is still only a hypothesis "
                           f"here, and a result from {f.measured_on} is evidence "
                           f"about {f.measured_on}.")
        self.decisions[f.content_hash()] = out
        return out

    # -- the sealing ---------------------------------------------------
    def seal(self, f: Finding, hypothesis_id: str) -> Absorbed:
        """Register a queued finding as a sealed hypothesis in Aurum's registry.

        Deliberately takes the id rather than minting one: the hypothesis book
        owns hypothesis identity, and a second module inventing ids is how two
        registries drift apart.
        """
        cur = self.decisions.get(f.content_hash())
        if cur is None or cur.status != QUEUED:
            raise ValueError(
                f"cannot seal a finding that is {cur.status if cur else 'unqueued'}. "
                f"Only QUEUED findings become hypotheses; anything else is being "
                f"promoted past the check that queued it.")
        out = Absorbed(f, SEALED,
                       "sealed into Aurum's registry at zero authority. It may "
                       "not refuse a single trade until it clears the promotion "
                       "gate on Aurum's OWN forward evidence.", hypothesis_id)
        self.decisions[f.content_hash()] = out
        return out

    # -- the verdict ---------------------------------------------------
    def record_result(self, f: Finding, transferred: bool, evidence: str) -> Absorbed:
        """The half that makes the channel compound.

        A failure is recorded as loudly as a success and its hash is remembered,
        so the same idea is not re-absorbed next cycle by a process with no
        memory of having tried it. A loop that only records successes does not
        get smarter — it gets more confident.
        """
        if transferred:
            out = Absorbed(f, TRANSFERRED,
                           f"replicated on Aurum's own data: {evidence}. It is "
                           f"now Aurum's finding, held to Aurum's gate like any "
                           f"other.",
                           self.decisions.get(f.content_hash(), Absorbed(f, "", "")).hypothesis_id)
        else:
            out = Absorbed(f, NON_TRANSFERABLE,
                           f"did NOT replicate on Aurum's data: {evidence}. That "
                           f"is a fact about {f.measured_on}, not about gold as "
                           f"Aurum trades it. Recorded permanently so it is not "
                           f"tried again.")
        self.decisions[f.content_hash()] = out
        return out

    # -- reporting -----------------------------------------------------
    def by_status(self) -> dict:
        out: dict = {}
        for a in self.decisions.values():
            out.setdefault(a.status, []).append(a)
        return out

    def report(self) -> str:
        s = self.by_status()
        counts = {k: len(v) for k, v in sorted(s.items())}
        lines = [f"ABSORPTION  ({ABSORB_VERSION})",
                 f"  findings seen        {len(self.decisions)}",
                 f"  by status            {counts}"]
        transferred = len(s.get(TRANSFERRED, ()))
        failed = len(s.get(NON_TRANSFERABLE, ()))
        tested = transferred + failed
        if tested:
            lines.append(f"  transfer rate        {transferred}/{tested} "
                         f"({transferred / tested:.0%})")
            lines.append("    A low rate is the honest outcome and the useful one: "
                         "most findings from another universe are about that "
                         "universe.")
        else:
            lines.append("  transfer rate        nothing tested yet — so nothing "
                         "has been absorbed, only queued.")
        if s.get(NON_TRANSFERABLE):
            lines.append("")
            lines.append("  what did NOT transfer (and will not be retried):")
            lines += [f"    - {a.finding.statement[:64]}"
                      for a in s[NON_TRANSFERABLE][:8]]
        return "\n".join(lines)

    # -- persistence ---------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({
            "version": ABSORB_VERSION,
            "decisions": [{"hash": h, "status": a.status, "reason": a.reason,
                           "hypothesis_id": a.hypothesis_id,
                           "decided_utc": a.decided_utc,
                           "finding": {**a.finding.__dict__}}
                          for h, a in self.decisions.items()]}, indent=1, default=str)

    def save(self, path: Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(self.to_json())
        tmp.replace(p)

    @staticmethod
    def load(path: Path) -> "Absorber":
        p = Path(path)
        if not p.exists():
            return Absorber()
        d = json.loads(p.read_text(encoding='utf-8'))
        ab = Absorber()
        for row in d.get("decisions", ()):
            f = Finding(**row["finding"])
            ab.decisions[row["hash"]] = Absorbed(
                f, row["status"], row["reason"], row.get("hypothesis_id"),
                row.get("decided_utc", ""))
        return ab


# ------------------------------------------------------ was it worth absorbing?

def absorption_value(before: Sequence[float], after: Sequence[float],
                     min_n: int = 30) -> dict:
    """Did absorbing anything actually make Aurum better?

    Paired on nothing, deliberately — these are different periods and cannot be
    paired, which is exactly why the answer is usually "cannot tell". Reported
    as such rather than as a difference of means dressed up as a result.
    """
    b = [x for x in before if x == x]
    a = [x for x in after if x == x]
    if len(b) < min_n or len(a) < min_n:
        return {"verdict": "INSUFFICIENT",
                "why": (f"{len(b)} before and {len(a)} after; {min_n} each "
                        f"required. No claim either way.")}
    mb, ma = sum(b) / len(b), sum(a) / len(a)
    return {
        "verdict": "OBSERVED",
        "mean_before": mb, "mean_after": ma, "delta": ma - mb,
        "why": ("These are SEQUENTIAL periods, not paired states, so the "
                "difference includes whatever the market did between them. It is "
                "an observation about two windows and not an estimate of what "
                "absorption was worth. For that, run the specialist marginal-value "
                "test on states both versions decided."),
    }
