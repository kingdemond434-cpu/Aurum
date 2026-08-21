"""Durable, versioned, reversible active-policy state.

The previous adaptation cycle had a hole big enough to invalidate the whole
claim of self-improvement: it wrote a `Change("policy", "active", ...)` row into
an audit trail and then *did nothing*. The active policy never moved. Evidence
accumulated, a line was logged, and the desk carried on running the incumbent.
An audit entry is not an adaptation.

It had a second hole: every promotion `adapt` did make (router ADVISORY ->
ENFORCING) mutated the in-memory `COHORTS` list. Restart the process and every
piece of learning evaporated silently, which is worse than not learning, because
the trail claims a change that the running system does not have.

This module is the substrate both holes needed. It owns:

  * WHICH policy is live, per decision slot, right now, on disk
  * WHY it is live — the evidence snapshot that promoted it
  * WHEN that evidence expires and must be re-earned (decay)
  * HOW to go back, exactly, one step at a time

Everything is a plain JSON document written atomically. There is no database
and no schema migration story, because the state is small and the ability to
read it with `cat` during an incident is worth more than elegance.

DECAY IS NOT OPTIONAL. A promotion carries an expiry. When it lapses the slot
reverts to its declared default and the policy must re-earn authority against
fresh evidence. A finding from 2019 is a historical fact, not a standing
permission.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

STATE_VERSION = "policystate-2026-08-14-a"

# How long a promotion stands before it must be re-proved. Chosen to be shorter
# than the horizon over which a gold regime can turn over completely; it is a
# REVALIDATION CADENCE, not a performance threshold, so it does not fall under
# the no-arbitrary-constants rule — but it is declared here rather than buried.
DEFAULT_TTL_DAYS = 90


@dataclass(frozen=True)
class Binding:
    """One decision slot bound to one policy version, with its warrant."""
    slot: str                  # e.g. "management_chooser", "reentry_policy"
    policy: str                # the policy's version string
    since: str                 # ISO date the binding took effect
    expires: str               # ISO date the warrant lapses
    warrant: str               # the evidence that justified it, in words
    evidence: dict = field(default_factory=dict)
    default: bool = False      # True when this is the declared fallback

    def live_on(self, day: date) -> bool:
        return day <= date.fromisoformat(self.expires)

    def render(self) -> str:
        tag = " (default)" if self.default else ""
        return (f"{self.slot:<22} -> {self.policy:<26}{tag}\n"
                f"{'':<22}    since {self.since} expires {self.expires}\n"
                f"{'':<22}    {self.warrant}")


class PolicyState:
    """The desk's active configuration, on disk, with history.

    Read at start-up, consulted at every decision, written only by adaptation.
    """

    def __init__(self, path: Path, defaults: Optional[dict[str, str]] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.defaults = dict(defaults or {})
        self._doc: dict[str, Any] = {"version": STATE_VERSION,
                                     "bindings": {}, "history": []}
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> "PolicyState":
        if self.path.exists():
            try:
                self._doc = json.loads(self.path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError) as e:
                # Never start on a half-written state file — an unreadable
                # policy state means "unknown", and unknown means defaults.
                log.error("policy state unreadable (%s) — falling back to defaults", e)
                self._doc = {"version": STATE_VERSION, "bindings": {}, "history": []}
        return self

    def _write(self) -> None:
        """Atomic replace. A torn policy file is a silent behaviour change."""
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._doc, fh, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- reading ---------------------------------------------------------
    def active(self, slot: str, *, on: Optional[date] = None) -> str:
        """The policy version bound to `slot` right now.

        Falls back to the declared default when nothing is bound OR when the
        binding's warrant has expired. Expiry is checked on every read, so a
        lapsed promotion stops applying even if no adaptation cycle has run
        since — the desk cannot coast on stale authority.
        """
        day = on or datetime.now(timezone.utc).date()
        raw = self._doc["bindings"].get(slot)
        if raw is None:
            return self.defaults.get(slot, "")
        b = Binding(**raw)
        if not b.live_on(day):
            log.info("binding %s -> %s EXPIRED on %s; reverting to default",
                     slot, b.policy, b.expires)
            return self.defaults.get(slot, "")
        return b.policy

    def binding(self, slot: str) -> Optional[Binding]:
        raw = self._doc["bindings"].get(slot)
        return Binding(**raw) if raw else None

    def expiring_within(self, days: int, *, on: Optional[date] = None) -> list[Binding]:
        """Warrants about to lapse — what the next research cycle must re-prove."""
        day = on or datetime.now(timezone.utc).date()
        out = []
        for raw in self._doc["bindings"].values():
            b = Binding(**raw)
            if not b.default and 0 <= (date.fromisoformat(b.expires) - day).days <= days:
                out.append(b)
        return sorted(out, key=lambda b: b.expires)

    # -- writing ---------------------------------------------------------
    def bind(self, slot: str, policy: str, warrant: str,
             evidence: Optional[dict] = None, *,
             ttl_days: int = DEFAULT_TTL_DAYS,
             on: Optional[date] = None) -> Binding:
        """Promote a policy into a slot. THIS is what changes desk behaviour."""
        day = on or datetime.now(timezone.utc).date()
        b = Binding(slot=slot, policy=policy, since=day.isoformat(),
                    expires=(day + timedelta(days=ttl_days)).isoformat(),
                    warrant=warrant, evidence=evidence or {})
        prev = self._doc["bindings"].get(slot)
        self._doc["bindings"][slot] = asdict(b)
        self._doc["history"].append({
            "ts": datetime.now(timezone.utc).isoformat(), "op": "bind",
            "slot": slot, "before": prev, "after": asdict(b)})
        self._write()
        log.info("policy bound: %s -> %s (%s)", slot, policy, warrant)
        return b

    def revert(self, slot: str) -> Optional[str]:
        """Undo the most recent binding for one slot, exactly.

        Restores the previous binding if there was one, otherwise clears the
        slot back to its default. Returns whatever is active afterwards.
        """
        for entry in reversed(self._doc["history"]):
            if entry.get("op") == "bind" and entry.get("slot") == slot:
                before = entry.get("before")
                if before is None:
                    self._doc["bindings"].pop(slot, None)
                else:
                    self._doc["bindings"][slot] = before
                self._doc["history"].append({
                    "ts": datetime.now(timezone.utc).isoformat(), "op": "revert",
                    "slot": slot, "undid": entry.get("after"), "restored": before})
                self._write()
                return self.active(slot)
        return None

    def render(self) -> str:
        out = [f"POLICY STATE {self.path}  ({self._doc.get('version')})"]
        if not self._doc["bindings"]:
            out.append("  (nothing bound — every slot on its declared default)")
        for slot in sorted(self._doc["bindings"]):
            out.append("  " + Binding(**self._doc["bindings"][slot]).render())
        for slot, dflt in sorted(self.defaults.items()):
            if slot not in self._doc["bindings"]:
                out.append(f"  {slot:<22} -> {dflt:<26} (default, unbound)")
        return "\n".join(out)
