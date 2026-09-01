"""State-change prediction: booking and resolution (#2).

Before the trade, the analyst states what the MARKET STATE will do next —
a distinct belief, resolved independently of the trade, so it is learnable on
its own timetable (`expected_onset_minutes`). ACTIONABLE reads that carry a
`state_change` get a STATE_CHANGE_PRED journal row at signal time.

Resolution is a pure review function: it pairs each prediction with the
nearest LATER observation of the same meter and scores what happened. `correct`
is intentionally a strict word match between the predicted transition's state
and how the meter actually arrived (or `None` if the horizon elapsed or data
never exists — absence is scored as unknown, never as a hit).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

PRED_KIND = "STATE_CHANGE_PRED"


@dataclass
class StateChangeResolution:
    pred_t0: str
    meter_key: str
    predicted: str
    probability: float
    outcome: str                      # "MATCH" | "MISS" | "ELAPSED" | "UNOBSERVED"
    actual: Optional[str] = None
    latency_minutes: Optional[float] = None

    def score(self) -> float:
        if self.outcome == "MATCH":
            return self.probability
        if self.outcome == "MISS":
            return 1.0 - self.probability
        return 0.5


def book_into_ledger(ledger, compiled, as_of: datetime,
                     decided_by: str = "MODEL") -> bool:
    sc = getattr(compiled, "state_change", None)
    if not sc or getattr(sc, "transition", None) is None:
        return False
    ledger.append_raw({
        "kind": PRED_KIND,
        "t0": as_of.isoformat(),
        "symbol": str(getattr(compiled, "symbol", "XAUUSD")),
        "decision": {
            "meter_key": getattr(sc, "meter_key", None),
            "current_state": getattr(sc, "current_state", None),
            "transition": sc.transition,
            "probability": getattr(sc, "probability", None),
            "onset_minutes": getattr(sc, "expected_onset_minutes", None),
            "is_complete": bool(getattr(sc, "is_complete", False)),
        },
        "decided_by": decided_by,
        "reason": "state-change prediction booked alongside ACTIONABLE signal",
        "notes": [],
    })
    return True


def resolve(rows: Sequence[dict],
            observed: Callable[[dict, str], Optional[dict]],
            *,
            horizon_scale: float = 4.0) -> list[StateChangeResolution]:
    """Pair every prediction with the next observation of its meter.

    `observed(row, meter_key)` returns the meter's dict at that row (e.g. the
    context's volatility_state), or None. The horizon is `onset_minutes` scaled
    by `horizon_scale` (default: a 15-minute onset booking expects the state
    within 60 minutes, matching a 4x margin for "about to happen").
    """
    out: list[StateChangeResolution] = []
    preds = [r for r in rows if r.get("kind") == PRED_KIND]
    for p in preds:
        dec = p.get("decision", {})
        meter = dec.get("meter_key")
        onset = dec.get("onset_minutes")
        prob = 0.0
        try:
            prob = float(dec.get("probability") or 0.0)
        except (TypeError, ValueError):
            pass
        horizon_m = (float(onset) if isinstance(onset, (int, float)) else 30.0) * horizon_scale
        horizon = datetime.fromisoformat(p["t0"]).replace(tzinfo=timezone.utc) + timedelta(minutes=horizon_m)

        resolution = StateChangeResolution(
            pred_t0=p["t0"], meter_key=meter or "?",
            predicted=str(dec.get("transition") or ""), probability=prob,
            outcome="UNOBSERVED")
        for later in rows:
            if later is p:
                continue
            later_t = later.get("t0")
            if not later_t or later_t <= p["t0"]:
                continue
            try:
                later_dt = datetime.fromisoformat(str(later_t)).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if later_dt > horizon:
                resolution.outcome = "ELAPSED"
                break
            obs = observed(later, meter)
            if obs is None:
                continue
            actual = str(obs.get("state") or obs.get("value") or "")
            resolution.actual = actual or None
            resolution.latency_minutes = (later_dt - datetime.fromisoformat(p["t0"]).replace(
                tzinfo=timezone.utc)).total_seconds() / 60.0
            resolution.outcome = "MATCH" if _is_match(dec, obs, actual) else "MISS"
            break
        out.append(resolution)
    return out


def _is_match(dec: dict, obs: dict, actual: str) -> bool:
    pred = str(dec.get("transition") or "").lower()
    if not actual:
        return False
    actual_l = actual.lower()
    if actual_l in pred or pred in actual_l:
        return True
    words = predicate_words(pred)
    return any(w and w in actual_l for w in words)


def predicate_words(transition: str) -> list[str]:
    """The state words inside a transition phrase ("compression breaks" -> break)."""
    for marker in ("->", " from ", " to ", " towards ", " breaks ", " expand"):
        transition = transition.replace(marker, " ")
    words = [w.strip(".,") for w in transition.split()]
    return [w for w in words if len(w) > 3 and w not in ("state", "market")]


def brier(resolutions: Sequence[StateChangeResolution]) -> Optional[float]:
    scored = [r for r in resolutions if r.outcome in ("MATCH", "MISS")]
    if not scored:
        return None
    return sum(r.score() for r in scored) / len(scored)