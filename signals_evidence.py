"""Turn captured external calls into evidence. Measurement only — decides nothing.

`signals_capture.py` records what was said. This reconstructs what actually
happened, against your own broker data, and profiles the sources. Together they
implement §13, §15 and §20 of the mandate. Nothing here is imported by any
module in `golddesk`, and nothing here gates a trade.

THE THREE THINGS IT MEASURES, AND WHY EACH IS THE HONEST VERSION

  1. RECONSTRUCTION (§13). A provider's screenshot is not an outcome. Every
     call is replayed on your broker's series: was the entry ever reachable
     after the message arrived, when was it touched, which of stop/target came
     first, and at what resolution is that known. A call whose entry never
     traded is NOT_FILLED — which is an outcome, not a row to discard. Dropping
     unfilled calls silently flatters every provider who posts unreachable
     entries.

  2. DEPENDENCE (§15). Ten channels calling SHORT is often one analyst and nine
     reposts. Lead/lag, identical levels and text overlap are computed pairwise
     so that apparent consensus can be discounted to effective independent
     sources. This is arithmetic on observed messages, not a threshold on a
     trade decision.

  3. SOURCE LEDGER (§20). Per source, conditioned on session and signal age.
     Ranked by nothing. `incremental_value_to_aurum` is deliberately left
     unfilled and REFUSES to be estimated from this data alone — it requires
     paired same-state Aurum runs, which do not exist yet.

WHAT IT DELIBERATELY WILL NOT PRODUCE

  The mandate's §16 "EXTERNAL GOLD INTELLIGENCE STATE" — directional pressure,
  effective independent source count, information half-life, conditional
  incremental EV — is not built here, and §27 is the reason. Every one of those
  is a derived scalar with a tuned cutoff underneath it, sitting directly
  upstream of a trade decision. That is a confluence engine with better
  vocabulary. The fields below are per-source facts an analyst can read; the
  synthesis belongs to the model.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

UTC = timezone.utc
EVIDENCE_VERSION = "sigev-2026-08-14-a"


# --------------------------------------------------------------------------
# Source registry — metadata only. No scores, no tiers earned by size.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceMeta:
    """What a source IS. Never what it is worth.

    `subscribers` is recorded because it drives DISCOVERY PRIORITY and nothing
    else (§26). It must never appear in a weighting, and it does not appear in
    any computation in this file.
    """
    source_id: str
    url: str = ""
    platform: str = "telegram"
    language: str = "unknown"
    region: str = "unknown"
    subscribers: Optional[int] = None
    notes: str = ""


# --------------------------------------------------------------------------
# §13 Independent reconstruction
# --------------------------------------------------------------------------

@dataclass
class Reconstruction:
    signal_id: str
    source_id: str
    received_utc: str
    direction: Optional[str]
    entry: Optional[float]
    sl: Optional[float]
    tps: list = field(default_factory=list)

    # feasibility
    fillable: bool = False
    fill_utc: Optional[str] = None
    minutes_to_fill: Optional[float] = None
    # How far price had ALREADY run in the call's own direction when it landed,
    # in R. Positive means the entry sat behind the market: the call was posted
    # after the move it describes. This is the number that replaces arguing.
    chase_r: Optional[float] = None
    pre_move_r: Optional[float] = None      # move in the 30m BEFORE the message

    # outcome
    outcome: str = "UNEVALUATED"            # TARGET / STOP / OPEN / NOT_FILLED
    resolution: str = "UNRESOLVED"
    net_r: Optional[float] = None
    mfe_r: Optional[float] = None
    mae_r: Optional[float] = None
    minutes_to_mfe: Optional[float] = None
    minutes_to_mae: Optional[float] = None

    spread_at_receipt: Optional[float] = None
    session: str = "UNKNOWN"
    note: str = ""


def _session_of(ts: datetime) -> str:
    h = ts.astimezone(UTC).hour
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 12:
        return "LONDON"
    if 12 <= h < 17:
        return "OVERLAP"
    if 17 <= h < 21:
        return "NY"
    return "LATE"


def reconstruct(sig: dict, series: Sequence[tuple[datetime, float, float]],
                *, resolution_label: str = "M1_OBSERVED",
                max_hold_minutes: int = 60 * 24,
                spread_cost: Optional[float] = None) -> Reconstruction:
    """Replay ONE call on an ordered broker series of (ts, bid, ask).

    First touch decides the outcome, because that is what would have happened.
    The series must be ordered and must begin at or before the message.
    """
    p = sig.get("parsed") or {}
    rec = Reconstruction(
        signal_id=str(sig.get("message_id")),
        source_id=str(sig.get("channel")),
        received_utc=str(sig.get("received_utc")),
        direction=p.get("direction"), entry=p.get("entry"),
        sl=p.get("sl"), tps=list(p.get("tps") or []))

    if not p.get("complete"):
        rec.note = "incomplete parse — no entry/stop to reconstruct"
        return rec

    t0 = datetime.fromisoformat(rec.received_utc)
    rec.session = _session_of(t0)
    long = rec.direction == "LONG"
    entry, sl = float(rec.entry), float(rec.sl)
    risk = abs(entry - sl)
    if risk <= 0:
        rec.note = "zero risk distance in the call itself"
        return rec
    tp = max(rec.tps) if (rec.tps and long) else (min(rec.tps) if rec.tps else None)

    fwd = [(ts, b, a) for ts, b, a in series if ts >= t0]
    if not fwd:
        rec.note = "no broker data at or after receipt"
        return rec

    # quote at receipt, and how far the move had already gone
    ts0, bid0, ask0 = fwd[0]
    mid0 = (bid0 + ask0) / 2.0
    rec.spread_at_receipt = round(ask0 - bid0, 4)
    rec.chase_r = round(((mid0 - entry) if long else (entry - mid0)) / risk, 4)
    before = [(ts, b, a) for ts, b, a in series if t0 - timedelta(minutes=30) <= ts < t0]
    if before:
        m_first = (before[0][1] + before[0][2]) / 2.0
        rec.pre_move_r = round(((mid0 - m_first) if long else (m_first - mid0)) / risk, 4)

    # ---- fill -----------------------------------------------------------
    # A LONG fills when the ASK trades down to the entry; a SHORT when the BID
    # trades up to it. Using mid here would manufacture fills that the spread
    # would have denied.
    # The entry level may sit either side of the market when the call lands, and
    # the two mean different orders. "BUY 2050" with gold at 2002 is a breakout
    # entry that must WAIT for price to rise; "BUY 1995" is a limit that waits
    # for a dip. Testing only `ask <= entry` filled every above-market call
    # instantly at receipt, which handed unreachable levels a real outcome and
    # made a provider posting fantasy entries look like one posting good ones.
    #
    # The correct test either way is a CROSSING of the entry level, from
    # whichever side price happened to be on.
    approach_from_above = mid0 > entry
    fill_i = None
    for i, (ts, b, a) in enumerate(fwd):
        px = a if long else b                  # the side that would fill us
        touched = (px <= entry) if approach_from_above else (px >= entry)
        if touched:
            fill_i = i
            break
        if (ts - t0).total_seconds() > max_hold_minutes * 60:
            break
    if fill_i is None:
        rec.outcome, rec.resolution = "NOT_FILLED", resolution_label
        rec.net_r = 0.0
        rec.note = ("entry never reachable after the message — an outcome, not a "
                    "discard")
        return rec
    fts = fwd[fill_i][0]
    rec.fillable = True
    rec.fill_utc = fts.isoformat()
    rec.minutes_to_fill = round((fts - t0).total_seconds() / 60.0, 2)

    # ---- first touch after the fill --------------------------------------
    mfe = mae = 0.0
    t_mfe = t_mae = fts
    for ts, b, a in fwd[fill_i:]:
        px = b if long else a                      # exit crosses the spread
        r = ((px - entry) if long else (entry - px)) / risk
        if r > mfe:
            mfe, t_mfe = r, ts
        if r < mae:
            mae, t_mae = r, ts
        hit_sl = (b <= sl) if long else (a >= sl)
        hit_tp = tp is not None and ((b >= tp) if long else (a <= tp))
        if hit_sl or hit_tp:
            if hit_sl and hit_tp:
                rec.outcome, rec.resolution = "STOP", "BAR_ASSUMED_STOP_FIRST"
                rec.net_r = -1.0
            elif hit_sl:
                rec.outcome, rec.resolution = "STOP", resolution_label
                rec.net_r = -1.0
            else:
                rec.outcome, rec.resolution = "TARGET", resolution_label
                rec.net_r = round(abs(tp - entry) / risk, 4)
            break
        if (ts - fts).total_seconds() > max_hold_minutes * 60:
            rec.outcome, rec.resolution = "OPEN", resolution_label
            rec.net_r = round(r, 4)
            rec.note = "still open at the hold limit"
            break
    else:
        rec.outcome, rec.resolution = "OPEN", resolution_label
        last = fwd[-1]
        px = last[1] if long else last[2]
        rec.net_r = round(((px - entry) if long else (entry - px)) / risk, 4)
        rec.note = "series ended before resolution"

    if spread_cost:
        rec.net_r = round((rec.net_r or 0.0) - spread_cost / risk, 4)
    rec.mfe_r, rec.mae_r = round(mfe, 4), round(mae, 4)
    rec.minutes_to_mfe = round((t_mfe - fts).total_seconds() / 60.0, 2)
    rec.minutes_to_mae = round((t_mae - fts).total_seconds() / 60.0, 2)
    return rec


# --------------------------------------------------------------------------
# §15 Source dependence — ten channels are not ten opinions
# --------------------------------------------------------------------------

def _tokens(s: str) -> set:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in (s or "")).split()
            if len(w) > 2}


@dataclass
class PairDependence:
    a: str
    b: str
    co_calls: int                 # same direction, b after a, inside the window
    median_lag_s: Optional[float]
    identical_levels: int
    median_text_overlap: float
    dependence: float             # 0..1, an OBSERVED co-movement rate

    def render(self) -> str:
        lag = f"{self.median_lag_s:>6.0f}s" if self.median_lag_s is not None else "     -"
        return (f"  {self.a:<22} -> {self.b:<22} n={self.co_calls:<4} lag={lag} "
                f"same-levels={self.identical_levels:<3} text={self.median_text_overlap:.2f} "
                f"dep={self.dependence:.2f}")


def dependence_graph(signals: Sequence[dict], window_minutes: int = 45
                     ) -> list[PairDependence]:
    """Pairwise co-movement between sources. Arithmetic, not a verdict.

    For every ordered pair (A, B) it counts the times B issued a same-direction
    call shortly after A, and how often the levels or wording matched. High
    values are consistent with copying, a shared upstream, or simply both
    reacting to the same obvious level — this does NOT distinguish those, and
    says so rather than labelling one of them "copying".
    """
    by_src: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        p = s.get("parsed") or {}
        if not p.get("is_trade_call") or not p.get("direction"):
            continue
        by_src[str(s.get("channel"))].append(s)
    for v in by_src.values():
        v.sort(key=lambda x: x["received_utc"])

    out: list[PairDependence] = []
    win = timedelta(minutes=window_minutes)
    for a, av in by_src.items():
        for b, bv in by_src.items():
            if a == b:
                continue
            lags, same_levels, overlaps = [], 0, []
            for sa in av:
                ta = datetime.fromisoformat(sa["received_utc"])
                pa = sa["parsed"]
                for sb in bv:
                    tb = datetime.fromisoformat(sb["received_utc"])
                    if not (ta < tb <= ta + win):
                        continue
                    pb = sb["parsed"]
                    if pa.get("direction") != pb.get("direction"):
                        continue
                    lags.append((tb - ta).total_seconds())
                    if (pa.get("entry") == pb.get("entry")
                            and pa.get("sl") == pb.get("sl")
                            and pa.get("entry") is not None):
                        same_levels += 1
                    ta_, tb_ = _tokens(sa.get("raw_text", "")), _tokens(sb.get("raw_text", ""))
                    if ta_ and tb_:
                        overlaps.append(len(ta_ & tb_) / len(ta_ | tb_))
                    break                       # nearest following call only
            if not lags:
                continue
            # Share of A's calls that B echoed inside the window.
            dep = len(lags) / max(len(av), 1)
            out.append(PairDependence(
                a, b, len(lags), statistics.median(lags), same_levels,
                round(statistics.median(overlaps), 3) if overlaps else 0.0,
                round(dep, 3)))
    return sorted(out, key=lambda p: -p.dependence)


# --------------------------------------------------------------------------
# §20 Source ledger — profiled, not ranked
# --------------------------------------------------------------------------

@dataclass
class SourceProfile:
    source_id: str
    signals: int = 0
    complete: int = 0
    filled: int = 0
    not_filled: int = 0
    net_r: float = 0.0
    mean_r: Optional[float] = None
    wins: int = 0
    median_chase_r: Optional[float] = None
    median_pre_move_r: Optional[float] = None
    median_minutes_to_fill: Optional[float] = None
    by_session: dict = field(default_factory=dict)
    edits: int = 0
    deletions: int = 0
    # NEVER estimated from this file's data. See the note in render().
    incremental_value_to_aurum: Optional[float] = None

    def render(self) -> str:
        mr = f"{self.mean_r:+.3f}" if self.mean_r is not None else "  -  "
        ch = f"{self.median_chase_r:+.2f}" if self.median_chase_r is not None else "  -  "
        sess = "  ".join(f"{k}:{v['n']}@{v['mean_r']:+.2f}"
                         for k, v in sorted(self.by_session.items()))
        return (f"  {self.source_id:<24} sig={self.signals:<5} complete={self.complete:<5} "
                f"filled={self.filled:<5} unfilled={self.not_filled:<4}\n"
                f"  {'':<24} meanR={mr}  net={self.net_r:+.2f}  "
                f"chase={ch}R  edits={self.edits} deletes={self.deletions}\n"
                f"  {'':<24} by session: {sess or '-'}")


def source_ledger(recs: Sequence[Reconstruction],
                  capture_rows: Sequence[dict] = ()) -> list[SourceProfile]:
    """Per-source longitudinal profile. Deliberately returns NO ranking.

    Sorting these by win rate or mean R is the mistake §20 warns about: a source
    can be directionally useless and still carry information (its activity may
    precede volatility). The ordering here is alphabetical so that reading the
    table does not imply a league position.
    """
    edits: dict[str, int] = defaultdict(int)
    dels: dict[str, int] = defaultdict(int)
    for r in capture_rows:
        if r.get("event") == "edit":
            edits[str(r.get("channel"))] += 1
        elif r.get("event") == "deletion":
            dels[str(r.get("channel"))] += 1

    by: dict[str, list[Reconstruction]] = defaultdict(list)
    for r in recs:
        by[r.source_id].append(r)

    out: list[SourceProfile] = []
    for sid, rs in by.items():
        p = SourceProfile(source_id=sid, signals=len(rs),
                          edits=edits.get(sid, 0), deletions=dels.get(sid, 0))
        done = [r for r in rs if r.net_r is not None]
        p.complete = len(done)
        p.filled = sum(1 for r in done if r.fillable)
        p.not_filled = sum(1 for r in done if r.outcome == "NOT_FILLED")
        scored = [r for r in done if r.fillable]
        if scored:
            vals = [r.net_r for r in scored]
            p.net_r = round(sum(vals), 4)
            p.mean_r = round(statistics.fmean(vals), 4)
            p.wins = sum(1 for v in vals if v > 0)
            chase = [r.chase_r for r in scored if r.chase_r is not None]
            pre = [r.pre_move_r for r in scored if r.pre_move_r is not None]
            fills = [r.minutes_to_fill for r in scored if r.minutes_to_fill is not None]
            p.median_chase_r = round(statistics.median(chase), 3) if chase else None
            p.median_pre_move_r = round(statistics.median(pre), 3) if pre else None
            p.median_minutes_to_fill = round(statistics.median(fills), 2) if fills else None
            sess: dict[str, list[float]] = defaultdict(list)
            for r in scored:
                sess[r.session].append(r.net_r)
            p.by_session = {k: {"n": len(v), "mean_r": round(statistics.fmean(v), 3)}
                            for k, v in sess.items()}
        out.append(p)
    return sorted(out, key=lambda p: p.source_id)


def incremental_value_note() -> str:
    return (
        "incremental_value_to_aurum is NOT computed here and must not be inferred\n"
        "from the columns above. A source's standalone expectancy answers a\n"
        "different question from whether it improves Aurum's decisions. That\n"
        "requires paired same-state runs (mandate §18, arms A..F) against a base\n"
        "system with a measured edge. Aurum's baseline is currently -7.8R with\n"
        "arms B..H unrun, so there is no base to improve on and the comparison\n"
        "would be uninterpretable in either direction.")


# --------------------------------------------------------------------------

def load_capture(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def report(recs: Sequence[Reconstruction], rows: Sequence[dict],
           deps: Sequence[PairDependence]) -> str:
    out = [f"EXTERNAL SIGNAL EVIDENCE  ({EVIDENCE_VERSION})", ""]
    n = len(recs)
    filled = sum(1 for r in recs if r.fillable)
    nf = sum(1 for r in recs if r.outcome == "NOT_FILLED")
    inc = sum(1 for r in recs if not (r.entry and r.sl))
    out += [f"reconstructed {n} call(s): {filled} fillable, {nf} never reachable, "
            f"{inc} unparseable", ""]
    assumed = sum(1 for r in recs if r.resolution == "BAR_ASSUMED_STOP_FIRST")
    if n:
        out += [f"resolution: {assumed}/{n} required a stop-first assumption "
                f"({assumed / n:.0%})", ""]
    out += ["SOURCE LEDGER (alphabetical — deliberately not ranked)"]
    for p in source_ledger(recs, rows):
        out.append(p.render())
    out += ["", "SOURCE DEPENDENCE (top pairs by observed echo rate)"]
    out += [d.render() for d in deps[:10]] or ["  (no co-directional pairs observed)"]
    out += ["", "NOTE", incremental_value_note()]
    return "\n".join(out)
