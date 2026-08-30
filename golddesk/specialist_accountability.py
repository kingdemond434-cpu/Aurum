"""Durable specialist verdicts and evidence-backed desk scorecards.

Specialists are shadow alternatives, never a voting body.  Every seat reads the
same CausalSnapshot, every read is appended to the decision ledger, and a seat
earns standing only from states where following it would have changed the
desk's action and improved the paired forward outcome after cost.

Standing grants one privilege: the current read may be shown to the analyst as
measured context.  It never grants authority over prices, geometry, risk, or the
compiler.  The deterministic compiler remains the final decision boundary.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .ledger import DecisionKind, Ledger
from .specialists import (DEFAULT_CHANGE_COST_R, MIN_CHANGED, MarginalValue,
                          SpecialistRead, marginal_value)

VERDICT_KIND = "SPECIALIST_VERDICT"
ACCOUNTABILITY_VERSION = "specialist-accountability-2026-08-30-a"

_HORIZON_BY_BARS = ((1, "m15"), (2, "m30"), (4, "h1"),
                    (16, "h4"), (32, "session"))


def _horizon(horizon_bars: int) -> str:
    for cap, name in _HORIZON_BY_BARS:
        if horizon_bars <= cap:
            return name
    return "session"


def _action(row: dict) -> str:
    """The action Aurum actually took, not the proposal it refused."""
    if row.get("kind") != DecisionKind.SIGNAL.value:
        return "FLAT"
    direction = str((row.get("decision") or {}).get("direction") or "FLAT")
    return direction if direction in ("LONG", "SHORT") else "FLAT"


def _outcome_direction(row: dict) -> str:
    dec = row.get("decision") or {}
    direction = str(dec.get("outcome_direction") or dec.get("direction")
                    or dec.get("declined") or "LONG")
    return direction if direction in ("LONG", "SHORT") else "LONG"


def _up_move_r(row: dict, horizon: str) -> Optional[float]:
    """Convert the row's direction-normalised return back to an upward move."""
    value = ((row.get("outcome") or {}).get("returns_r") or {}).get(horizon)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    sign = -1.0 if _outcome_direction(row) == "SHORT" else 1.0
    return float(value) * sign


def _payoff(action: str, up_move_r: float) -> float:
    if action == "LONG":
        return up_move_r
    if action == "SHORT":
        return -up_move_r
    return 0.0


def _regime(row: dict) -> str:
    c = row.get("context") or {}
    return "|".join(str(c.get(k) or "UNKNOWN") for k in
                    ("trend_direction", "volatility_state", "session"))


def _decision_index(rows: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """Latest final decision per causal state and exact content."""
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row.get("kind") not in {k.value for k in DecisionKind}:
            continue
        dec = row.get("decision") or {}
        sid, ch = dec.get("state_id"), dec.get("content_hash")
        if sid and ch:
            out[(str(sid), str(ch))] = row
    return out


def verdict_row(snapshot, read: SpecialistRead) -> dict:
    """One permanent, self-contained record for one seat on one state."""
    return {
        "kind": VERDICT_KIND,
        "version": ACCOUNTABILITY_VERSION,
        "verdict_id": f"{snapshot.state_id}|{read.name}",
        "state_id": snapshot.state_id,
        "content_hash": snapshot.content_hash,
        "as_of_utc": snapshot.as_of_utc.isoformat(),
        "specialist": read.name,
        "role": read.role,
        "available": read.available,
        "direction": read.direction,
        "strength": read.strength,
        "probability_up": read.p_up,
        "probability_basis": ("reported" if read.probability_up is not None
                              else "derived_from_bounded_strength"),
        "horizon_bars": read.horizon_bars,
        "why": read.why,
        "meta": dict(read.meta),
    }


def record_verdicts(ledger: Ledger, snapshot, report: dict,
                    existing_rows: Optional[Sequence[dict]] = None) -> list[dict]:
    """Append each state/seat verdict once, including across process restarts."""
    rows = [verdict_row(snapshot, r) for r in report.get("reads", ())]
    history = ledger.read_all() if existing_rows is None else existing_rows
    seen = {str(r.get("verdict_id")) for r in history
            if r.get("kind") == VERDICT_KIND and r.get("verdict_id")}
    written = []
    for row in rows:
        if row["verdict_id"] in seen:
            continue
        ledger.append_raw(row)
        seen.add(row["verdict_id"])
        written.append(row)
    return written


def decision_stamp(report: Optional[dict]) -> dict:
    """Link a final signal/refusal to the exact specialist state it followed."""
    if not report:
        return {}
    return {
        "state_id": report.get("state_id"),
        "content_hash": report.get("content_hash"),
        "specialist_verdicts": [
            {"specialist": r.name, "role": r.role, "available": r.available,
             "direction": r.direction, "strength": r.strength,
             "probability_up": r.p_up, "horizon_bars": r.horizon_bars}
            for r in report.get("reads", ())
        ],
        "specialist_agreement": report.get("agreement"),
        "specialist_authority": "ADVISORY_ONLY_COMPILER_FINAL",
    }


@dataclass(frozen=True)
class RegimeValue:
    changed_n: int
    incremental_net_r: float
    mean_r_per_change: float


@dataclass(frozen=True)
class SpecialistScorecard:
    specialist: str
    role: str
    states_seen: int
    available_n: int
    resolved_n: int
    changed_n: int
    incremental_net_r: Optional[float]
    mean_r_per_change: Optional[float]
    t_stat: Optional[float]
    brier: Optional[float]
    desk_brier: Optional[float]
    brier_improvement: Optional[float]
    regime_value: dict[str, RegimeValue] = field(default_factory=dict)
    standing: str = "SHADOW"
    why: str = ""

    @property
    def has_standing(self) -> bool:
        return self.standing == "EARNED"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["regime_value"] = {k: asdict(v) for k, v in self.regime_value.items()}
        return d

    def render(self) -> str:
        net = "n/a" if self.incremental_net_r is None else f"{self.incremental_net_r:+.2f}R"
        bp = ("n/a" if self.brier_improvement is None
              else f"{self.brier_improvement:+.4f}")
        return (f"{self.specialist:<12} {self.standing:<9} states {self.states_seen:>4}  "
                f"available {self.available_n:>4}  changed {self.changed_n:>3}  "
                f"net {net:>9}  Brier lift {bp}")


def _score_one(name: str, verdicts: Sequence[dict], decisions: dict) -> SpecialistScorecard:
    role = next((str(v.get("role") or "") for v in verdicts if v.get("role")), "")
    available = [v for v in verdicts if v.get("available")]
    with_spec: list[str] = []
    without_spec: list[str] = []
    spec_r: list[float] = []
    desk_r: list[float] = []
    probs: list[float] = []
    desk_probs: list[float] = []
    labels: list[float] = []
    regime_deltas: dict[str, list[float]] = {}

    for v in available:
        row = decisions.get((str(v.get("state_id")), str(v.get("content_hash"))))
        if row is None:
            continue
        horizon = _horizon(int(v.get("horizon_bars") or 1))
        up = _up_move_r(row, horizon)
        if up is None:
            continue
        specialist_action = str(v.get("direction") or "FLAT")
        if specialist_action not in ("LONG", "SHORT", "FLAT"):
            continue
        desk_action = _action(row)
        sr, dr = _payoff(specialist_action, up), _payoff(desk_action, up)
        with_spec.append(specialist_action)
        without_spec.append(desk_action)
        spec_r.append(sr)
        desk_r.append(dr)

        if up != 0:
            p = float(v.get("probability_up", 0.5))
            label = 1.0 if up > 0 else 0.0
            # The actual desk action is the honest comparison.  FLAT carries no
            # directional probability; LONG/SHORT use only bounded analyst
            # confidence when it was journalled, otherwise a coarse 0.75/0.25.
            dec = row.get("decision") or {}
            ar = dec.get("analyst_read") or {}
            conf = ar.get("confidence")
            strength = (max(0.0, min(1.0, float(conf) / 5.0))
                        if isinstance(conf, (int, float)) else 0.5)
            dp = (0.5 if desk_action == "FLAT" else
                  0.5 + strength / 2.0 if desk_action == "LONG" else
                  0.5 - strength / 2.0)
            probs.append(p); desk_probs.append(dp); labels.append(label)

        if specialist_action != desk_action:
            delta = sr - dr - DEFAULT_CHANGE_COST_R
            regime_deltas.setdefault(_regime(row), []).append(delta)

    mv: MarginalValue = marginal_value(
        name, with_spec, without_spec, spec_r, desk_r,
        cost_r=DEFAULT_CHANGE_COST_R, min_changed=MIN_CHANGED)
    brier = (statistics.fmean((p - y) ** 2 for p, y in zip(probs, labels))
             if probs else None)
    desk_brier = (statistics.fmean((p - y) ** 2 for p, y in zip(desk_probs, labels))
                  if desk_probs else None)
    lift = (desk_brier - brier
            if brier is not None and desk_brier is not None else None)
    regimes = {
        k: RegimeValue(len(ds), sum(ds), statistics.fmean(ds))
        for k, ds in sorted(regime_deltas.items())
    }

    if mv.has_standing and (lift is None or lift >= 0):
        standing = "EARNED"
        why = ("paired changed decisions paid after cost and calibration did "
               "not underperform the desk; admit as analyst context only")
    elif mv.verdict == "NEGATIVE" or (lift is not None and lift < 0 and
                                      mv.n_changed >= MIN_CHANGED):
        standing = "REVOKED"
        why = ("changed decisions or calibration underperformed; keep out of "
               "the analyst brief")
    else:
        standing = "SHADOW"
        why = mv.why
    return SpecialistScorecard(
        name, role, len(verdicts), len(available), len(with_spec), mv.n_changed,
        mv.net_r, mv.mean_r_per_change, mv.t_stat, brier, desk_brier, lift,
        regimes, standing, why)


def scorecards(rows: Sequence[dict]) -> list[SpecialistScorecard]:
    decisions = _decision_index(rows)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("kind") == VERDICT_KIND and row.get("specialist"):
            grouped.setdefault(str(row["specialist"]), []).append(row)
    return [_score_one(name, vs, decisions) for name, vs in sorted(grouped.items())]


def earned_brief_block(report: Optional[dict], cards: Sequence[SpecialistScorecard]) -> str:
    """Render only seats with earned standing; disagreement remains separate."""
    if not report:
        return ""
    earned = {c.specialist: c for c in cards if c.has_standing}
    reads = [r for r in report.get("reads", ()) if r.available and r.name in earned]
    if not reads:
        return ""
    lines = [
        "SPECIALIST READS WITH EARNED STANDING (advisory, never votes)",
        "Each read saw the exact snapshot hash shown below. Disagreement is preserved.",
        f"SNAPSHOT {report.get('state_id')}  CONTENT {report.get('content_hash')}",
    ]
    for read in reads:
        card = earned[read.name]
        lines.append(
            f"  {read.name} / {read.role}: {read.direction} strength {read.strength:.2f}; "
            f"changed n={card.changed_n}, net={card.incremental_net_r:+.2f}R; {read.why}")
    lines.append("The DETERMINISTIC COMPILER remains final authority over every number and veto.")
    return "\n".join(lines)


def gate_id(kind: Any, reason: str, decision: Optional[dict] = None) -> Optional[str]:
    """Stable attribution key for every refusal, including analyst passes."""
    kval = kind.value if hasattr(kind, "value") else str(kind)
    if not kval.startswith("REFUSAL"):
        return None
    reason_l = reason.lower()
    explicit = (decision or {}).get("gate_id")
    if explicit:
        return str(explicit)
    mapping = (
        ("no_setup", "analyst.no_setup"),
        ("re-entry blocked", "reentry.policy"),
        ("hierarchical bias", "entry.hierarchical_bias"),
        ("expectancy gate", "entry.expectancy_gate"),
        ("edge router", "entry.cohort_router"),
        ("stale tick", "risk.stale_tick"),
        ("spread", "entry.spread_fraction"),
        ("drift", "entry.drift_anti_chase"),
        ("risk:", "risk.portfolio_heat"),
        ("one-position", "risk.one_position"),
        ("enforcing hypothesis", "entry.hypothesis_veto"),
        ("level", "entry.geometry"),
        ("inverted", "entry.geometry"),
    )
    return next((gid for needle, gid in mapping if needle in reason_l),
                "refusal.unclassified")


def render_dashboard(rows: Sequence[dict], latest_report: Optional[dict] = None) -> str:
    """Operator view backed entirely by durable evidence, never an edge badge."""
    cards = scorecards(rows)
    lines = ["AURUM SPECIALIST DESK", "NO VOTING — DETERMINISTIC COMPILER FINAL", ""]
    if latest_report:
        lines.append(f"state {latest_report.get('state_id')}")
        lines.append(f"content {latest_report.get('content_hash')}")
        lines.append(f"disagreement {latest_report.get('agreement')}")
        lines.append("")
    if not cards:
        lines.append("No specialist verdict history yet.")
    else:
        lines.extend(c.render() for c in cards)

    try:
        from .constitution import measure
        gates = measure(rows)
    except Exception:                              # dashboard cannot affect trading
        gates = []
    if gates:
        lines += ["", "REFUSAL / GATE ACCOUNTABILITY"]
        lines.extend(g.render() for g in gates)
    return "\n".join(lines)
