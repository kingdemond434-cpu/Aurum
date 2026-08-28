"""Is the analyst any GOOD, not merely responding.

THE GAP THIS FILLS, AND THE ONE IT CANNOT

analyst_health.py measures whether reads arrive, how fast, and as what model.
Every one of those can be perfect while the reads are worthless. "Is this
analysis correct" is answerable only against what the market then did, so this
module reads OUTCOMES and nothing else -- never the prose, never the confidence
on its own, never how convincing a `why` sounded.

It cannot tell you a single read was right. Nothing can: one trade is one draw
from a distribution nobody has measured. What it CAN do is ask whether the
analyst's own signals carry information across many reads, which is the only
form the question has an answer in.

THE THREE QUESTIONS, in the order they become answerable:

  CALIBRATION   does higher stated confidence actually resolve better? If a
                conf-4 read pays no more than a conf-2, the confidence field is
                noise being carried into every downstream decision that reads it
                -- sizing, the evidence tier, the router.

  DISCRIMINATION does the analyst beat its own base rate? A desk whose signals
                resolve exactly like a coin has an expensive random number
                generator, and every gate downstream is tuning noise.

  SELECTION     do the trades it TOOK beat the ones it REFUSED? This is the
                sharpest of the three and the least intuitive: an analyst that
                refuses better trades than it takes is not being cautious, it is
                being wrong in a way that no win-rate can show.

WHAT MAKES THIS HONEST RATHER THAN FLATTERING

Every check states its denominator and refuses below it. The desk has TWO
resolved trades as this is written, and a calibration curve over two trades is
not a weak signal -- it is an artifact of arithmetic wearing a percentage sign.
UNMEASURED is the correct answer for weeks yet, and saying so is the entire
value of the module until then.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence

READ_QUALITY_VERSION = "readq-2026-08-28-a"

#: Resolved trades before CALIBRATION means anything. Confidence has five
#: levels; below this there is not one trade per level.
MIN_FOR_CALIBRATION = 25

#: Resolved trades before DISCRIMINATION means anything.
MIN_FOR_EDGE = 30

#: Refusals with a resolved forward path before SELECTION means anything.
MIN_FOR_SELECTION = 30


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'LOOK':<5}] {self.check:<22} {self.detail}"


def _resolved(rows: Sequence[dict]) -> list[dict]:
    return [r for r in rows
            if r.get("kind") == "TRADE_CLOSED"
            and isinstance(r.get("realised_r"), (int, float))]


def _conf_of(rows: Sequence[dict], closed: dict) -> Optional[int]:
    """The confidence the analyst stated on the SIGNAL that opened this trade."""
    for r in rows:
        if r.get("kind") == "SIGNAL" and str(r.get("t0")) == str(closed.get("entry_t0")):
            c = ((r.get("decision") or {}).get("analyst_read") or {}).get("confidence")
            return int(c) if c is not None else None
    return None


def check_calibration(rows: Sequence[dict]) -> Finding:
    """Does higher stated confidence resolve better?

    If it does not, `confidence` is noise -- and it is not an idle field: sizing
    reads it, the evidence tier reads it, and a human reads it off the message
    before deciding what to risk.
    """
    pairs = []
    for c in _resolved(rows):
        conf = _conf_of(rows, c)
        if conf is not None:
            pairs.append((conf, float(c["realised_r"])))
    if len(pairs) < MIN_FOR_CALIBRATION:
        return Finding("calibration", True,
                       f"UNMEASURED — {len(pairs)} resolved trade(s) carry a "
                       f"stated confidence, under {MIN_FOR_CALIBRATION}. A "
                       f"calibration curve over this many is arithmetic, not "
                       f"evidence.")
    hi = [r for c, r in pairs if c >= 4]
    lo = [r for c, r in pairs if c <= 2]
    if len(hi) < 5 or len(lo) < 5:
        return Finding("calibration", True,
                       f"UNMEASURED — {len(hi)} high-confidence and {len(lo)} "
                       f"low-confidence resolved trades; the comparison needs "
                       f"both tails")
    mh, ml = statistics.fmean(hi), statistics.fmean(lo)
    if mh <= ml:
        return Finding("calibration", False,
                       f"conf>=4 resolves {mh:+.2f}R against conf<=2 at {ml:+.2f}R "
                       f"— stated confidence carries NO information, and sizing, "
                       f"the evidence tier and the operator all read it as though "
                       f"it does.")
    return Finding("calibration", True,
                   f"conf>=4 {mh:+.2f}R vs conf<=2 {ml:+.2f}R over {len(pairs)} trades")


def check_edge(rows: Sequence[dict]) -> Finding:
    """Does the analyst beat a coin, after costs?

    Deliberately not a Sharpe or a t-stat: with this sample size a test
    statistic invites a precision the data cannot support. Mean R after costs is
    the number the account actually experiences.
    """
    rs = [float(c["realised_r"]) for c in _resolved(rows)]
    if len(rs) < MIN_FOR_EDGE:
        return Finding("edge", True,
                       f"UNMEASURED — {len(rs)} resolved trade(s), under "
                       f"{MIN_FOR_EDGE}. Whether this desk has an edge is the "
                       f"one question it exists to answer, and it is NOT "
                       f"answered yet.")
    mean = statistics.fmean(rs)
    wins = sum(1 for r in rs if r > 0)
    if mean <= 0:
        return Finding("edge", False,
                       f"{mean:+.3f}R per trade over {len(rs)} resolved "
                       f"({wins} wins). The reads are arriving and resolving "
                       f"NEGATIVE — every gate downstream is tuning noise.")
    return Finding("edge", True,
                   f"{mean:+.3f}R per trade over {len(rs)} resolved ({wins} wins)")


def check_selection(rows: Sequence[dict]) -> Finding:
    """Do the trades it TOOK beat the ones it REFUSED?

    The sharpest of the three and the least intuitive. An analyst refusing
    better trades than it takes is not cautious -- it is wrong in a direction no
    win-rate can show, because the refused trades never enter the numerator.

    Refusals carry a resolved forward path precisely so this is answerable.
    """
    took = [float(c["realised_r"]) for c in _resolved(rows)]
    passed = []
    for r in rows:
        if not str(r.get("kind", "")).startswith("REFUSAL"):
            continue
        mfe = (r.get("outcome") or {}).get("mfe_r")
        if isinstance(mfe, (int, float)):
            passed.append(float(mfe))
    if len(took) < 10 or len(passed) < MIN_FOR_SELECTION:
        return Finding("selection", True,
                       f"UNMEASURED — {len(took)} taken and {len(passed)} refused "
                       f"with a resolved path; needs 10 and {MIN_FOR_SELECTION}")
    mt, mp = statistics.fmean(took), statistics.fmean(passed)
    # The refused side is MFE, an upper bound on what a refusal could have paid;
    # the taken side is realised. The comparison is deliberately unfair TO THE
    # DESK -- if it still wins, the selection is real.
    if mt <= 0 and mp > 0:
        return Finding("selection", False,
                       f"taken trades resolve {mt:+.2f}R while refusals reached "
                       f"{mp:+.2f}R at best. The analyst is selecting AGAINST "
                       f"itself — that is not caution, it is being wrong in a "
                       f"direction no win-rate shows.")
    return Finding("selection", True,
                   f"taken {mt:+.2f}R realised vs refused {mp:+.2f}R best-case "
                   f"over {len(took)}/{len(passed)}")


def audit(rows: Sequence[dict]) -> list[Finding]:
    return [check_calibration(rows), check_edge(rows), check_selection(rows)]


def render(findings: Sequence[Finding]) -> str:
    bad = [f for f in findings if not f.ok]
    unknown = [f for f in findings if f.ok and "UNMEASURED" in f.detail]
    if findings and len(unknown) == len(findings):
        head = (f"READ QUALITY ({READ_QUALITY_VERSION}) — NOTHING IS MEASURABLE "
                f"YET. Whether these reads are any good is UNKNOWN, which is not "
                f"the same as fine.")
    elif bad:
        head = f"READ QUALITY ({READ_QUALITY_VERSION}) — {len(bad)} finding(s)"
    else:
        head = f"READ QUALITY ({READ_QUALITY_VERSION}) — holding up so far"
    out = [head] + [f.line for f in findings]
    out += ["",
            "  NONE of this can say a single read was right. One trade is one",
            "  draw from a distribution nobody has measured. These ask whether",
            "  the analyst's signals carry information ACROSS many reads, which",
            "  is the only form the question has an answer in."]
    return "\n".join(out)
