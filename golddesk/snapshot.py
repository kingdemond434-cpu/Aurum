"""Timestamp-causal market snapshots — the substrate every brain competes on.

The desk wants to ask "is Claude better than a rule, than a different model,
than me?" and the only comparison that answers it is PAIRED: every competitor
decides the SAME state, and the difference is taken per state. `competition.py`
does that arithmetic. This module produces the states.

WHY THAT IS HARDER THAN IT SOUNDS, AND WHY IT GETS ITS OWN FILE

A paired comparison is only as honest as the claim that both arms saw the same
thing, and `state_id` — `symbol|timeframe|timestamp` — is a COORDINATE. Two arms
can carry the identical id while having been shown different content: one built
from a snapshot that included the forming bar, one that did not; one with a
macro series at its latest revision, one at the vintage actually published that
day. They join cleanly. The p-value is computed over a population that never
existed, and nothing anywhere says so.

So a snapshot carries two identifiers and they answer different questions:

    state_id      WHICH MOMENT. Arm-independent, identical to competition.py's,
                  so pairing keeps working exactly as it does today.
    content_hash  WHAT WAS SHOWN. Over the causal content only. Two arms with
                  the same state_id and different content_hash were not
                  compared, whatever the table says — `assert_same_content()`
                  fails loudly rather than reporting a confident delta.

THE LOOKAHEAD THIS EXISTS TO MAKE STRUCTURALLY IMPOSSIBLE

Every observation carries the instant it became KNOWABLE, and the builder
refuses any observation stamped after the snapshot's `as_of`. Refuses, not
warns: a snapshot that contains one field from the future is not slightly
optimistic, it is evidence of nothing, and the failure is invisible in every
downstream number it touches.

Two specific ways it gets in, both handled here rather than left to callers:

  THE FORMING BAR. A bar labelled 12:00 on M15 is not knowable at 12:00 — it is
  knowable at 12:15, when it closes. Including it is the single most common
  backtest leak in existence and it looks like nothing: the series simply ends
  at the right timestamp. `add_bars()` computes knowability from open time plus
  period and drops the bar that has not finished.

  THE REVISED SERIES. Macro data is republished. CPI as it stands today is not
  CPI as it was known the morning it printed, and a brain shown the revised
  figure is being told the answer. Revisable observations carry `vintage_utc`
  and the builder refuses a vintage published after `as_of` even when the
  underlying reference period is safely historical.

WHAT AN EXTERNAL BRAIN GETS

`to_json()` — the whole snapshot, self-describing, with no desk objects in it.
That is deliberate: a competitor that has to import golddesk to be scored is a
competitor that can read the ledger, and the point of a league is to admit
brains we did not write.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

SNAPSHOT_VERSION = "snap-2026-08-18-a"

#: Bar period by timeframe name, used to turn an OPEN time into the instant the
#: bar became knowable. Anything not listed here cannot be checked for the
#: forming-bar leak, so `add_bars` refuses it rather than guessing.
PERIODS: dict[str, timedelta] = {
    "M1": timedelta(minutes=1), "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15), "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1), "H4": timedelta(hours=4),
    "D1": timedelta(days=1), "W1": timedelta(weeks=1),
}


class LookaheadError(Exception):
    """An observation was offered that could not have been known at `as_of`.

    Deliberately an exception and not a filter. Silently dropping the offending
    field would leave the snapshot valid-looking and one field poorer than the
    caller believed, and the caller would never find out.
    """


def _utc(t: datetime) -> datetime:
    """Naive datetimes are the other way lookahead gets in: a naive 12:00
    compared against an aware 12:00+02:00 raises, and the usual fix is to strip
    tzinfo from both, which quietly shifts every comparison by the offset."""
    if t.tzinfo is None:
        raise ValueError(f"naive datetime {t!r} — every instant here must carry "
                         "a timezone, or the causality check compares wall "
                         "clocks from different zones")
    return t.astimezone(timezone.utc)


@dataclass(frozen=True)
class Observation:
    """One fact, and the instant it became knowable.

    `observed_utc` is NOT when the fact refers to — it is when the desk could
    first have had it. For a closed bar those differ by one period; for a macro
    print they differ by however long the agency took to publish.
    """
    key: str
    value: Any
    observed_utc: datetime
    source: str = "desk"
    #: For revisable series: which publication this value came from. A reference
    #: period safely in the past tells you nothing about whether the NUMBER was
    #: knowable — that is what the vintage is for.
    vintage_utc: Optional[datetime] = None

    def knowable_at(self) -> datetime:
        """The later of observation and vintage. Both must have passed."""
        t = _utc(self.observed_utc)
        return max(t, _utc(self.vintage_utc)) if self.vintage_utc else t

    def to_dict(self) -> dict:
        d = {"key": self.key, "value": self.value,
             "observed_utc": _utc(self.observed_utc).isoformat(),
             "source": self.source}
        if self.vintage_utc:
            d["vintage_utc"] = _utc(self.vintage_utc).isoformat()
        return d


@dataclass(frozen=True)
class CausalSnapshot:
    """Everything a brain may see at one instant, and nothing else."""
    symbol: str
    timeframe: str
    as_of_utc: datetime
    observations: tuple[Observation, ...] = ()
    #: Recorded, never used to decide. A snapshot that knows which brain will
    #: read it has already stopped being arm-independent.
    built_by: str = SNAPSHOT_VERSION

    # -- identity ------------------------------------------------------
    @property
    def state_id(self) -> str:
        """WHICH MOMENT. Byte-identical to competition.state_id so the existing
        pairing keeps working — this module adds a check, it does not fork the
        join key."""
        return f"{self.symbol}|{self.timeframe}|{_utc(self.as_of_utc).isoformat()}"

    @property
    def content_hash(self) -> str:
        """WHAT WAS SHOWN. Over the causal content, sorted, so two snapshots
        built in a different order over the same facts hash the same — the
        question is what the brain saw, not what order the builder ran in.

        `built_by` is EXCLUDED. A version bump that changes no observation must
        not invalidate a league's paired history; a changed observation must.
        """
        body = json.dumps(
            {"symbol": self.symbol, "timeframe": self.timeframe,
             "as_of": _utc(self.as_of_utc).isoformat(),
             "obs": sorted((o.to_dict() for o in self.observations),
                           key=lambda d: (d["key"], d["observed_utc"]))},
            sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    # -- access --------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        for o in self.observations:
            if o.key == key:
                return o.value
        return default

    def keys(self) -> tuple[str, ...]:
        return tuple(o.key for o in self.observations)

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return {"version": SNAPSHOT_VERSION, "symbol": self.symbol,
                "timeframe": self.timeframe,
                "as_of_utc": _utc(self.as_of_utc).isoformat(),
                "state_id": self.state_id, "content_hash": self.content_hash,
                "built_by": self.built_by,
                "observations": [o.to_dict() for o in self.observations]}

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @staticmethod
    def from_dict(d: dict) -> "CausalSnapshot":
        obs = tuple(
            Observation(key=o["key"], value=o["value"],
                        observed_utc=datetime.fromisoformat(o["observed_utc"]),
                        source=o.get("source", "desk"),
                        vintage_utc=(datetime.fromisoformat(o["vintage_utc"])
                                     if o.get("vintage_utc") else None))
            for o in d.get("observations", ()))
        snap = CausalSnapshot(symbol=d["symbol"], timeframe=d["timeframe"],
                              as_of_utc=datetime.fromisoformat(d["as_of_utc"]),
                              observations=obs,
                              built_by=d.get("built_by", "unknown"))
        # A round trip that silently produced different content would defeat the
        # entire purpose of shipping snapshots to external brains: they would be
        # scored on something other than what was recorded.
        if "content_hash" in d and d["content_hash"] != snap.content_hash:
            raise ValueError(
                f"content_hash mismatch on load: recorded {d['content_hash']}, "
                f"recomputed {snap.content_hash}. This snapshot was edited or "
                f"serialised by an incompatible version; it must not be scored.")
        return snap

    @staticmethod
    def from_json(s: str) -> "CausalSnapshot":
        return CausalSnapshot.from_dict(json.loads(s))

    # -- what a brain reads --------------------------------------------
    def render(self) -> str:
        """Plain text for a model that is not ours. Ordered by key so two brains
        cannot be handed the same facts in a different order and disagree for
        that reason alone."""
        head = [f"AS OF   {_utc(self.as_of_utc).isoformat()}",
                f"SYMBOL  {self.symbol}   TIMEFRAME {self.timeframe}",
                f"STATE   {self.state_id}",
                f"CONTENT {self.content_hash}",
                "",
                "Every value below was knowable at AS OF. Nothing after it exists.",
                ""]
        for o in sorted(self.observations, key=lambda x: x.key):
            v = o.value
            if isinstance(v, float):
                v = f"{v:.5f}".rstrip("0").rstrip(".")
            head.append(f"{o.key:<28} {v}")
        return "\n".join(head)


class SnapshotBuilder:
    """Accumulates observations and refuses the ones from the future."""

    def __init__(self, symbol: str, timeframe: str, as_of_utc: datetime,
                 built_by: str = SNAPSHOT_VERSION):
        self.symbol, self.timeframe = symbol, timeframe
        self.as_of = _utc(as_of_utc)
        self.built_by = built_by
        self._obs: list[Observation] = []
        #: What was offered and rejected. A snapshot missing a field because the
        #: data was not yet knowable is a different fact from one missing it
        #: because nobody supplied it, and only the first is about the market.
        self.refused: list[tuple[str, str]] = []

    def add(self, key: str, value: Any, observed_utc: datetime,
            source: str = "desk", vintage_utc: Optional[datetime] = None) -> "SnapshotBuilder":
        o = Observation(key, value, observed_utc, source, vintage_utc)
        k = o.knowable_at()
        if k > self.as_of:
            raise LookaheadError(
                f"{key!r} was knowable at {k.isoformat()}, which is after this "
                f"snapshot's as_of {self.as_of.isoformat()}"
                + (f" (vintage {_utc(vintage_utc).isoformat()})" if vintage_utc else "")
                + ". A snapshot with one field from the future is evidence of "
                  "nothing, and the damage is invisible downstream.")
        self._obs.append(o)
        return self

    def add_if_known(self, key: str, value: Any, observed_utc: datetime,
                     source: str = "desk",
                     vintage_utc: Optional[datetime] = None) -> "SnapshotBuilder":
        """For genuinely optional context: skip rather than raise, and RECORD
        the skip. Use this only where absence is expected and meaningful; `add`
        is the default because a silent drop is how a field disappears from
        every snapshot without anyone noticing."""
        try:
            return self.add(key, value, observed_utc, source, vintage_utc)
        except LookaheadError as e:
            self.refused.append((key, str(e)))
            return self

    def add_bars(self, key_prefix: str, bars: Sequence[Any], timeframe: str,
                 fields: Sequence[str] = ("open", "high", "low", "close"),
                 count: int = 40) -> "SnapshotBuilder":
        """Closed bars only, knowability derived from open time plus period.

        THE FORMING BAR IS THE LEAK. A bar labelled 12:00 on M15 is knowable at
        12:15, not at 12:00. Included, it hands the brain the very candle it is
        being asked to predict — and the series still ends at exactly the
        timestamp a reviewer would expect, so nothing looks wrong.
        """
        period = PERIODS.get(timeframe.upper())
        if period is None:
            raise ValueError(
                f"unknown timeframe {timeframe!r}: cannot compute when a bar "
                f"becomes knowable, so the forming-bar leak cannot be checked. "
                f"Known: {', '.join(sorted(PERIODS))}")
        kept = 0
        for b in reversed(list(bars)):
            if kept >= count:
                break
            t = getattr(b, "time", None) or getattr(b, "ts", None)
            if t is None:
                raise ValueError("bar has neither .time nor .ts")
            closes_at = _utc(t) + period
            if closes_at > self.as_of:
                continue                      # still forming — not knowable yet
            idx = kept                        # 0 is the most recent CLOSED bar
            for f in fields:
                v = getattr(b, f, None)
                if v is not None:
                    self.add(f"{key_prefix}.{idx}.{f}", float(v), closes_at,
                             source=f"bars/{timeframe}")
            kept += 1
        self.add(f"{key_prefix}.n_closed", kept, self.as_of, source="builder")
        return self

    def build(self) -> CausalSnapshot:
        return CausalSnapshot(self.symbol, self.timeframe, self.as_of,
                              tuple(self._obs), self.built_by)


# ------------------------------------------------------------------ the league

@dataclass
class Decision:
    """One brain's answer on one snapshot. Free-form on purpose: an external
    competitor must not have to model our signal schema to enter."""
    brain: str
    state_id: str
    content_hash: str
    action: str                              # LONG | SHORT | FLAT | NO_SETUP
    reasoning: str = ""
    confidence: Optional[float] = None
    meta: dict = field(default_factory=dict)
    decided_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {"brain": self.brain, "state_id": self.state_id,
                "content_hash": self.content_hash, "action": self.action,
                "reasoning": self.reasoning, "confidence": self.confidence,
                "meta": self.meta, "decided_utc": self.decided_utc.isoformat()}


def assert_same_content(decisions: Iterable[Decision]) -> tuple[bool, str]:
    """THE CHECK `state_id` CANNOT MAKE.

    Two arms joined on a coordinate may have been shown different content. This
    is where that becomes visible instead of becoming a p-value, and it returns
    a verdict rather than raising so a league can report the corrupt states and
    still score the clean ones.
    """
    by_state: dict[str, set[str]] = {}
    who: dict[str, set[str]] = {}
    for d in decisions:
        by_state.setdefault(d.state_id, set()).add(d.content_hash)
        who.setdefault(d.state_id, set()).add(d.brain)
    bad = {s: h for s, h in by_state.items() if len(h) > 1}
    if not bad:
        return True, f"{len(by_state)} states, every arm shown identical content"
    lines = [f"{len(bad)} of {len(by_state)} states were NOT a fair comparison: "
             f"arms paired on state_id but saw different content."]
    for s, hashes in list(bad.items())[:5]:
        lines.append(f"  {s}  hashes={sorted(hashes)}  arms={sorted(who[s])}")
    lines.append("Those states must be excluded, not averaged. A paired test "
                 "over them is a statement about a population that never existed.")
    return False, "\n".join(lines)


def paired_states(decisions: Iterable[Decision], brains: Sequence[str]) -> list[str]:
    """States EVERY named brain decided, and whose content they agree on.

    An arm that skipped a state must not have it counted: dropping the hard ones
    and keeping the easy ones is how an arm wins on paper.
    """
    seen: dict[str, dict[str, str]] = {}
    for d in decisions:
        seen.setdefault(d.state_id, {})[d.brain] = d.content_hash
    out = []
    for sid, m in seen.items():
        if all(b in m for b in brains) and len({m[b] for b in brains}) == 1:
            out.append(sid)
    return sorted(out)


@dataclass
class League:
    """The hour-by-hour record: which brain saw what, said what, and why."""
    snapshots: dict[str, CausalSnapshot] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)

    def offer(self, snap: CausalSnapshot) -> CausalSnapshot:
        self.snapshots[snap.state_id] = snap
        return snap

    def record(self, brain: str, snap: CausalSnapshot, action: str,
               reasoning: str = "", confidence: Optional[float] = None,
               **meta) -> Decision:
        """The content_hash is taken from the SNAPSHOT, never from the caller.
        A brain that could stamp its own hash could claim to have seen whatever
        made its record pair."""
        d = Decision(brain=brain, state_id=snap.state_id,
                     content_hash=snap.content_hash, action=action,
                     reasoning=reasoning, confidence=confidence, meta=meta)
        self.decisions.append(d)
        return d

    def brains(self) -> list[str]:
        return sorted({d.brain for d in self.decisions})

    def report(self) -> dict:
        ok, why = assert_same_content(self.decisions)
        bs = self.brains()
        paired = paired_states(self.decisions, bs) if len(bs) >= 2 else []
        acted = {b: sum(1 for d in self.decisions
                        if d.brain == b and d.action in ("LONG", "SHORT"))
                 for b in bs}
        return {
            "version": SNAPSHOT_VERSION,
            "brains": bs,
            "snapshots": len(self.snapshots),
            "decisions": len(self.decisions),
            "content_consistent": ok,
            "content_note": why,
            "paired_states": len(paired),
            "acted": acted,
            "verdict": (
                "not comparable — see content_note" if not ok else
                f"{len(paired)} states every brain saw identically"
                if len(bs) >= 2 else
                "one brain only; a league needs at least two to say anything"),
        }

    def to_jsonl(self) -> str:
        rows = [{"kind": "snapshot", **s.to_dict()} for s in self.snapshots.values()]
        rows += [{"kind": "decision", **d.to_dict()} for d in self.decisions]
        return "\n".join(json.dumps(r, default=str) for r in rows)
