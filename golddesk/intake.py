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

#: Symbols this desk is allowed to carry. Aurum is a GOLD desk: the whole thesis
#: is that concentrating every hour of study on one market compounds, and a book
#: led by `NZDJPY|monday_gap` and `EURCHF|monday_gap` is not that desk — it is a
#: generic cell-search wearing Aurum's name. Those two FX crosses reached the top
#: of the shadow book on in-sample Sharpe ~2.9 selected from 3,168 trials with
#: `dsr_deflated: null`, which is what selection looks like, not what edge looks
#: like, and it cost slots and attention that belong to gold.
#:
#: Matching is on the symbol prefix so GC, MGC and any XAU quote all pass.
GOLD_SYMBOLS = ("XAU", "GC", "MGC", "GOLD")


def is_gold(cell: str) -> bool:
    """True if the cell's symbol is a gold instrument.

    The symbol is everything before the first `|`. A cell with no `|` cannot be
    parsed into a strategy at all, so it is not gold and not anything else.
    """
    symbol = cell.partition("|")[0].strip().upper()
    if not symbol or "|" not in cell:
        return False
    return any(symbol.startswith(g) for g in GOLD_SYMBOLS)


def read_sources(paths: Sequence[Path] = DEFAULT_SOURCES,
                 gold_only: bool = True) -> list:
    """Every candidate list that exists. Missing files are not an error.

    A sweep that has not run yet is the normal state on a fresh install, and
    treating it as a failure would make the daily cycle red for no reason.

    `gold_only` drops non-gold cells at the door rather than registering and
    then ignoring them. Registering is not free: a cell in the book occupies a
    shadow slot, accrues forward days, and — the part that actually costs
    something — counts as a concurrent test in the book-level promotion
    correction, so carrying 70 FX cells raises the bar every gold cell must
    clear. Filtering here makes the gold book EASIER to promote from, not harder.
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
            out.extend(r for r in rows if isinstance(r, dict) and r.get("cell")
                       and (not gold_only or is_gold(str(r["cell"]))))
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


def record_day(book: list, returns: dict, day: Optional[str] = None,
                trades: Optional[dict] = None) -> int:
    """Post one forward day per shadow/live cell, then re-evaluate every one.

    `day` is passed through to observe() because the marginal-growth gate needs
    to know which calendar days two sleeves shared. Omitting it does not break
    anything; it downgrades promotion to the significance test alone.

    `trades` carries how many FILLS produced each day's R. The promotion floor
    counts fills rather than rows, because one row is one day and a day can hold
    six fills or one — judging a slow sleeve on rows is how a calendar clock ends
    up executing it on three trades.

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
        observe(c, float(v), day=day,
                n_trades=int((trades or {}).get(c.cell, 1)))
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
        slots: Optional[int] = MAX_SHADOW,
        day: Optional[str] = None,
        trades: Optional[dict] = None,
        gold_only: bool = True) -> tuple:
    """One daily pass. Returns (book, summary text).

    `gold_only` is the charter enforced in code. It is a parameter rather than a
    constant so a migration or an audit can still load the full book, but the
    default is the desk's actual mandate.
    """
    book = load(book_path)
    # Filtering intake only stops NEW non-gold cells; the 77 already registered
    # would keep accruing days and keep inflating the concurrent-test count for
    # ever. Drop them from the book too. Nothing is lost that matters: none had
    # forward evidence, and a cell this desk will never trade cannot earn any.
    dropped_foreign = [c for c in book if not is_gold(c.cell)] if gold_only else []
    if dropped_foreign:
        book = [c for c in book if is_gold(c.cell)]
    before = len(book)
    rows = read_sources(sources, gold_only=gold_only)
    book, added, skipped, rejected = intake(rows, book)
    started = promote_queue(book, slots)
    posted = record_day(book, returns or {},
                        day=day or str(datetime.now(timezone.utc).date()),
                        trades=trades)
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
    ] + ([
        f"  dropped non-gold  {len(dropped_foreign)} cell(s) retired from the "
        f"book: {', '.join(sorted({c.cell.partition('|')[0] for c in dropped_foreign}))}"
        " — this is a gold desk, and every foreign cell raised the promotion bar"
        " for the gold ones by counting as a concurrent test",
    ] if dropped_foreign else []) + [
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
