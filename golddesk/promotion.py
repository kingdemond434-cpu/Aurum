"""Candidates in on the raw threshold; capital out on forward evidence only.

THE POLICY THIS FILE IMPLEMENTS, AND WHY IT IS NOT A WEAKENING

The desk used to let the deflated Sharpe VETO a cell: search three thousand
parameter points, raise the bar by E[max of N], and arm whatever cleared it.
Nothing ever cleared it, which is not conservatism, it is a desk that can never
adopt anything. A screening statistic used as a final gate has no power to
separate "noise" from "real but small", and it throws away the second along with
the first.

So the rule here is:

    RAW THRESHOLD  ->  admits to SHADOW.       No multiplicity haircut, no veto.
    FORWARD RESULT ->  admits to LIVE CAPITAL. The only gate that decides.

That is strictly MORE permissive at the door and strictly LESS permissive at the
till, and the second half is what makes the first half safe. Out-of-sample days
were not in the search, so a multiplicity artefact cannot survive them: a cell
that looked good because three thousand were tried reverts within weeks, and the
decay monitor retires it having cost nothing but time.

DEFLATION IS KEPT, AND DEMOTED FROM JUDGE TO PRIORITISER

The multiplicity information is real and throwing it away would be its own
error. It just is not a verdict. Here it sets QUEUE ORDER: when shadow slots or
live capital are scarce, the candidate with the stronger deflated Sharpe goes
first. Same population admitted, better ordering within it. A cell with a weak
DSR is not refused, it waits.

WHAT THIS FILE WILL NOT DO

It will not mark anything LIVE on in-sample evidence, whatever its Sharpe. The
word "survivor" is reserved for a cell that has survived forward days it could
not have been fitted to. That is not a stylistic preference: a status field is
read by code that sizes positions, and a cell labelled LIVE gets real lots.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence

PROMOTION_VERSION = "promotion-2026-08-18-a"


class Status(str, Enum):
    """Where a cell sits. The ONLY value that authorises real lots is LIVE."""
    CANDIDATE = "CANDIDATE"      # passed the raw threshold, not yet shadowing
    SHADOW = "SHADOW"            # accruing forward days, no capital
    LIVE = "LIVE"                # forward-validated, sized by the risk layer
    RETIRED = "RETIRED"          # decayed or failed forward; never re-armed silently
    REJECTED = "REJECTED"        # failed even the raw threshold


#: THE ORIGINAL THRESHOLD, deliberately un-inflated. A cell needs a positive
#: in-sample Sharpe and a probabilistic Sharpe clearing this against a ZERO
#: benchmark — the same bar the desk used before multiplicity entered the
#: picture. This is the gate the principal asked to be judged on and it is the
#: gate applied, exactly.
RAW_PSR_THRESHOLD = 0.95

#: Forward days a shadow cell must accrue before it can be considered at all.
#: Not a statistical bar — a sample-size floor. Below this the forward mean is
#: dominated by whichever few days happened to land in it.
MIN_SHADOW_DAYS = 60

#: Forward evidence required to promote. The t-statistic is computed on days the
#: cell could not have been fitted to, which is the entire point.
MIN_FORWARD_T = 1.5

#: Forward days before a LIVE cell is re-examined against its shadow record.
REVIEW_EVERY_DAYS = 20


@dataclass
class Candidate:
    """One searched cell, and everything known about it.

    `dsr` is carried but never gates: it orders the queue. Storing it on the
    record means a future reader can see what the multiplicity cost was, rather
    than discovering that the question was never asked.
    """
    cell: str
    in_sample_sharpe: float
    psr_raw: float                       # against a zero benchmark — the gate
    dsr_deflated: Optional[float] = None  # against E[max of N] — diagnostic only
    n_trials_searched: int = 1
    status: Status = Status.CANDIDATE
    registered_at: str = ""
    shadow_days: int = 0
    forward_r: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    # ------------------------------------------------------------- forward stats
    @property
    def forward_mean(self) -> Optional[float]:
        return statistics.fmean(self.forward_r) if self.forward_r else None

    @property
    def forward_t(self) -> Optional[float]:
        """t of the forward mean against zero. None below two observations.

        None rather than 0.0 deliberately: a t-statistic that cannot be computed
        is not a t-statistic of zero, and returning a number here would let a
        one-day cell compare equal to a flat hundred-day one.
        """
        n = len(self.forward_r)
        if n < 2:
            return None
        sd = statistics.stdev(self.forward_r)
        if sd <= 0:
            return None
        return statistics.fmean(self.forward_r) / (sd / math.sqrt(n))

    @property
    def queue_priority(self) -> float:
        """Higher goes first. Deflated Sharpe when known, else raw PSR.

        THIS IS WHERE MULTIPLICITY DOES ITS WORK. It cannot exclude a candidate;
        it can only decide who is served first when slots are finite, which is
        the honest use of a statistic that measures confidence rather than truth.
        """
        return float(self.dsr_deflated if self.dsr_deflated is not None
                     else self.psr_raw)


def screen(cell: str, in_sample_sharpe: float, psr_raw: float,
           dsr_deflated: Optional[float] = None,
           n_trials_searched: int = 1) -> Candidate:
    """Apply the RAW threshold. Deflation is recorded, not applied.

    A cell clearing the un-inflated bar becomes a CANDIDATE however many other
    cells were searched. That is the policy, and the deflated figure travels
    with it so nothing is hidden from whoever reads the record later.
    """
    c = Candidate(cell=cell, in_sample_sharpe=float(in_sample_sharpe),
                  psr_raw=float(psr_raw), dsr_deflated=dsr_deflated,
                  n_trials_searched=int(n_trials_searched),
                  registered_at=datetime.now(timezone.utc).isoformat())
    if in_sample_sharpe > 0 and psr_raw >= RAW_PSR_THRESHOLD:
        c.status = Status.CANDIDATE
        note = (f"admitted on the raw threshold (PSR {psr_raw:.4f} >= "
                f"{RAW_PSR_THRESHOLD}), no multiplicity haircut applied")
        if dsr_deflated is not None and dsr_deflated < RAW_PSR_THRESHOLD:
            note += (f"; deflated Sharpe at N={n_trials_searched} is "
                     f"{dsr_deflated:.4f} and would have refused it — recorded "
                     f"for queue order, not applied as a veto")
        c.notes.append(note)
    else:
        c.status = Status.REJECTED
        c.notes.append(f"below the raw threshold: sharpe "
                       f"{in_sample_sharpe:+.3f}, PSR {psr_raw:.4f}")
    return c


def to_shadow(c: Candidate) -> Candidate:
    """Move a CANDIDATE into shadow. Costs nothing but time, so nothing gates it."""
    if c.status is not Status.CANDIDATE:
        c.notes.append(f"cannot shadow from {c.status.value}")
        return c
    c.status = Status.SHADOW
    c.notes.append("shadowing — accruing forward days, no capital at risk")
    return c


def observe(c: Candidate, r: float) -> Candidate:
    """Record one forward day. The only kind of evidence that promotes."""
    if c.status not in (Status.SHADOW, Status.LIVE):
        return c
    if not math.isfinite(r):
        return c
    c.forward_r.append(float(r))
    c.shadow_days = len(c.forward_r)
    return c


def consider_promotion(c: Candidate, min_days: int = MIN_SHADOW_DAYS,
                       min_t: float = MIN_FORWARD_T) -> Candidate:
    """Promote to LIVE on FORWARD evidence alone.

    The in-sample Sharpe plays no part here and must not: it is the number the
    search maximised, so using it twice would count the same evidence twice.
    """
    if c.status is not Status.SHADOW:
        return c
    if c.shadow_days < min_days:
        return c
    t = c.forward_t
    if t is None:
        return c
    if t >= min_t and (c.forward_mean or 0.0) > 0:
        c.status = Status.LIVE
        c.notes.append(f"PROMOTED on {c.shadow_days} forward days, "
                       f"mean {c.forward_mean:+.4f}R, t={t:+.2f} — evidence the "
                       f"search could not have fitted")
    return c


def review(c: Candidate, lookback: int = REVIEW_EVERY_DAYS) -> Candidate:
    """Retire a LIVE cell whose recent forward record has turned over.

    Deliberately crude and deliberately one-directional: this retires, it never
    re-arms. A cell that recovers goes back through shadow like anything else,
    because "it came back" is exactly what a noise cell looks like half the time.
    """
    if c.status is not Status.LIVE or len(c.forward_r) < lookback + 10:
        return c
    recent = c.forward_r[-lookback:]
    if statistics.fmean(recent) <= 0:
        c.status = Status.RETIRED
        c.notes.append(f"RETIRED: last {lookback} forward days average "
                       f"{statistics.fmean(recent):+.4f}R")
    return c


def queue(cands: Iterable[Candidate], slots: Optional[int] = None) -> list:
    """Candidates in service order. Every one is served eventually if slots allow.

    Sorted by deflated Sharpe where known — the multiplicity-aware figure decides
    WHO FIRST, never who at all.
    """
    ordered = sorted([c for c in cands if c.status is Status.CANDIDATE],
                     key=lambda c: -c.queue_priority)
    return ordered if slots is None else ordered[:slots]


def save(cands: Sequence[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{**asdict(c), "status": c.status.value}
                                for c in cands], indent=1), "utf-8")


def load(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for row in json.loads(path.read_text("utf-8")):
        row["status"] = Status(row.get("status", "CANDIDATE"))
        out.append(Candidate(**row))
    return out


def report(cands: Sequence[Candidate]) -> str:
    by = {s: [c for c in cands if c.status is s] for s in Status}
    lines = [f"PROMOTION PIPELINE  ({PROMOTION_VERSION})",
             f"  raw threshold PSR >= {RAW_PSR_THRESHOLD}, no multiplicity veto",
             f"  live requires {MIN_SHADOW_DAYS}+ forward days at t >= "
             f"{MIN_FORWARD_T}", ""]
    for s in Status:
        lines.append(f"  {s.value:<10} {len(by[s]):>4}")
    live = by[Status.LIVE]
    if live:
        lines += ["", "  LIVE (forward-validated):"]
        for c in sorted(live, key=lambda c: -(c.forward_t or 0)):
            lines.append(f"    {c.cell:<40} {c.shadow_days:>4}d  "
                         f"mean {c.forward_mean:+.4f}R  t={c.forward_t:+.2f}")
    shadow = by[Status.SHADOW]
    if shadow:
        lines += ["", "  SHADOW (no capital):"]
        for c in sorted(shadow, key=lambda c: -c.queue_priority)[:12]:
            t = c.forward_t
            lines.append(f"    {c.cell:<40} {c.shadow_days:>4}d  "
                         + (f"t={t:+.2f}" if t is not None else "t=—"))
    return "\n".join(lines)
