"""Daily self-review — the loop that makes the analyst compound.

The desk improves by accumulating *resolved facts*, not opinions. Every night
this module:

    1. resolves yesterday's signals mechanically from price (never self-scored)
    2. resolves yesterday's REFUSALS the same way — the false-negative ledger
    3. measures calibration: does confidence 4 actually beat confidence 2?
    4. asks the analyst to read that record and propose lessons
    5. promotes a lesson to STANDING only after 10 days of consistent support

The hard boundary, enforced by code below:

    model MAY write   -> candidate lessons (prose, entering future prompts)
    model MAY NOT     -> thresholds, gates, the resolution rule, its own score

A lesson is context, never a rule. The compiler in analyst.py does not read
this file. That is deliberate: a model that could loosen its own gates would,
because looser gates produce more signals and more signals feel like progress.

`lessons.json` here is the same shape the desk already keeps, extended with
per-lesson support counts.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence

import anthropic
from pydantic import BaseModel, Field, ValidationError

from .analyst import MODEL, AnalystError

log = logging.getLogger(__name__)

PROMOTION_DAYS = 10          # charter rule: 10 days of support -> standing
MIN_BUCKET_N = 20            # below this, a calibration bucket says nothing


# --------------------------------------------------------------------------
# Facts (mechanical — no model involvement)
# --------------------------------------------------------------------------

Outcome = Literal["TARGET", "STOP", "EXPIRED", "PENDING"]


@dataclass(frozen=True)
class ResolvedSignal:
    """A signal that was sent, resolved forward from the execution venue."""
    signal_id: str
    sent_utc: datetime
    setup: str
    direction: str
    confidence: int
    outcome: Outcome
    r_realised: float          # net of spread, from actual bars


@dataclass(frozen=True)
class ResolvedRefusal:
    """A setup the desk declined. What would it have paid?

    This is the asset the manual-execution boundary buys for free: the desk can
    resolve the path it did not take. Execution desks pay for this with capital.
    """
    refusal_id: str
    seen_utc: datetime
    reason: str
    would_have_r: Optional[float]   # None if unresolvable (no entry level)


def calibration(signals: Sequence[ResolvedSignal]) -> dict[int, dict]:
    """Does the analyst's confidence carry information? Buckets 1-5.

    If mean R does not rise with confidence, confidence is noise — and that is
    a finding worth more than most positive results, because it means the
    number can be dropped from the signal entirely.
    """
    buckets: dict[int, list[float]] = defaultdict(list)
    for s in signals:
        if s.outcome != "PENDING":
            buckets[s.confidence].append(s.r_realised)

    out: dict[int, dict] = {}
    for c in sorted(buckets):
        rs = buckets[c]
        out[c] = {
            "n": len(rs),
            "mean_r": round(statistics.fmean(rs), 4),
            "win_rate": round(sum(r > 0 for r in rs) / len(rs), 3),
            "informative": len(rs) >= MIN_BUCKET_N,
        }
    return out


def calibration_verdict(cal: dict[int, dict]) -> str:
    """One honest line about whether confidence separates."""
    usable = {c: d for c, d in cal.items() if d["informative"]}
    if len(usable) < 2:
        total = sum(d["n"] for d in cal.values())
        return (f"UNDETERMINED — {total} resolved across {len(cal)} buckets, "
                f"need {MIN_BUCKET_N}+ in at least two")
    ordered = [usable[c]["mean_r"] for c in sorted(usable)]
    if ordered == sorted(ordered):
        return f"SEPARATES — mean R rises monotonically across {len(usable)} buckets"
    return ("DOES NOT SEPARATE — confidence is not carrying information; "
            "treat it as noise until it does")


# --------------------------------------------------------------------------
# Lessons (model-writable, append-only, never executable)
# --------------------------------------------------------------------------

class CandidateLesson(BaseModel):
    model_config = {"extra": "forbid"}

    claim: str = Field(max_length=280, description="One falsifiable sentence.")
    mechanism: str = Field(max_length=280, description="Why this would be true economically.")
    anti_condition: str = Field(max_length=280, description="When it would NOT hold. Required.")
    evidence: str = Field(max_length=280, description="Which resolved rows support it.")


class DailyReview(BaseModel):
    model_config = {"extra": "forbid"}

    summary: str = Field(max_length=900)
    what_went_wrong: str = Field(max_length=700, description="Never empty. Find something.")
    candidate_lessons: list[CandidateLesson] = Field(max_length=3)


REVIEW_SYSTEM = """\
You are reviewing the gold desk's own forward record for one day. You are not \
trading and not forecasting — you are reading what already resolved.

## What you may and may not do

You may propose lessons. A lesson is an observation that enters future reads as \
context. You may NOT propose changes to thresholds, gates, risk limits, or the \
way outcomes are scored. Those are outside your reach by design: a reviewer who \
can loosen the rules that judge it will loosen them.

## Standards

One day is a tiny sample. Most days contain no lesson at all, and proposing zero \
is the correct and common answer. A lesson needs 10 days of consistent support \
before it becomes standing, so a claim you cannot imagine being retested \
tomorrow is not a lesson.

Every candidate needs an `anti_condition` — the case where it would not hold. A \
claim that cannot fail cannot be tested and will be discarded.

`what_went_wrong` is mandatory and must be substantive. A day that produced only \
wins still contains errors: setups missed, refusals that would have paid, \
confidence that did not match outcome, signals that resolved right for the wrong \
reason. Find them. A review that reports only confirmation has failed regardless \
of the day's P&L.

## What counts as evidence

Resolved rows only. "It felt like" is not evidence. Refusals that would have \
paid are as informative as signals that did — more so, because the desk has \
almost none of them recorded.

Beware of reading a trend into a handful of trades on one instrument with one \
price history. If a pattern would need 50 observations to distinguish from noise \
and you have 4, say so instead of proposing it.
"""


def build_review_prompt(
    day: date,
    signals: Sequence[ResolvedSignal],
    refusals: Sequence[ResolvedRefusal],
    cal: dict[int, dict],
    standing: Sequence[str],
) -> str:
    resolved = [s for s in signals if s.outcome != "PENDING"]
    net_r = sum(s.r_realised for s in resolved)

    lines = [f"DAY {day.isoformat()}", ""]
    lines.append(f"SIGNALS SENT {len(signals)}   RESOLVED {len(resolved)}   NET {net_r:+.2f}R")
    for s in resolved:
        lines.append(
            f"  {s.signal_id}  {s.setup:<20} {s.direction:<5} conf{s.confidence} "
            f"-> {s.outcome:<8} {s.r_realised:+.2f}R"
        )

    paid = [r for r in refusals if r.would_have_r is not None]
    missed = sum(r.would_have_r for r in paid if r.would_have_r > 0)
    lines += ["", f"REFUSALS {len(refusals)}   RESOLVABLE {len(paid)}   "
                  f"MISSED UPSIDE {missed:+.2f}R"]
    for r in paid[:40]:
        lines.append(f"  {r.refusal_id}  {r.reason[:60]:<60} would have paid {r.would_have_r:+.2f}R")

    lines += ["", "CALIBRATION TO DATE", f"  verdict: {calibration_verdict(cal)}"]
    for c in sorted(cal):
        d = cal[c]
        mark = "" if d["informative"] else "   (n too small to read)"
        lines.append(f"  conf {c}: n={d['n']:<4} mean {d['mean_r']:+.4f}R  "
                     f"win {d['win_rate']:.0%}{mark}")

    if standing:
        lines += ["", "STANDING LESSONS (already promoted — do not re-propose)"]
        lines += [f"  - {s}" for s in standing]

    return "\n".join(lines)


# --------------------------------------------------------------------------
# The ledger — append-only on disk
# --------------------------------------------------------------------------

@dataclass
class Lesson:
    claim: str
    mechanism: str
    anti_condition: str
    first_seen: str
    support_days: list[str] = field(default_factory=list)

    @property
    def standing(self) -> bool:
        return len(self.support_days) >= PROMOTION_DAYS


class LessonStore:
    """Append-only. Nothing here is ever executed — it only enters prompts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lessons: list[Lesson] = []
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding='utf-8') or "{}")
            self._lessons = [Lesson(**l) for l in raw.get("lessons", [])]

    def standing_claims(self) -> list[str]:
        return [l.claim for l in self._lessons if l.standing]

    def record(self, day: date, candidates: Sequence[CandidateLesson]) -> list[str]:
        """Add support to matching lessons, or open new ones. Returns promotions."""
        promoted: list[str] = []
        iso = day.isoformat()
        for c in candidates:
            match = next((l for l in self._lessons if _same_claim(l.claim, c.claim)), None)
            if match is None:
                self._lessons.append(Lesson(
                    claim=c.claim, mechanism=c.mechanism,
                    anti_condition=c.anti_condition, first_seen=iso,
                    support_days=[iso],
                ))
                continue
            if iso in match.support_days:
                continue
            was = match.standing
            match.support_days.append(iso)
            if match.standing and not was:
                promoted.append(match.claim)
        self._flush()
        return promoted

    def _flush(self) -> None:
        self.path.write_text(json.dumps(
            {"lessons": [asdict(l) for l in self._lessons]}, indent=1
        ), encoding="utf-8")


# Jaccard overlap above which two lesson claims are treated as the same claim.
# Named rather than inlined because the anti-drift auditor is right that a bare
# literal in a comparison is a threshold nobody declared. This one governs
# lesson deduplication, not trading, so it is not a constitutional restriction —
# but it is still a knob, and a named knob can be found and changed.
CLAIM_SIMILARITY = 0.6


def _same_claim(a: str, b: str) -> bool:
    """Crude dedupe by token overlap. Replace with embeddings if it misfires."""
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) > CLAIM_SIMILARITY


# --------------------------------------------------------------------------
# The nightly call
# --------------------------------------------------------------------------

def run_daily_review(
    day: date,
    signals: Sequence[ResolvedSignal],
    refusals: Sequence[ResolvedRefusal],
    all_signals_to_date: Sequence[ResolvedSignal],
    store: LessonStore,
    *,
    client: Optional[anthropic.Anthropic] = None,
) -> tuple[DailyReview, list[str]]:
    """One review. Returns (review, newly_promoted_claims). ~$0.10/night."""
    client = client or anthropic.Anthropic()
    cal = calibration(all_signals_to_date)
    prompt = build_review_prompt(day, signals, refusals, cal, store.standing_claims())

    schema = DailyReview.model_json_schema()
    schema["additionalProperties"] = False

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=[{"type": "text", "text": REVIEW_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "high",   # nightly, not latency-bound
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        raise AnalystError(f"review failed: {e.status_code} {e.message}") from e

    if resp.stop_reason == "refusal":
        raise AnalystError("review declined by the model")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise AnalystError("empty review")
    try:
        review = DailyReview.model_validate_json(text)
    except ValidationError as e:
        raise AnalystError(f"review schema violation: {e}") from e

    promoted = store.record(day, review.candidate_lessons)
    return review, promoted


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def format_signal(sig) -> str:
    """Actionable-only, per the desk's existing channel discipline."""
    arrow = "LONG" if sig.direction == "LONG" else "SHORT"
    return (
        f"*{arrow} XAUUSD* — {sig.setup.value.replace('_', ' ').title()}\n"
        f"`entry  {sig.entry:.2f}`\n"
        f"`stop   {sig.stop:.2f}`  ({sig.risk:.2f} risk)\n"
        f"`target {sig.target:.2f}`  ({sig.rr:.2f}R net of spread)\n"
        f"conf {sig.confidence}/5 · valid {sig.ttl_minutes}m · "
        f"cost {sig.spread_cost_r:.2f}R\n\n"
        f"{sig.read}\n\n"
        f"*Why:* {sig.why}\n"
        f"*Against:* {sig.why_not}\n"
        f"*Invalid if:* {sig.invalidation}"
    )


def format_review(day: date, review: DailyReview, promoted: Sequence[str]) -> str:
    """Nightly digest. Send to the research channel, not the signal channel."""
    parts = [f"*Desk review — {day.isoformat()}*", "", review.summary, "",
             f"*What went wrong:* {review.what_went_wrong}"]
    if review.candidate_lessons:
        parts += ["", f"*Candidates ({len(review.candidate_lessons)}):*"]
        parts += [f"· {c.claim}" for c in review.candidate_lessons]
    if promoted:
        parts += ["", f"*Promoted to standing after {PROMOTION_DAYS} days:*"]
        parts += [f"· {c}" for c in promoted]
    return "\n".join(parts)
