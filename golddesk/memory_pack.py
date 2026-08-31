"""Causal analogue memory for the analyst brief.

The ledger is the source of truth. This module selects prior *closed* trades
whose deterministic context resembles the current state and renders a compact
pack. A case is eligible only when its close timestamp precedes the brief's
as-of time; using an entry that was still open would leak an outcome that was
not yet knowable.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

MEMORY_PACK_VERSION = "memory-pack-2026-08-31-a"

_FIELDS = ("trend_direction", "trend_health", "trend_maturity",
           "volatility_state", "htf_alignment", "session")


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return out.replace(tzinfo=out.tzinfo or timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Analogue:
    entry_t0: str
    closed_ts: str
    direction: str
    setup: str
    mechanism: str
    realised_r: float
    mfe_r: Optional[float]
    mae_r: Optional[float]
    close_reason: str
    similarity: float
    matched: tuple[str, ...]

    @property
    def diagnosis(self) -> str:
        """Separate a bad read from a good read managed badly."""
        if self.realised_r <= 0 and self.mfe_r is not None:
            if self.mfe_r >= 1.0:
                return "MANAGEMENT GIVEBACK — direction paid before the close"
            if self.mfe_r < 0.5:
                return "THESIS/TIMING FAILURE — never produced +0.5R"
        if self.realised_r > 0:
            return "PAID"
        return "OUTCOME INCONCLUSIVE"


@dataclass(frozen=True)
class MemoryPack:
    as_of_utc: datetime
    eligible_n: int
    analogues: tuple[Analogue, ...]

    def render(self) -> str:
        if not self.analogues:
            return ""
        rs = [a.realised_r for a in self.analogues]
        wins = sum(r > 0 for r in rs)
        lines = [
            "PRIOR CLOSED DECISIONS MOST LIKE THIS STATE",
            (f"Causal only: {len(rs)} shown from {self.eligible_n} eligible; "
             f"mean {statistics.fmean(rs):+.2f}R, wins {wins}/{len(rs)}. "
             "Similarity is context matching, not a vote or forecast."),
        ]
        for a in self.analogues:
            excursion = ("MFE/MAE unmeasured" if a.mfe_r is None else
                         f"MFE {a.mfe_r:+.2f}R / MAE "
                         f"{a.mae_r:+.2f}R" if a.mae_r is not None else
                         f"MFE {a.mfe_r:+.2f}R / MAE unmeasured")
            lines.append(
                f"  {a.entry_t0[:16]}  {a.direction:<5} {a.setup:<18} "
                f"{a.realised_r:+.2f}R  similarity {a.similarity:.2f}  "
                f"mechanism={a.mechanism}\n"
                f"    {a.diagnosis}; {excursion}; close={a.close_reason}")
        lines.append(
            "Do not learn 'trade less' from a loss: distinguish wrong direction/"
            "timing from a profitable path that management gave back.")
        lines.append("The deterministic compiler and risk gates remain final.")
        return "\n".join(lines)


def build_memory_pack(rows: Sequence[dict], brief, limit: int = 5) -> MemoryPack:
    """Select nearest prior completed decisions without crossing `as_of_utc`."""
    current = dict(brief.context.__dict__) | {"session": brief.session}
    candidates: list[tuple[float, datetime, Analogue]] = []
    for row in rows:
        if row.get("kind") != "TRADE_CLOSED":
            continue
        closed = _dt(row.get("ts"))
        entered = _dt(row.get("entry_t0"))
        realised = row.get("realised_r")
        if (closed is None or entered is None or closed >= brief.as_of_utc
                or not isinstance(realised, (int, float))):
            continue
        context = row.get("context") or {}
        available = [f for f in _FIELDS if context.get(f) is not None]
        if not available:
            continue
        matched = tuple(f for f in available
                        if str(context.get(f)) == str(current.get(f)))
        similarity = len(matched) / len(available)
        if len(matched) < 2:
            continue
        candidates.append((similarity, closed, Analogue(
            entered.isoformat(), closed.isoformat(),
            str(row.get("direction") or "NONE"),
            str(row.get("setup") or "UNKNOWN"),
            str(row.get("mechanism_name") or "unnamed"),
            float(realised),
            float(row["mfe_r"]) if isinstance(row.get("mfe_r"), (int, float)) else None,
            float(row["mae_r"]) if isinstance(row.get("mae_r"), (int, float)) else None,
            str(row.get("reason") or "UNMEASURED"), similarity, matched)))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return MemoryPack(brief.as_of_utc, len(candidates),
                      tuple(x[2] for x in candidates[:max(0, limit)]))
