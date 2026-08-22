"""The macro state the analyst never saw.

WHAT WAS ACTUALLY MISSING

Aurum has serious macro machinery: `macro_vintage.py` keeps a point-in-time
store so a backtest cannot read a revision nobody had on the day,
`drivers_free.py` fetches the driver set daily from the cycle, and
`crossmarket.py` / `attribution.py` decompose gold's move across those
drivers. None of it reached the ANALYST. `MarketBrief` carried price
structure only -- trend, sweep, displacement, levels -- so the model making
the entry call had never seen a single macro variable. For an instrument
whose entire bid is macro, that is the gap worth closing first.

WHY ALL OF IT, NOT A GOLD-ONLY SUBSET

The tempting scope is "just the gold-relevant series". Every series in the
vector is gold-relevant, which is a fact about gold rather than a failure to
prioritise: real rates set the opportunity cost of holding a zero-coupon
asset, the dollar is the denomination, risk state drives haven flow,
inflation drives hedge demand, liquidity and the curve price the policy path
that moves all four. A subset would be a claim that some of these do not
transmit, and that claim is not measured anywhere.

WHAT THIS IS NOT

It is not a signal and carries no vote. `crossmarket.py` already states the
rule this follows -- cross-market context is EVIDENCE, it has no vote on
direction and never overrides structure. The analyst may weigh it; the
compiler still owns every number and the risk gate still holds the veto.
Adding a macro field must not become a back door through which prose moves
a stop.

STALENESS FAILS CLOSED

A macro vector read as current when it is a week old is worse than no macro
vector: every read inherits a stale world and nothing reports a problem.
`MacroContext.render()` says UNMEASURED when the state is missing, expired
or unparseable, and never substitutes a neutral value -- a zero rendered as
"neutral macro" would be indistinguishable, in the prompt, from a measured
neutral reading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

#: FRED publishes most series monthly; daily rate series refresh on business
#: days. 48h keeps a Friday read usable across the weekend while still
#: catching a fetcher that has genuinely stopped.
DEFAULT_MAX_AGE_H = 48.0


@dataclass(frozen=True)
class MacroContext:
    """One macro reading, with its own age attached and never defaulted."""
    updated: Optional[datetime] = None
    age_hours: float = float("inf")
    detail: str = "not loaded"
    states: dict[str, Any] = field(default_factory=dict)
    max_age_hours: float = DEFAULT_MAX_AGE_H

    @property
    def stale(self) -> bool:
        return self.updated is None or self.age_hours > self.max_age_hours

    @property
    def usable(self) -> bool:
        """False when missing, unparseable or expired.

        A caller treating False as "no macro tilt" is right. One treating it
        as "neutral macro" is wrong: neutral is a measurement, absence is
        not, and the whole point of this type is to keep those distinct.
        """
        return not self.stale

    def get(self, key: str) -> Optional[float]:
        """One value, or None. Never a zero default -- see the class docstring."""
        if not self.usable:
            return None
        v = self.states.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    @property
    def real_rate_index(self) -> Optional[float]:
        """POLICY_RATE - INFLATION_STATE. The classic gold driver.

        Gold pays no coupon, so its opportunity cost is the REAL rate, not the
        nominal one -- which is why a nominal-only view misreads gold badly in
        an inflationary cutting cycle.

        UNITS ARE NOT A YIELD. POLICY_RATE is percent (3.75); INFLATION_STATE
        is a z-score. Their difference is an ORDINAL INDEX and must be read as
        "higher = tighter real conditions", never quoted as a real yield in
        percent. Named `_index` so a call site cannot forget.
        """
        pol, inf = self.get("POLICY_RATE"), self.get("INFLATION_STATE")
        if pol is None or inf is None:
            return None
        return pol - inf

    def render(self) -> str:
        """The block that goes into the brief. Says UNMEASURED when it is."""
        if not self.usable:
            return ("MACRO CONTEXT: UNMEASURED — " + self.detail +
                    "\n  Treat as ABSENT, not as neutral. Do not infer a macro "
                    "tilt in either direction from this line.")
        lines = [
            f"MACRO CONTEXT (as of {self.updated:%Y-%m-%d %H:%MZ}, "
            f"{self.age_hours:.0f}h old — evidence only, no vote on direction)"
        ]
        for k, v in sorted(self.states.items()):
            if isinstance(v, bool):
                lines.append(f"  {k:<22} {v}")
            elif isinstance(v, (int, float)):
                lines.append(f"  {k:<22} {v:+.3f}")
            elif v is None:
                lines.append(f"  {k:<22} UNMEASURED")
        rr = self.real_rate_index
        if rr is not None:
            lines.append(f"  {'REAL_RATE_INDEX':<22} {rr:+.3f}  "
                         f"(POLICY_RATE − INFLATION_STATE; ordinal, not a yield)")
        return "\n".join(lines)


def load(path: Path, *, max_age_hours: float = DEFAULT_MAX_AGE_H,
         now: Optional[datetime] = None) -> MacroContext:
    """Read a macro state file. Never raises -- absence is a state, not an error.

    A loader that throws makes every call site wrap it in a try/except whose
    except branch invents a neutral default, which is exactly the
    substitution this module exists to prevent.
    """
    now = now or datetime.now(tz=timezone.utc)
    path = Path(path)
    if not path.exists():
        return MacroContext(detail=f"{path} absent — no macro fetch has run here",
                            max_age_hours=max_age_hours)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return MacroContext(detail=f"unreadable: {e}", max_age_hours=max_age_hours)

    raw = doc.get("updated")
    try:
        updated = datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        updated = None
    if updated is None:
        return MacroContext(
            detail="no parseable 'updated' timestamp — age UNKNOWN, which is "
                   "not the same as fresh",
            states=doc.get("states", {}) or {}, max_age_hours=max_age_hours)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)

    age_h = (now - updated).total_seconds() / 3600.0
    detail = (f"{age_h:.1f}h old (limit {max_age_hours:.0f}h) — the macro fetch "
              f"may have stopped" if age_h > max_age_hours else f"{age_h:.1f}h old")
    return MacroContext(updated=updated, age_hours=age_h, detail=detail,
                        states=doc.get("states", {}) or {},
                        max_age_hours=max_age_hours)


__all__ = ["MacroContext", "load", "DEFAULT_MAX_AGE_H"]
