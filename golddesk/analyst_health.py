"""Is the analyst still answering, and still answering WELL?

WHY THIS IS ITS OWN AXIS

self_audit asks "is the desk wired". capture asks "is it still exploiting".
Neither can see the analyst degrade, and it can degrade in ways that look
exactly like a quiet market:

  it stops answering at all -- a wedged CLI session, an expired login, a
  provider outage. Observed 2026-08-27: dozens of reads discarded on prose
  length, and the ledger recorded nothing at all for them.

  it keeps answering but SLOWER, drifting toward the timeout. Every read that
  crosses it is silently lost, and the first symptom is fewer decisions --
  which reads as selectivity.

  it quietly answers as a DIFFERENT MODEL or at a lower effort. A fallback is
  supposed to be visible; a fallback nobody notices is a permanent downgrade.
  Observed on the quant desk: "roster capabilities ABSENT -> EVERY seat runs on
  the 'high' fallback, not its advertised max."

NONE of these raises. All of them look like a desk being careful.

WHAT THIS CANNOT DO, said plainly because it is the limit that matters: it
cannot tell whether a read is CORRECT. That needs resolved trades, and no
number of checks substitutes for them. It measures whether the analyst is
RESPONDING, how fast, and as what -- the mechanical half, which is the half that
fails silently.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

ANALYST_HEALTH_VERSION = "analyst-2026-08-28-a"

#: Wakes in a window before an answer RATE means anything.
MIN_WAKES = 20

#: Fraction of recent wakes the analyst may fail to answer before it is a fault
#: rather than a blip. A fifth is generous on purpose: a busy provider drops the
#: occasional call, and a check that fires on one timeout is one nobody reads.
BLIND_FRACTION = 0.20

#: Reads before a LATENCY comparison means anything.
MIN_READS = 10

#: Fraction of the timeout budget the median read may reach before it is worth
#: naming. At 0.75 of the budget, ordinary variance is already crossing it and
#: those reads are being lost silently.
LATENCY_WARN_FRAC = 0.75

#: The desk's primary read budget, seconds. Mirrors providers.DEFAULT_TIMEOUT_S;
#: passed in by the caller where it matters, defaulted here so the module has no
#: import-time dependency on the provider layer.
DEFAULT_BUDGET_S = 600.0


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'BROKEN'}] {self.check:<22} {self.detail}"


def _ts(r: dict) -> Optional[datetime]:
    raw = r.get("t0") or r.get("ts")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _recent(rows: Sequence[dict], now: datetime, hours: float) -> list[dict]:
    cut = now - timedelta(hours=hours)
    return [r for r in rows if (_ts(r) or now) >= cut]


def _stamps(rows: Sequence[dict]) -> list[dict]:
    """Decision rows carrying a provider stamp -- the analyst actually ran."""
    out = []
    for r in rows:
        dec = r.get("decision") or {}
        if dec.get("provider") or dec.get("model"):
            out.append(dec)
    return out


def check_answer_rate(rows: Sequence[dict], now: Optional[datetime] = None,
                      hours: float = 12.0) -> Finding:
    """How often a wake produced an answer rather than a BLIND row.

    This is the check that would have caught 2026-08-27 in the hour it started
    instead of at the end of the day.
    """
    now = now or datetime.now(timezone.utc)
    rec = _recent(rows, now, hours)
    answered = [r for r in rec if str(r.get("kind", "")) in
                ("SIGNAL", "REFUSAL_MODEL", "REFUSAL_COMPILER", "REFUSAL_ROUTER")]
    blind = [r for r in rec if r.get("kind") == "BLIND"]
    total = len(answered) + len(blind)
    if total < MIN_WAKES:
        return Finding("analyst answering", True,
                       f"UNMEASURED — {total} wake(s) in {hours:.0f}h, under {MIN_WAKES}")
    frac = len(blind) / total
    if frac > BLIND_FRACTION:
        return Finding("analyst answering", False,
                       f"{len(blind)}/{total} wakes ({frac:.0%}) got NO answer in "
                       f"the last {hours:.0f}h. The desk is BLIND on those bars, "
                       f"not selective — a wedged session, an expired login or a "
                       f"provider outage all look like this.")
    return Finding("analyst answering", True,
                   f"{len(answered)}/{total} wakes answered ({1 - frac:.0%})")


def check_latency(rows: Sequence[dict], now: Optional[datetime] = None,
                  hours: float = 12.0,
                  budget_s: float = DEFAULT_BUDGET_S) -> Finding:
    """Median read latency against the timeout budget.

    Drift toward the budget is the failure mode nobody sees: every read that
    crosses it is lost, the desk simply decides less, and less deciding reads as
    discipline. Median rather than mean -- one 600s outlier should not define
    the reading, and the question is where the BULK of reads sit.
    """
    now = now or datetime.now(timezone.utc)
    lat = [float(d["latency_ms"]) / 1000.0
           for d in _stamps(_recent(rows, now, hours))
           if d.get("latency_ms") is not None]
    if len(lat) < MIN_READS:
        return Finding("analyst latency", True,
                       f"UNMEASURED — {len(lat)} stamped read(s), under {MIN_READS}")
    med = statistics.median(lat)
    frac = med / budget_s if budget_s else 0.0
    if frac > LATENCY_WARN_FRAC:
        return Finding("analyst latency", False,
                       f"median read {med:.0f}s against a {budget_s:.0f}s budget "
                       f"({frac:.0%}). Ordinary variance is already crossing it, "
                       f"and every read that does is lost silently — the desk "
                       f"just decides less, which looks like selectivity.")
    return Finding("analyst latency", True,
                   f"median {med:.0f}s of {budget_s:.0f}s budget ({frac:.0%}) "
                   f"over {len(lat)} reads")


def check_model_is_what_was_asked_for(rows: Sequence[dict],
                                      expected_model: Optional[str] = None,
                                      now: Optional[datetime] = None,
                                      hours: float = 12.0) -> Finding:
    """Is the analyst answering as the model the desk was configured with?

    A fallback is supposed to be visible. A fallback nobody notices is a
    permanent downgrade wearing the configured name -- exactly the shape of the
    quant desk's "roster capabilities ABSENT -> every seat runs on the 'high'
    fallback, not its advertised max".
    """
    now = now or datetime.now(timezone.utc)
    models = [str(d.get("model")) for d in _stamps(_recent(rows, now, hours))
              if d.get("model")]
    if not models:
        return Finding("analyst model", True, "no stamped reads in the window")
    seen = sorted(set(models))
    if expected_model and any(expected_model not in m for m in seen):
        odd = [m for m in seen if expected_model not in m]
        return Finding("analyst model", False,
                       f"reads answered as {odd} while the desk was configured "
                       f"for {expected_model!r}. A fallback nobody notices is a "
                       f"permanent downgrade.")
    if len(seen) > 1:
        return Finding("analyst model", False,
                       f"{len(seen)} different models answered in {hours:.0f}h "
                       f"({seen}) — reads from different models are not "
                       f"comparable evidence and land in the same cohort.")
    return Finding("analyst model", True, f"all reads answered as {seen[0]}")


def audit(rows: Sequence[dict], now: Optional[datetime] = None,
          expected_model: Optional[str] = None,
          budget_s: float = DEFAULT_BUDGET_S) -> list[Finding]:
    return [
        check_answer_rate(rows, now),
        check_latency(rows, now, budget_s=budget_s),
        check_model_is_what_was_asked_for(rows, expected_model, now),
    ]


def render(findings: Sequence[Finding]) -> str:
    bad = [f for f in findings if not f.ok]
    head = (f"ANALYST HEALTH ({ANALYST_HEALTH_VERSION}) — "
            + ("responding normally" if not bad else f"{len(bad)} FAULT(S)"))
    out = [head] + [f.line for f in findings]
    if bad:
        out += ["",
                "  These measure whether the analyst is RESPONDING, how fast, and",
                "  as what. None of them can tell whether a read is CORRECT --",
                "  that needs resolved trades, and no number of checks is a",
                "  substitute for them."]
    return "\n".join(out)
