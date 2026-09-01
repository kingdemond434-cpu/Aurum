"""Counterfactual replay of every decision (#4).

After every decision the desk reconstructs what the SAME bars would have paid
under different execution choices, and books the sheet to the ledger next to
the realised row. This is how the desk discovers that its *prediction* was
right but its *monetisation* was wrong — or the reverse — instead of
confusing the two.

Variants resolved analytically on the stored decision (entry, stop, tp1, tp2,
direction) against the forward bars, all with the desk's first-touch walker:

  market   as filled (the realized path already lives on the SIGNAL row)
  delayed  entry k bars later, same structure
  retest   entry at a pullback back inside the stop (stop + atr), same targets
  opposite mirror direction, same structure
  no_trade reference: what refusing paid (already measured for REFUSAL rows)
  tight    stop pulled to -0.5R, same target
  wide_tp  tp2 at 1.5x the original distance
  hold     no partial bank: never take the TP1 leg at midpoint, run to tp2
  reenter  after the first-touch stop, re-enter the same direction at the
           signal level and hold the original horizon (only when a first stop
           actually hit)

Every number is a first-touch R. Nothing here is a model — the code owns all
of it, and nothing here can change a past decision. It only makes the past
cheap to argue with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from .ledger import resolve_trade

HORIZON_BARS = 96                    # a capped lookahead, same for every variant


@dataclass
class Variant:
    name: str
    reason: str
    r: Optional[float] = None
    first_touch: str = "OPEN"        # "OPEN" | "STOP" | "TARGET" | "TIMEOUT"


def _resolve(bars: Sequence, t0: datetime, entry: float, stop: float,
             tp2: float, direction: str, horizon_bars: int) -> tuple[Optional[float], str]:
    if stop == entry or tp2 == entry:
        return None, "INVALID"
    fwd = [b for b in bars if b.ts >= t0][:horizon_bars]
    if not fwd:
        return None, "NO_BARS"
    r, first_touch = resolve_trade(fwd, t0, entry, stop, tp2, direction)
    return r, first_touch


def replay(row: dict, bars: Sequence, *,
           status_at: Optional[str] = None,
           horizon_bars: int = HORIZON_BARS) -> list[Variant]:
    """All counterfactual variants for one stored decision row."""
    dec = row.get("decision") or {}
    entry = dec.get("entry")
    stop = dec.get("stop")
    tp2 = dec.get("tp2")
    direction = dec.get("direction")
    t0 = dec.get("t0")
    if not entry or not stop or not tp2 or not direction or not t0:
        return []
    try:
        t0 = datetime.fromisoformat(str(t0))
    except ValueError:
        return []
    direction = str(direction)
    if entry is None or stop is None or tp2 is None:
        return []

    risk = abs(entry - stop)
    if risk <= 0:
        return []
    r2 = abs(tp2 - entry)

    variants: list[Variant] = []

    # delayed entry: the same structure entered 6 bars later, at the price that
    # actually existed then. Risk is re-based on that entry against the same
    # stop, so R means the same thing in both variants. Measures "was I early".
    later = [b for b in bars if b.ts > t0]
    if len(later) >= 6:
        de_entry = later[5].close
        if abs(de_entry - stop) > 0:
            r, ft = _resolve(bars, t0, de_entry, stop, tp2, direction,
                             horizon_bars)
            variants.append(Variant("delayed_6bar",
                                    "enter 6 bars later with the same levels",
                                    r, ft))

    # retest: enter on a pullback back inside the level (stop + atr buffer),
    # same targets. measures "did chasing the break cost me".
    retest_entry = _retest_entry(bars, t0, entry, stop, direction, risk)
    if retest_entry is not None:
        retest_stop = stop + (entry - stop) * 0.33 if direction == "LONG" \
            else stop - (stop - entry) * 0.33
        r, ft = _resolve(bars, t0, retest_entry, retest_stop, tp2, direction,
                         horizon_bars)
        variants.append(Variant("retest",
                                "enter the retest pullback rather than the break",
                                r, ft))

    # opposite
    o_dir = "SHORT" if direction == "LONG" else "LONG"
    if o_dir == "SHORT":
        r, ft = _resolve(bars, t0, entry, stop + 2 * risk, entry - r2, o_dir,
                         horizon_bars)
    else:
        r, ft = _resolve(bars, t0, entry, stop - 2 * risk, entry + r2, o_dir,
                         horizon_bars)
    variants.append(Variant("opposite",
                            "the mirror trade, same width",
                            r, ft))

    # tighter stop (-0.5R)
    t_stop = entry - 0.5 * risk if direction == "LONG" else entry + 0.5 * risk
    r, ft = _resolve(bars, t0, entry, t_stop, tp2, direction, horizon_bars)
    variants.append(Variant("tight_stop",
                            "stop pulled to -0.5R, same target",
                            r, ft))

    # wider target (1.5x)
    w_tp2 = entry + 1.5 * r2 if direction == "LONG" else entry - 1.5 * r2
    r, ft = _resolve(bars, t0, entry, stop, w_tp2, direction, horizon_bars)
    variants.append(Variant("wide_tp2",
                            "runner extended to 1.5x the original distance",
                            r, ft))

    # hold (no midpoint partial)
    r, ft = _resolve(bars, t0, entry, stop, tp2, direction, horizon_bars)
    variants.insert(0, Variant("hold_full",
                               "no TP1 partial — run the full thesis to tp2",
                               r, ft))

    # re-enter: if the first touch was a stop, re-enter from the signal level
    # and hold the remaining horizon.
    r0, ft0 = _resolve(bars, t0, entry, stop, tp2, direction, horizon_bars)
    if r0 is not None and r0 < 0:
        reentry = _reenter(bars, t0, entry, direction, tp2, stop, horizon_bars)
        if reentry is not None:
            variants.append(Variant(
                "reenter_after_stop",
                "stop hit; re-entered the same thesis for the remaining horizon",
                *reentry))

    return variants


def _retest_entry(bars, t0, entry, stop, direction, risk) -> Optional[float]:
    """A pullback that re-enters inside the original structure."""
    fwd = [b for b in bars if b.ts >= t0][:HORIZON_BARS]
    if not fwd:
        return None
    for b in fwd:
        if direction == "LONG" and b.low <= entry - 0.5 * risk:
            return min(b.close, entry - 0.5 * risk + 0.1)
        if direction == "SHORT" and b.high >= entry + 0.5 * risk:
            return max(b.close, entry + 0.5 * risk - 0.1)
    return None


def _reenter(bars, t0, entry, direction, tp2, stop, horizon_bars) -> Optional[tuple]:
    """Re-enter at the signal level after the first stop, hold remaining bars."""
    fwd = [b for b in bars if b.ts >= t0][:horizon_bars * 2]
    if not fwd:
        return None
    estop = None
    for b in fwd:
        if direction == "LONG" and b.low <= stop:
            estop = b.ts
            break
        if direction == "SHORT" and b.high >= stop:
            estop = b.ts
            break
    if estop is None:
        return None
    remain = [b for b in fwd if b.ts > estop]
    if not remain:
        return None
    r, ft = resolve_trade(remain, remain[0].ts, entry, stop, tp2, direction)
    return r, ft


def best_variant(variants: Sequence[Variant]) -> Optional[Variant]:
    cands = [v for v in variants if v.r is not None]
    if not cands:
        return None
    return max(cands, key=lambda v: v.r)


def to_sheet(variants: Sequence[Variant]) -> dict:
    best = best_variant(variants)
    return {
        "variants": [
            {"name": v.name, "reason": v.reason, "r": v.r, "first_touch": v.first_touch}
            for v in variants
        ],
        "best_variant": best.name if best else None,
        "best_r": best.r if best else None,
        "realised_r": next((v.r for v in variants if v.name == "hold_full"), None),
    }