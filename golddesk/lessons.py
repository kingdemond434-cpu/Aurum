"""Self-feedback mined from the desk's own resolved ledger.

The analyst that never sees its own scorecard is an explainer, not a learner.
This module is that scorecard, rendered INTO the brief so the next call carries
the history its own mechanism cohorts were made of: what mechanisms actually
pay, where refusals went blind, whether high confidence is calibrated, and
whether novelty — reported as uncertainty — resolves or burns.

Rules inherited from the constitution:
  - Only stable evidence becomes memory. A mechanism with fewer than
    `min_mech` resolved rows is omitted, not guessed about.
  - Every R number below is the FIRST-TOUCH resolution the rest of the desk
    uses (counterfactual_r), so the analyst's beliefs are scored the same way
    the desk measures every gate.
  - Nothing here can refuse a trade. It is information in the brief, nothing
    more.
"""

from __future__ import annotations

import statistics
from typing import Optional, Sequence


def _decision(row: dict) -> dict:
    return row.get("decision") or {}


def _read(row: dict) -> dict:
    return _decision(row).get("analyst_read") or {}


def _mech(row: dict) -> str:
    r = _read(row)
    return r.get("mechanism_name") or _decision(row).get("mechanism") or "UNKNOWN"


def _first_touch_r(row: dict, target_r: float, stop_r: float) -> Optional[float]:
    from .constitution import counterfactual_r
    return counterfactual_r(row.get("outcome") or {}, target_r=target_r, stop_r=stop_r)


def build_lessons(rows: Sequence[dict], *,
                  target_r: float = 2.0, stop_r: float = -1.0,
                  min_mech: int = 5, min_bucket: int = 5,
                  cap: int = 6) -> Optional[str]:
    """A compact scorecard, or None when the ledger is too thin to say anything.

    Returns None often and early — that is the honest state of a young desk.
    """
    mech_rows: dict[str, list[float]] = {}
    cal_hi: list[float] = []
    cal_mid: list[float] = []
    novelty_buckets: dict[str, list[float]] = {}
    direction_buckets: dict[str, list[float]] = {}
    session_buckets: dict[str, list[float]] = {}
    blind: list[tuple[str, str, str, float]] = []

    for row in rows:
        kind = str(row.get("kind", ""))
        rr = _first_touch_r(row, target_r, stop_r)
        if rr is None:
            continue
        read = _read(row)
        if kind == "SIGNAL":
            mech_rows.setdefault(_mech(row), []).append(rr)
            direction = str((row.get("decision") or {}).get("direction") or "UNKNOWN")
            session = str((row.get("context") or {}).get("session") or "UNKNOWN")
            direction_buckets.setdefault(direction, []).append(rr)
            session_buckets.setdefault(session, []).append(rr)
            conf = read.get("confidence", 0)
            if conf >= 4:
                cal_hi.append(rr)
            elif conf:
                cal_mid.append(rr)
            nov = read.get("novelty") or "LOW"
            novelty_buckets.setdefault(nov, []).append(rr)
        elif kind.startswith("REFUSAL") and rr > 1.0 and read:
            blind.append((str(row.get("t0", ""))[:16], _mech(row),
                          read.get("novelty") or "LOW", rr))

    lines: list[str] = []
    mech_ag = [(m, v) for m, v in mech_rows.items() if len(v) >= min_mech]
    mech_ag.sort(key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
    if mech_ag:
        lines.append("MY OWN RESOLVED EVIDENCE (first-touch, target 2R / stop -1R):")
        lines.append("  mechanisms: " + " · ".join(
            f"{m} {statistics.fmean(v):+.2f}R/{len(v)}" for m, v in mech_ag[:cap]))
    resolved_signals = sum(len(v) for v in mech_rows.values())
    if resolved_signals >= min_bucket:
        lines.append("MY OWN RESOLVED EVIDENCE (advisory; diagnose, do not ban):")
        lines.append("  direction: " + " · ".join(
            f"{k} {statistics.fmean(v):+.2f}R/{len(v)}"
            for k, v in sorted(direction_buckets.items()) if len(v) >= min_bucket))
        lines.append("  session: " + " · ".join(
            f"{k} {statistics.fmean(v):+.2f}R/{len(v)}"
            for k, v in sorted(session_buckets.items()) if len(v) >= min_bucket))
        unique = len(mech_rows)
        if unique / resolved_signals >= 0.80:
            lines.append(
                f"  mechanism memory is fragmented: {unique} names across "
                f"{resolved_signals} resolved signals. Reuse a prior name only "
                "when the forced-flow mechanism is genuinely the same; novelty "
                "does not justify renaming the same idea.")
    if len(cal_hi) >= min_bucket and len(cal_mid) >= min_bucket:
        lines.append(
            f"  calibration: conf>=4 {statistics.fmean(cal_hi):+.2f}R/{len(cal_hi)}"
            f" vs conf<=3 {statistics.fmean(cal_mid):+.2f}R/{len(cal_mid)}")
        if statistics.fmean(cal_hi) < statistics.fmean(cal_mid):
            lines.append("    -> high confidence is NOT paying more than low. Deflate.")
    nov_summary = [(n, novelty_buckets[n]) for n in ("HIGH", "MEDIUM", "LOW")
                   if len(novelty_buckets.get(n, ())) >= min_bucket]
    if nov_summary:
        lines.append("  novelty-as-uncertainty: " + " · ".join(
            f"{n} {statistics.fmean(v):+.2f}R/{len(v)}" for n, v in nov_summary))
    if blind:
        lines.append("  BLIND SPOTS — I refused and it would have paid: "
                     + ", ".join(f"{d} {m} ({nov}) +{rr:.1f}R"
                                 for d, m, nov, rr in blind[-cap:]))
    return "\n".join(lines) if lines else None
