"""Hunt output to shadow book, without a human in the loop.

A candidate list that needs someone to notice it is not a pipeline, it is a
document. This module is the automation between the two: it reads whatever the
last sweep wrote, screens every cell on the RAW threshold, registers the new
ones, and starts them accruing forward days. It runs from the daily cycle and
asks nothing.

WHAT IS AUTOMATED AND WHAT DELIBERATELY IS NOT

Admission is automated all the way to SHADOW, because shadow costs nothing but
time and requiring approval for a free action just means the queue never moves.
Promotion to LIVE is also automated -- but on FORWARD evidence, which is a fact
about the world rather than a judgement, so there is nothing for a human to add
except delay.

What is not automated is widening the risk budget. New sleeves change the heat
solve, and a pipeline that could both admit sleeves and enlarge the budget they
draw on could grow the book's total risk without anyone deciding to. So intake
reports the new budget and never applies it.

IDEMPOTENCE IS THE WHOLE GAME FOR A DAILY JOB

This runs every day against a file that mostly does not change. Re-registering
the same cell each morning would reset its shadow clock forever and nothing
would ever reach the promotion threshold -- a pipeline that looks busy and can
never finish. Cells are keyed by their identity string and a cell already in the
book is left strictly alone.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from golddesk.promotion import (Candidate, Status, load, observe,
                                promote_book, queue, review, save, screen,
                                to_shadow)

INTAKE_VERSION = "intake-2026-08-18-a"

#: Where the pipeline lives between runs.
DEFAULT_BOOK = Path("state/pipeline.json")

#: Sweeps drop candidate lists here. Each is a list of dicts carrying at least
#: `cell`, `in_sample_sharpe` and `psr_raw`.
DEFAULT_SOURCES = (
    Path("state/hunt_candidates.json"),
    Path("inbox/hunt_candidates.json"),
)

#: Shadow slots. Not a risk limit — shadow risks nothing — but a bound on how
#: many series the daily job carries. None means unbounded.
MAX_SHADOW: Optional[int] = None


def read_sources(paths: Sequence[Path] = DEFAULT_SOURCES) -> list:
    """Every candidate list that exists. Missing files are not an error.

    A sweep that has not run yet is the normal state on a fresh install, and
    treating it as a failure would make the daily cycle red for no reason.
    """
    out = []
    for p in paths:
        if not p.exists():
            continue
        try:
            rows = json.loads(p.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(rows, list):
            out.extend(r for r in rows if isinstance(r, dict) and r.get("cell"))
    return out


def intake(rows: Sequence[dict], book: list) -> tuple:
    """Screen and register. Returns (book, added, skipped, rejected).

    Cells already in the book are SKIPPED rather than re-screened. Re-screening
    would overwrite a shadow record with a fresh zero-day one every morning, and
    the pipeline would never promote anything while appearing to work.
    """
    known = {c.cell for c in book}
    added, skipped, rejected = [], [], []
    for r in rows:
        cell = str(r["cell"])
        if cell in known:
            skipped.append(cell)
            continue
        c = screen(cell,
                   in_sample_sharpe=float(r.get("in_sample_sharpe", 0.0)),
                   psr_raw=float(r.get("psr_raw", 0.0)),
                   dsr_deflated=(None if r.get("dsr_deflated") is None
                                 else float(r["dsr_deflated"])),
                   n_trials_searched=int(r.get("n_trials_searched", 1)))
        if c.status is Status.REJECTED:
            rejected.append(cell)
            continue
        book.append(c)
        known.add(cell)
        added.append(c)
    return book, added, skipped, rejected


def promote_queue(book: list, slots: Optional[int] = MAX_SHADOW) -> list:
    """Move CANDIDATEs into shadow, best deflated Sharpe first.

    The ordering is where multiplicity does its work: it decides who starts
    accruing days first when slots are finite, and never who is excluded.
    """
    started = []
    for c in queue(book, slots=slots):
        to_shadow(c)
        started.append(c)
    return started


def record_day(book: list, returns: dict) -> int:
    """Post one forward day per shadow/live cell, then re-evaluate every one.

    Cells absent from `returns` get NOTHING, not a zero. A day a sleeve did not
    trade is not a day it broke even, and writing zeros would dilute the forward
    t-statistic toward zero — making a real edge look flat and a flat one look
    certain.
    """
    n = 0
    for c in book:
        if c.status not in (Status.SHADOW, Status.LIVE):
            continue
        v = returns.get(c.cell)
        if v is None:
            continue
        observe(c, float(v))
        n += 1
    # BOOK-LEVEL, not cell-by-cell. Promoting each cell against a fixed t
    # ignores that the forward gate is a multiple test across everything
    # shadowing at once, which is how a noise cell reached LIVE at t=+2.14 in
    # this module's own test. promote_book corrects for the concurrent set.
    promote_book(book)
    for c in book:
        review(c)
    return n


def run(book_path: Path = DEFAULT_BOOK,
        sources: Sequence[Path] = DEFAULT_SOURCES,
        returns: Optional[dict] = None,
        slots: Optional[int] = MAX_SHADOW) -> tuple:
    """One daily pass. Returns (book, summary text)."""
    book = load(book_path)
    before = len(book)
    rows = read_sources(sources)
    book, added, skipped, rejected = intake(rows, book)
    started = promote_queue(book, slots)
    posted = record_day(book, returns or {})
    save(book, book_path)

    by = {s: sum(1 for c in book if c.status is s) for s in Status}
    lines = [
        f"INTAKE  ({INTAKE_VERSION})  {datetime.now(timezone.utc).date()}",
        f"  sources           {len(rows)} candidate rows from "
        f"{sum(1 for p in sources if p.exists())} file(s)",
        f"  registered        {len(added)} new, {len(skipped)} already known, "
        f"{len(rejected)} below the raw threshold",
        f"  moved to shadow   {len(started)}",
        f"  forward days      {posted} posted",
        f"  book              {before} -> {len(book)}",
        "",
        "  " + "  ".join(f"{s.value} {by[s]}" for s in Status),
    ]
    live = [c for c in book if c.status is Status.LIVE]
    if live:
        lines += ["", "  LIVE — promoted on forward evidence:"]
        for c in sorted(live, key=lambda c: -(c.forward_t or 0.0)):
            lines.append(f"    {c.cell:<44}{c.shadow_days:>4}d  "
                         f"t={c.forward_t:+.2f}")
    waiting = [c for c in book if c.status is Status.SHADOW]
    if waiting:
        near = sorted(waiting, key=lambda c: -c.shadow_days)[:5]
        lines += ["", "  SHADOW — longest-running:"]
        for c in near:
            t = c.forward_t
            lines.append(f"    {c.cell:<44}{c.shadow_days:>4}d  "
                         + (f"t={t:+.2f}" if t is not None else "t=—"))
    if not live:
        lines += ["",
                  "  NOTHING IS LIVE. Candidates are in-sample screens; only "
                  "forward days",
                  "  promote, and none has accrued enough yet. That is the "
                  "pipeline working."]
    return book, "\n".join(lines)


def budget_note(book: list, daily_returns: Sequence[float],
                tolerance: float = 0.35) -> str:
    """What the heat solve WOULD return with the current live set. Never applied.

    Reported rather than applied on purpose: a pipeline that can both admit
    sleeves and enlarge the budget those sleeves draw on can raise the book's
    total risk with nobody deciding to. Admission is automatic; spending more is
    not.
    """
    from golddesk.growth import solve_heat
    live = [c for c in book if c.status is Status.LIVE]
    heat, why = solve_heat(daily_returns, tolerance=tolerance)
    if heat <= 0:
        return f"  heat: {why}"
    return (f"  heat WOULD solve to {heat:.2%} across {len(live)} live sleeve(s) "
            f"at a {tolerance:.0%} tolerance.\n  {why}\n"
            f"  NOT APPLIED — widening the risk budget stays a human decision.")
