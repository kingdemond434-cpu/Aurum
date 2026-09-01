"""Decision + counterfactual ledger — every decision, and what happened next.

Three properties this file exists to guarantee:

1. EVERY decision is recorded, not just the ones that became trades. A refusal
   is a decision. A HOLD is a decision. The desk cannot measure false negatives
   from a table that only contains its yeses.

2. Outcomes are resolved MECHANICALLY from price, at several horizons, with
   full excursion. Never self-scored, never summarised by the thing being
   scored.

3. The underlying forward PATH is preserved by reference, not just its summary
   statistics. Today's definition of "worked" will be wrong in six months. A
   ledger of summaries freezes today's mistake; a ledger of path references
   lets research recompute any definition later against the same bars.

Point 3 has teeth on this desk specifically: the bar cache was found writing
unfinished bars, so a path resolved on Monday can differ from the same path
re-read on Tuesday. PathRef therefore carries a content hash — if resolution is
replayed and the hash moved, the row is flagged rather than silently trusted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

log = logging.getLogger(__name__)

# Horizons resolved for every decision. Add to the end — never reorder or
# repurpose, or historical rows stop meaning what they say.
HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("m5", timedelta(minutes=5)),
    ("m15", timedelta(minutes=15)),
    ("m30", timedelta(minutes=30)),
    ("h1", timedelta(hours=1)),
    ("h4", timedelta(hours=4)),
    ("session", timedelta(hours=8)),
)


class DecisionKind(str, Enum):
    SIGNAL = "SIGNAL"                # compiled and sent
    REFUSAL_MODEL = "REFUSAL_MODEL"  # analyst said NO_SETUP
    REFUSAL_COMPILER = "REFUSAL_COMPILER"   # geometry/cost/drift gate
    REFUSAL_ROUTER = "REFUSAL_ROUTER"       # empirical cohort veto
    MANAGEMENT = "MANAGEMENT"        # in-trade action


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class PathRef:
    """Points at the bars used, so any future definition can be recomputed."""
    symbol: str
    timeframe: str
    t0: datetime
    t_end: datetime
    bar_count: int
    content_sha256: str

    @staticmethod
    def of(symbol: str, timeframe: str, bars: Sequence[Bar]) -> "PathRef":
        h = hashlib.sha256()
        for b in bars:
            h.update(f"{b.ts.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close};".encode())
        return PathRef(symbol, timeframe, bars[0].ts, bars[-1].ts, len(bars),
                       h.hexdigest())


@dataclass(frozen=True)
class ForwardOutcome:
    """Mechanically resolved. Direction-normalised into R where a risk unit exists."""
    returns_r: dict[str, Optional[float]]   # horizon -> R move in the trade's favour
    returns_price: dict[str, Optional[float]]
    mfe_r: Optional[float]
    mae_r: Optional[float]
    time_to_mfe_s: Optional[float]
    time_to_mae_s: Optional[float]
    best_achievable_r: Optional[float]      # MFE, the ceiling any exit could reach
    resolved_at: str
    incomplete: bool                        # horizon ran past available bars


def resolve_forward(
    bars: Sequence[Bar],
    t0: datetime,
    reference_price: float,
    direction: Literal["LONG", "SHORT", "NONE"],
    risk_price: Optional[float],
) -> ForwardOutcome:
    """Resolve a decision's forward path. Works for refusals too.

    For a refusal, pass the direction the analyst DECLINED to take (or the
    direction a setup would have had) and the risk unit the compiler would have
    used. That converts "we said no" into "we said no and it would have paid
    +2.1R", which is the only form that supports false-negative analysis.
    """
    fwd = [b for b in bars if b.ts >= t0]
    if not fwd:
        raise ValueError("no forward bars at or after t0")

    sign = 1.0 if direction != "SHORT" else -1.0
    unit = risk_price if risk_price and risk_price > 0 else None

    def to_r(price_move: float) -> Optional[float]:
        return (price_move / unit) if unit else None

    returns_price: dict[str, Optional[float]] = {}
    returns_r: dict[str, Optional[float]] = {}
    incomplete = False
    horizon_end = fwd[-1].ts
    for name, delta in HORIZONS:
        target = t0 + delta
        if target > horizon_end:
            returns_price[name] = None
            returns_r[name] = None
            incomplete = True
            continue
        bar = max((b for b in fwd if b.ts <= target), key=lambda b: b.ts)
        move = (bar.close - reference_price) * sign
        returns_price[name] = round(move, 4)
        returns_r[name] = None if to_r(move) is None else round(to_r(move), 4)

    # Excursion over the full retained window.
    best = worst = 0.0
    t_best = t_worst = fwd[0].ts
    for b in fwd:
        up = (b.high - reference_price) * sign
        dn = (b.low - reference_price) * sign
        if sign < 0:
            up, dn = (reference_price - b.low) , (reference_price - b.high)
        if up > best:
            best, t_best = up, b.ts
        if dn < worst:
            worst, t_worst = dn, b.ts

    return ForwardOutcome(
        returns_r=returns_r,
        returns_price=returns_price,
        mfe_r=None if to_r(best) is None else round(to_r(best), 4),
        mae_r=None if to_r(worst) is None else round(to_r(worst), 4),
        time_to_mfe_s=(t_best - t0).total_seconds(),
        time_to_mae_s=(t_worst - t0).total_seconds(),
        best_achievable_r=None if to_r(best) is None else round(to_r(best), 4),
        resolved_at=datetime.now(timezone.utc).isoformat(),
        incomplete=incomplete,
    )


def resolve_trade(bars: Sequence[Bar], t0: datetime, entry: float, stop: float,
                  target: float, direction: str) -> tuple[float, str]:
    """First-touch resolution in R. The honest way a trade actually ends.

    Walks forward bar by bar and returns whichever level price reached first.
    A bar spanning both is resolved as the STOP — the pessimistic assumption,
    because intrabar order is unknowable from OHLC alone.
    """
    long = direction == "LONG"
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0, "INVALID"
    for b in (x for x in bars if x.ts > t0):
        hit_stop = (b.low <= stop) if long else (b.high >= stop)
        hit_tgt = (b.high >= target) if long else (b.low <= target)
        if hit_stop:
            return -1.0, "STOP"
        if hit_tgt:
            return abs(target - entry) / risk, "TARGET"
    last = bars[-1].close
    return (((last - entry) if long else (entry - last)) / risk), "OPEN_AT_END"


@dataclass
class DecisionRecord:
    """One row. Signal, refusal, or management action — same shape for all."""
    decision_id: str
    kind: DecisionKind
    t0: datetime
    symbol: str
    # Everything the decision saw. Enough to replay it against another model.
    context: dict[str, Any]
    brief_render: str
    # What was decided, and by whom.
    decided_by: Literal["MODEL", "COMPILER", "ROUTER", "POLICY"]
    decision: dict[str, Any]
    reason: str
    # Filled in later by the resolver.
    path_ref: Optional[PathRef] = None
    outcome: Optional[ForwardOutcome] = None
    setup_confirmed_later: Optional[bool] = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        d["t0"] = self.t0.isoformat()
        d["kind"] = self.kind.value
        if self.path_ref:
            d["path_ref"]["t0"] = self.path_ref.t0.isoformat()
            d["path_ref"]["t_end"] = self.path_ref.t_end.isoformat()
        return json.dumps(d, default=str)


class Ledger:
    """Append-only JSONL. One file, every decision, forever."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: DecisionRecord) -> None:
        with self.path.open("a") as fh:
            fh.write(rec.to_json() + "\n")

    def append_raw(self, row: dict) -> None:
        """Journal a non-decision event — a close, a management trace.

        Same file, because the whole value of an append-only ledger is that
        the ordering between "what was decided" and "what happened next" is
        preserved in one place. Rows carry `kind`, so readers filter.
        """
        with self.path.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def tail(self, n: int = 2000) -> list[dict]:
        """The last n rows, read without re-parsing the whole file.

        The desk's memory/replay scans are bounded on purpose: every wake re-reads
        the ledger in on_bar, and over a long backtest a full read_all per wake is
        quadratic. Reading backwards from the end keeps each wake O(n) regardless
        of how many rows the simulation has accumulated or how large each row is
        (rows embeds analyst model dumps, so they grow over time).
        """
        if not self.path.exists():
            return []
        thr = max(n, 32)
        block = 1 << 17                     # 128 KiB chunk, grown until enough lines
        size = self.path.stat().st_size
        chunks: list[bytes] = []
        offset = size
        lines_so_far = 0
        with self.path.open("rb") as fh:
            while offset > 0 and lines_so_far < thr:
                start = max(offset - block, 0)
                fh.seek(start)
                chunk = fh.read(offset - start)
                chunks.append(chunk)
                lines_so_far += chunk.count(b"\n")
                offset = start
        buf = b"".join(reversed(chunks))
        lines = buf.splitlines()
        if not lines:
            return []
        lines = lines[-n:]
        if offset > 0:
            try:
                json.loads(lines[0])        # back-edge fragment only if this fails
            except Exception:               # noqa: BLE001
                lines = lines[1:]
        out = []
        for l in lines:
            if l.strip():
                try:
                    out.append(json.loads(l))
                except Exception:           # noqa: BLE001 — partial line, skip
                    continue
        return out

    def unresolved(self) -> list[dict]:
        return [r for r in self.read_all() if r.get("outcome") is None]


def verify_path_stable(rec: DecisionRecord, bars: Sequence[Bar]) -> tuple[bool, str]:
    """Re-hash the bars a row was resolved on. Catches a mutating cache.

    The desk's parquet cache was observed persisting unfinished bars, so this
    is not hypothetical: a row resolved against a partial bar will silently
    disagree with the same row re-resolved after the bar completed.
    """
    if rec.path_ref is None:
        return False, "no path_ref recorded"
    now = PathRef.of(rec.path_ref.symbol, rec.path_ref.timeframe, bars)
    if now.content_sha256 != rec.path_ref.content_sha256:
        return False, (f"path changed since resolution "
                       f"({rec.path_ref.bar_count} -> {now.bar_count} bars)")
    return True, "stable"


# --------------------------------------------------------------------------
# False-negative analysis — the reason the refusal rows exist
# --------------------------------------------------------------------------

def false_negative_report(rows: Sequence[dict], min_r: float = 1.0) -> dict:
    """Which refusal classes cost the most? Group by reason, not by feeling."""
    by_reason: dict[str, list[float]] = {}
    for r in rows:
        if not str(r.get("kind", "")).startswith("REFUSAL"):
            continue
        out = r.get("outcome") or {}
        best = out.get("best_achievable_r")
        if best is None:
            continue
        by_reason.setdefault(r.get("reason", "unknown")[:70], []).append(best)

    summary = {}
    for reason, vals in by_reason.items():
        missed = [v for v in vals if v >= min_r]
        summary[reason] = {
            "n": len(vals),
            "missed_ge_min": len(missed),
            "missed_rate": round(len(missed) / len(vals), 3),
            "mean_best_r": round(sum(vals) / len(vals), 3),
            "total_forgone_r": round(sum(missed), 2),
        }
    return dict(sorted(summary.items(),
                       key=lambda kv: -kv[1]["total_forgone_r"]))
