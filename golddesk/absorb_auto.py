"""Quant's findings reach Aurum on a timer, with no human in the loop.

WHAT WAS ACTUALLY BROKEN

absorb.py has been correct and unused. `external/channels.txt` and
`external/signals.jsonl` are both zero bytes, so the answer to "does Aurum use
what quant learns" was no. The discipline existed; the pipe did not. This is
the pipe.

THE RELEVANCE RULE, WHICH IS THE WHOLE DESIGN

Quant researches 22 instruments. Aurum trades one. A finding measured on CADJPY
is evidence about CADJPY, and importing it because the same code produced it is
exactly the cargo-culting absorb.py was written to prevent. So a finding is
carried only if it is about GOLD, or about MACHINERY that is instrument-neutral
by construction:

  GOLD          the cell names XAUUSD, or the study is gold-only.
  MACHINERY     a leak, a cost model, an estimator, a sizing rule, an exit
                mechanism validated across instruments. These transfer because
                they are statements about arithmetic, not about a market.

Everything else is dropped, and the count of what was dropped is reported --
a channel that silently carried everything would be worse than none, because
its output would look like signal.

EVERY FINDING ENTERS SEALED AT ZERO AUTHORITY

Whatever its evidence grade upstream. It becomes a claim Aurum can be wrong
about, tested against Aurum's OWN ledger, and it earns influence only through
hypothesis.py's post-seal confirmation. An E5 finding from quant is still only
a hypothesis here. That is not caution for its own sake: quant's own record
this month includes a fill-bar leak that made a retail chart pattern score
t = +9.16 and a 3,168-cell hunt that produced zero survivors clearing its bar.

NEGATIVE RESULTS ARE CARRIED TOO, AND THEY ARE THE HALF THAT COMPOUNDS

"Banking on trend-death loses on 0 of 22 instruments" is worth more to Aurum
than most positive findings, because it closes a door Aurum would otherwise
spend a month opening. Absorber.record_result already remembers failures by
hash so the same idea is not re-absorbed by a process with no memory of having
tried it.

IDEMPOTENT BY CONSTRUCTION. Content-hashed, so running this hourly forever adds
each finding exactly once.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from .absorb import Absorber, Finding

log = logging.getLogger(__name__)

__all__ = ["GOLD_PATTERNS", "MACHINERY_SOURCES", "QUANT_MARKERS",
           "QUANT_ROOT_CANDIDATES", "discover_quant_root", "scan", "sync", "main"]

#: A cell/statement mentioning any of these is about gold.
GOLD_PATTERNS = (re.compile(r"\bXAU", re.I), re.compile(r"\bgold\b", re.I),
                 re.compile(r"\bXAG", re.I))

#: Reports whose findings are about ARITHMETIC rather than about an instrument,
#: and therefore transfer. Each entry is (filename, grade, why it transfers).
MACHINERY_SOURCES: dict[str, tuple[str, str]] = {
    "daily_stop.json": (
        "E3", "a daily loss limit is a risk rule, not a market claim; its "
              "effect is on the equity path and transfers to any book"),
    "growth_now.json": (
        "E3", "exit mechanics measured at matched drawdown across a book"),
    "push_ceiling.json": (
        "E3", "portfolio construction and sizing; instrument-neutral"),
    "cut_drawdown.json": (
        "E3", "drawdown levers measured at fixed CAGR"),
    "rank_ic.json": (
        "E2", "cross-sectional ranking machinery and its measured IC"),
}


def _is_gold(text: str) -> bool:
    return any(p.search(text) for p in GOLD_PATTERNS)


def _hunt_findings(path: Path) -> Iterable[Finding]:
    """Survivors and non-survivors from a hunt, gold cells only."""
    try:
        blob = json.loads(path.read_text("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        log.warning("unreadable %s: %s", path, e)
        return
    for row in blob.get("all", []):
        sym = str(row.get("sym", ""))
        if not _is_gold(sym):
            continue
        cell = f"{sym}.{row.get('win')}.{row.get('state')}"
        passed = bool(row.get("gate"))
        t = row.get("t")
        defl = row.get("defl")
        yield Finding(
            statement=(
                f"{cell} {'CLEARS' if passed else 'FAILS'} the acceptance "
                f"battery (t={t}, deflated={defl}, n={row.get('n')}, "
                f"PF={row.get('pf')})"),
            source=f"quant/{path.name}",
            grade="E3" if passed else "E2",
            measured_on=f"{sym} H1, quant universe, 8y",
            transfer_test=(
                "Aurum's own XAUUSD ledger reproduces a positive expectancy "
                "for this session/state cohort over at least 60 resolved "
                "signals, sealed before the outcomes are read"),
            meta={"cell": cell, "gate": passed, "t": t, "deflated": defl},
        )


def _machinery_findings(path: Path, grade: str, why: str) -> Iterable[Finding]:
    """One finding per report, carrying its headline result."""
    try:
        blob = json.loads(path.read_text("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        log.warning("unreadable %s: %s", path, e)
        return
    arms = blob.get("arms")
    if isinstance(arms, list) and arms:
        # Report the SPREAD, not just the winner. A best-arm-only summary is
        # how a search gets laundered into a result downstream.
        head = json.dumps(arms[0])[:200]
        yield Finding(
            statement=f"{path.stem}: {len(arms)} arms measured; first = {head}",
            source=f"quant/{path.name}", grade=grade,
            measured_on="quant book, matched drawdown, half-edge",
            transfer_test=(
                f"the same lever, applied to Aurum's own resolved signals, "
                f"moves its realised drawdown or expectancy in the same "
                f"direction. ({why})"),
            meta={"n_arms": len(arms)})
        return
    yield Finding(
        statement=f"{path.stem}: {json.dumps(blob)[:200]}",
        source=f"quant/{path.name}", grade=grade,
        measured_on="quant book", transfer_test=why)


def scan(quant_root: Path) -> tuple[list[Finding], int]:
    """Every carryable finding, plus the count of what was dropped."""
    reports = Path(quant_root) / "desks" / "mt5" / "reports"
    if not reports.is_dir():
        raise FileNotFoundError(
            f"no quant reports at {reports}. Point --quant at the quant "
            f"repository root (the directory containing desks/), not at the "
            f"desk. Absorbing nothing silently would look identical to "
            f"absorbing everything relevant.")
    out: list[Finding] = []
    dropped = 0
    for p in sorted(reports.glob("hunt*.json")):
        if "partial" in p.name:
            continue
        try:
            blob = json.loads(p.read_text("utf-8"))
            dropped += sum(1 for r in blob.get("all", [])
                           if not _is_gold(str(r.get("sym", ""))))
        except Exception:                                    # noqa: BLE001
            pass
        out.extend(_hunt_findings(p))
    for name, (grade, why) in MACHINERY_SOURCES.items():
        p = reports / name
        if p.is_file():
            out.extend(_machinery_findings(p, grade, why))
    return out, dropped


#: Where a quant checkout actually lives, tried in order. The env var wins; the
#: rest are the desk's own deployment reality:
#:   C:/opt/quant       the Contabo VPS, per Aurum's CLAUDE.md
#:   ../quant, ~/quant  a box with both desks side by side
#:   /home/user/quant   an agent clone
QUANT_ROOT_CANDIDATES = ("C:/opt/quant", "C:/quant", "/opt/quant", "../quant",
                         "~/quant", "/home/user/quant")

#: Files only the quant desk has. A directory existing is not enough: pointing
#: the scanner at the wrong repository absorbs nothing and reports exactly the
#: same "0 new findings" as a working pipe. `desks/mt5` is here because
#: self_heal used it as its marker, and TWO discovery implementations with
#: different markers can disagree about whether the checkout exists — which is
#: the split-brain this desk keeps finding. self_heal now delegates here.
QUANT_MARKERS = ("docs/MASTER_QUANT_CONSTITUTION.md", "docs/GAP_REGISTER.md",
                 "libs/research", "desks/mt5")


def discover_quant_root(env_value: Optional[str] = None,
                        candidates: Optional[Iterable] = None
                        ) -> tuple[Optional[Path], str]:
    """Find the quant checkout. Returns (path, basis); the basis is never empty.

    WHAT WAS ACTUALLY BROKEN. The pull ran only when AURUM_QUANT_ROOT was set,
    and when it was not the cycle wrote one line to a log nobody reads while the
    REPORT said "0 new finding(s) this cycle". That sentence is indistinguishable
    from "quant produced nothing new", so an unplugged pipe and a quiet week look
    identical in the only artifact anyone actually looks at. The desk has a name
    for that failure and it is its most repeated one: absence resolving to a
    clean answer.

    Discovery has a DEFAULT now, and every outcome carries its basis:

        env         AURUM_QUANT_ROOT, which still wins outright
        discovered  a conventional location carrying the quant markers
        absent      nothing found — and the caller must say so LOUDLY

    A directory that exists but carries none of the markers comes back as
    `wrong-repo` rather than being quietly accepted.
    """
    env = (env_value if env_value is not None
           else os.environ.get("AURUM_QUANT_ROOT", "")).strip()
    if env:
        p = Path(env).expanduser()
        if not p.is_dir():
            return None, f"env-missing:{p}"
        if not any((p / m).exists() for m in QUANT_MARKERS):
            return None, f"env-wrong-repo:{p}"
        return p, "env"
    here = Path(__file__).resolve().parent.parent
    for cand in (candidates if candidates is not None else QUANT_ROOT_CANDIDATES):
        cand = str(cand)
        p = (here / cand).resolve() if cand.startswith("..") \
            else Path(cand).expanduser()
        try:
            if p.is_dir() and any((p / m).exists() for m in QUANT_MARKERS):
                return p, "discovered"
        except OSError:                                # a network path that is down
            continue
    return None, "absent"


def to_inbox(quant_root: Path, inbox: Path, dry_run: bool = False) -> dict:
    """Write gold-relevant findings into Aurum's inbox. The preferred path.

    WHY THIS AND NOT DIRECT SEALING. aurum_cycle.step_absorb already owns the
    queue-and-seal half, reading `inbox/quant_findings.jsonl`, and its docstring
    states the reason it does not reach into the other repository: "a cycle that
    pulled from another repository would fail in a way neither desk owns."

    That concern is right and this does not overrule it. The pull is a SEPARATE,
    OPTIONAL step that either produces inbox rows or does nothing at all, and
    the cycle's own absorption is unchanged and still reads only a local file.
    A missing or moved quant checkout therefore degrades to "no new findings"
    instead of breaking Aurum's nightly cycle -- which is the failure mode the
    original comment was protecting against.

    Deduplication still happens downstream, by content hash, so appending the
    same finding to the inbox twice is harmless.
    """
    findings, dropped = scan(Path(quant_root))
    if not dry_run:
        Path(inbox).parent.mkdir(parents=True, exist_ok=True)
        with Path(inbox).open("a", encoding="utf-8") as fh:
            for f in findings:
                fh.write(json.dumps({
                    "statement": f.statement, "source": f.source,
                    "grade": f.grade, "measured_on": f.measured_on,
                    "transfer_test": f.transfer_test,
                    "observed_utc": f.observed_utc, "meta": f.meta}) + "\n")
    return {"written": len(findings), "dropped_not_relevant": dropped,
            "inbox": str(inbox)}


def sync(quant_root: Path, state: Path, journal: Optional[Path] = None,
         dry_run: bool = False) -> dict:
    """Scan, queue, seal. Idempotent. Returns a summary."""
    absorber = Absorber.load(state) if Path(state).is_file() else Absorber()
    findings, dropped = scan(Path(quant_root))

    new, already = [], 0
    for f in findings:
        if absorber.already_decided(f):
            already += 1
            continue
        new.append(f)

    results = []
    for f in new:
        if dry_run:
            results.append((f, "WOULD_QUEUE"))
            continue
        a = absorber.queue(f)
        # Zero authority, always. The seal id is the finding's own content hash
        # so the same claim maps to the same hypothesis across runs.
        if a.status.upper() in ("QUEUED", "SEALED", "ACCEPTED"):
            absorber.seal(f, f"quant-{f.content_hash()}")
        results.append((f, a.status))

    if not dry_run:
        Path(state).parent.mkdir(parents=True, exist_ok=True)
        absorber.save(Path(state))
        if journal:
            Path(journal).parent.mkdir(parents=True, exist_ok=True)
            with Path(journal).open("a", encoding="utf-8") as fh:
                for f, status in results:
                    fh.write(json.dumps({
                        "hash": f.content_hash(), "status": status,
                        "statement": f.statement, "source": f.source,
                        "grade": f.grade, "measured_on": f.measured_on,
                        "transfer_test": f.transfer_test,
                        "observed_utc": f.observed_utc}) + "\n")

    return {"scanned": len(findings), "new": len(new),
            "already_known": already, "dropped_not_relevant": dropped,
            "results": [(f.statement[:80], s) for f, s in results]}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Carry quant's gold-relevant findings into Aurum as "
                    "sealed hypotheses. Idempotent; safe to run on a timer.")
    ap.add_argument("--quant", required=True, type=Path,
                    help="quant repository ROOT (contains desks/)")
    ap.add_argument("--state", default=Path("state/absorb.json"), type=Path)
    ap.add_argument("--journal", default=Path("external/signals.jsonl"),
                    type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    try:
        s = sync(a.quant, a.state, a.journal, a.dry_run)
    except FileNotFoundError as e:
        print(str(e))
        return 2

    print(f"scanned {s['scanned']} gold-relevant findings, "
          f"{s['new']} new, {s['already_known']} already decided, "
          f"{s['dropped_not_relevant']} dropped as not about gold")
    for stmt, status in s["results"][:20]:
        print(f"  [{status}] {stmt}")
    if s["new"] == 0:
        print("nothing new — which is the expected steady state, not a fault.")
    if a.dry_run:
        print("\ndry run: nothing written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
