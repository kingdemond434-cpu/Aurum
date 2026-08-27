"""Is the desk still exploiting, or has it quietly gone timid?

WHY THIS IS A SEPARATE AXIS

self_audit.py asks "is the desk WIRED". These checks ask "is it still TAKING
WHAT IS THERE". A desk can be perfectly wired, pass every integrity check, and
still be worth nothing because it refuses everything, banks 15% of the moves it
calls correctly, or stops receiving the quant desk's certified survivors.

None of those raise an error. All of them look like a quiet week.

THE HARD PART IS NOT MEASURING, IT IS NOT LYING

A low signal rate is NOT evidence of timidity -- the market may simply be quiet,
and a check that treats every slow week as a fault trains the operator to ignore
it. So every check here reports what it can support and refuses the rest:

  it compares the desk against ITS OWN recent history, never against a target
  rate somebody invented
  it names the DOMINANT REFUSING GATE, which is the actionable half regardless
  of whether the rate is a problem -- if one gate refuses most of everything,
  that is a lever whether or not the week was quiet
  it says UNMEASURED below a sample it cannot support, rather than reporting a
  ratio over four trades

AND NOTHING HERE IS A GATE. Every function reports. Raising or lowering a
threshold in response is a human decision, and the whole point of the objective
this desk runs under is that timidity is a DEFECT -- so a check that could make
it trade less would be the defect it exists to catch.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

CAPTURE_VERSION = "capture-2026-08-28-a"

#: Decisions in a window before a signal RATE means anything. Below this the
#: honest answer is UNMEASURED -- a rate over a handful of decisions is noise
#: wearing a percentage sign.
MIN_DECISIONS = 40

#: Closed winners before a CAPTURE ratio means anything.
MIN_WINNERS = 5

#: Capture below this is worth naming. Observed live: a +1.88R move that kept
#: +0.29R -- 15%. Not a tuned number: half is the plainest statement of "most of
#: the move reached the account".
CAPTURE_FLOOR = 0.50

#: Hours before the quant findings inbox is stale. The chain runs daily, so a
#: file older than this means a link is broken, not that quant found nothing.
INBOX_STALE_H = 36.0


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'LOOK':<5}] {self.check:<22} {self.detail}"


def _ts(r: dict) -> Optional[datetime]:
    raw = r.get("t0") or r.get("ts")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _decisions(rows: Sequence[dict]) -> list[dict]:
    """Rows where the desk actually reached a verdict. BLIND is excluded on
    purpose: a bar the analyst never answered on is not a decision to stand
    aside, and counting it would make an outage look like selectivity."""
    return [r for r in rows
            if str(r.get("kind", "")) in ("SIGNAL", "REFUSAL_MODEL",
                                          "REFUSAL_COMPILER", "REFUSAL_ROUTER")]


def check_signal_rate(rows: Sequence[dict], now: Optional[datetime] = None,
                      window_days: float = 3.0) -> Finding:
    """Recent signal rate against the desk's OWN prior rate.

    Against itself, never against a target: a desk that "should" fire N times a
    week is a quota, and a quota has no economics in it. A step DOWN with the
    market unchanged is the timidity signal; a step down because the market went
    quiet is not, and this cannot tell them apart -- so it reports the change
    and says which it cannot rule out.
    """
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=window_days)
    recent = [r for r in _decisions(rows) if (_ts(r) or now) >= cut]
    prior = [r for r in _decisions(rows) if (_ts(r) or now) < cut]
    if len(recent) < MIN_DECISIONS or len(prior) < MIN_DECISIONS:
        return Finding("signal rate", True,
                       f"UNMEASURED — {len(recent)} recent / {len(prior)} prior "
                       f"decisions, under {MIN_DECISIONS} either side")
    r_rate = sum(1 for r in recent if r.get("kind") == "SIGNAL") / len(recent)
    p_rate = sum(1 for r in prior if r.get("kind") == "SIGNAL") / len(prior)
    if p_rate > 0 and r_rate < p_rate * 0.5:
        return Finding("signal rate", False,
                       f"{r_rate:.1%} of decisions became signals in the last "
                       f"{window_days:.0f}d vs {p_rate:.1%} before — less than "
                       f"half the prior rate. Either the market changed or the "
                       f"desk got timid; this cannot tell them apart, and only "
                       f"the second is a defect.")
    return Finding("signal rate", True,
                   f"{r_rate:.1%} recent vs {p_rate:.1%} prior")


def check_dominant_gate(rows: Sequence[dict]) -> Finding:
    """Which gate refuses most. Reported ALWAYS, pass or fail.

    This is the actionable half whatever the signal rate is doing: if one gate
    accounts for most refusals it is the lever, and knowing that costs nothing.
    Naming it is not an argument for removing it -- the refusals it produced are
    already priced by missed_money against their own forward paths.
    """
    reasons = Counter()
    for r in rows:
        if not str(r.get("kind", "")).startswith("REFUSAL"):
            continue
        reasons[str(r.get("reason", "?")).split("—")[0].strip()[:60]] += 1
    if not reasons:
        return Finding("dominant gate", True, "no refusals recorded yet")
    top, n = reasons.most_common(1)[0]
    total = sum(reasons.values())
    return Finding("dominant gate", True,
                   f"{n}/{total} ({n / total:.0%}) — {top!r}. The lever, "
                   f"whatever the rate is doing.")


def check_capture(rows: Sequence[dict]) -> Finding:
    """How much of what the desk CALLED RIGHT actually reached the account.

    OBSERVED LIVE: a short reached +1.88R and kept +0.29R. The call was right and
    85% of it was given back -- a leak invisible to every win-rate statistic,
    because it counts as a win.
    """
    winners = [r for r in rows
               if r.get("kind") == "TRADE_CLOSED"
               and float(r.get("mfe_r") or 0) > 0]
    if len(winners) < MIN_WINNERS:
        return Finding("capture", True,
                       f"UNMEASURED — {len(winners)} trade(s) with positive MFE, "
                       f"under {MIN_WINNERS}")
    ratios = [max(0.0, float(w.get("realised_r") or 0)) / float(w["mfe_r"])
              for w in winners]
    avg = sum(ratios) / len(ratios)
    if avg < CAPTURE_FLOOR:
        return Finding("capture", False,
                       f"{avg:.0%} of MFE kept across {len(ratios)} winners. The "
                       f"calls were right and most of the move was given back — "
                       f"a leak no win-rate statistic can show, because these "
                       f"all count as wins.")
    return Finding("capture", True, f"{avg:.0%} of MFE kept over {len(ratios)} winners")


def check_quant_inbox(base: Path, now: Optional[datetime] = None) -> Finding:
    """Are the quant desk's findings still arriving?

    The chain is: quant's daily_cycle exports at 21:45, Aurum-Sync carries it at
    22:15, this desk absorbs at 22:40. It has THREE links and two of them live
    in the other repository. A break anywhere shows up here as an old file, and
    nowhere else -- step_absorb reporting "0 new findings" is indistinguishable
    from the quant desk having found nothing.
    """
    now = now or datetime.now(timezone.utc)
    inbox = base / "inbox" / "quant_findings.jsonl"
    if not inbox.exists():
        return Finding("quant inbox", False,
                       "no quant_findings.jsonl has EVER arrived. The transport "
                       "(Aurum-Sync, registered by quant's installer) is probably "
                       "not scheduled — quant's installer only registers it when "
                       "run with -AurumRoot.")
    age_h = (now.timestamp() - inbox.stat().st_mtime) / 3600.0
    if age_h > INBOX_STALE_H:
        return Finding("quant inbox", False,
                       f"last updated {age_h:.0f}h ago and the chain runs daily. "
                       f"A link is broken, which is not the same as quant having "
                       f"found nothing.")
    return Finding("quant inbox", True, f"updated {age_h:.0f}h ago")


#: The phrase quant's exporter puts on a certified-survivor finding. Matched
#: literally rather than by keyword, because the first version of this check
#: guessed at "survivor"/"certified"/"passed", matched NONE of the 69 real
#: findings, and reported the channel as carrying only refutations. It was
#: carrying E2/E3 MEASUREMENT RESULTS -- CAGR, q-values, arms measured -- and
#: the check was wrong, not the channel. A watchdog that cries wolf is one the
#: operator learns to ignore, which costs more than the check was ever worth.
SURVIVOR_MARK = "cleared this desk's full original ten-gate battery"


def check_survivors_absorbed(base: Path) -> Finding:
    """Has any cell ever cleared the quant desk's gate battery and arrived here?

    quant commit 920b709 exists because the export had been carrying only
    negatives. The survivor finding is emitted ONLY when QQUANT_GATES.json holds
    passing verdicts, so its absence has two very different causes and this
    refuses to pick between them:

      no cell has passed the ten-gate battery yet -- which is the honest state
      of a desk whose own gap register says its sleeves have zero forward
      evidence, and is NOT a fault here

      the gates or the export never ran, and the channel is silently dead

    Reported as a LOOK either way, with both readings stated, because the
    difference is answered on the quant side and not from this file.
    """
    inbox = base / "inbox" / "quant_findings.jsonl"
    if not inbox.exists():
        return Finding("survivors", True, "no findings file yet")
    total = survivors = 0
    for line in inbox.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:                              # noqa: BLE001
            continue
        total += 1
        if SURVIVOR_MARK in str(d.get("statement", "")):
            survivors += 1
    if not total:
        return Finding("survivors", True, "findings file is empty")
    if not survivors:
        return Finding("survivors", False,
                       f"{total} finding(s) arrived and none reports a cell "
                       f"clearing the ten-gate battery. Either nothing has "
                       f"passed yet — the honest state of a desk with no forward "
                       f"evidence, and no fault here — or the gates never ran. "
                       f"Answered on the quant side: check reports/QQUANT_GATES.json.")
    return Finding("survivors", True,
                   f"{survivors} survivor finding(s) of {total}")


def audit(rows: Sequence[dict], now: Optional[datetime] = None,
          base: Optional[Path] = None) -> list[Finding]:
    out = [check_signal_rate(rows, now), check_dominant_gate(rows),
           check_capture(rows)]
    if base is not None:
        out += [check_quant_inbox(base, now), check_survivors_absorbed(base)]
    return out


def render(findings: Sequence[Finding]) -> str:
    bad = [f for f in findings if not f.ok]
    head = (f"CAPTURE & ABSORPTION ({CAPTURE_VERSION}) — "
            + ("nothing to look at" if not bad else f"{len(bad)} worth a look"))
    out = [head] + [f.line for f in findings]
    if bad:
        out += ["",
                "  LOOK, not BROKEN. Every line above is a description of what",
                "  happened, not a verdict about what to change. Timidity is a",
                "  defect on this desk and so is acting on four trades -- these",
                "  are here so a leak is visible early, not so a threshold moves",
                "  on the strength of a bad week."]
    return "\n".join(out)
