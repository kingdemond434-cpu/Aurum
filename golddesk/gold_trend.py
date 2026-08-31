"""Trend strength for XAUUSD, ported from the quant desk's cross-instrument work.

WHERE THIS CAME FROM AND WHY IT TRANSFERS

quant/desks/mt5/mt5desk/trendday.py built a trend detector and measured it on 22
instruments including XAUUSD: forward 24-bar move in the detected direction is
monotone in strength (+0.024 / +0.073 / +0.136 ATR across strength terciles,
deflated t roughly 0.7 / 2.2 / 1.8 after accounting for overlapping samples).
That is a real, if modest, measured edge, not a claim taken on faith — and
because every quantity is a RATIO (a move in ATRs, a range against its own
trailing median), the mechanism itself makes no reference to what instrument it
is looking at. It is exactly the kind of finding absorb.py's docstring says
transfers: "how session ranges behave" is a mechanism, not a CADJPY fact.

What is ported here is the MECHANISM, not a trust grant. The router and
compiler give this zero authority; it is additional MEASURED CONTEXT in the
brief, in the same spirit as every other Context field — the model reasons over
it, nothing here can refuse a trade on its own. See quant_findings.py for the
formal absorption record: the underlying predictive claim is queued through
absorb.Absorber like any external finding, not asserted by being wired in.

WHY THE PORT AND NOT AN IMPORT

Aurum and quant are separate repositories with no shared install boundary. A
live cross-repo import would make Aurum's signal desk depend on quant's package
layout staying stable, which is precisely the kind of hidden coupling that turns
a one-line refactor over there into an outage over here. The math is duplicated,
deliberately, with its own test suite pinning the same invariants quant's tests
pin: scale-free, mirror-symmetric, causal. A future change to quant's detector
does not silently change what Aurum sees until someone re-ports it on purpose.

WHAT IS DIFFERENT FROM THE QUANT VERSION

Only the input type. quant's version takes OHLC arrays; this takes a sequence
of anything with .open/.high/.low/.close (see OhlcBar below), so callers pass
their own bars directly without an array-conversion step. The formulas are
identical.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

__all__ = ["GoldTrendRead", "OhlcBar", "read"]


@runtime_checkable
class OhlcBar(Protocol):
    """The only shape this module needs. Deliberately NOT golddesk.bars.Bar --

    the live path (runner.build_brief) actually passes golddesk.features.Bar,
    a different class with the same four fields, and golddesk.ledger.Bar is a
    third. Importing one specific Bar class here would type-hint against a
    class that is not what production actually calls this with, which is the
    kind of mismatch that only surfaces when someone runs mypy and wonders why
    it disagrees with reality. Duck typing three compatible Bar classes is
    already this codebase's convention; this makes it explicit rather than
    accidentally correct.
    """
    open: float
    high: float
    low: float
    close: float


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(c), np.nan)
    if len(c) < 2:
        return out
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    # tr[k] is bar (k+1)'s true range, so a length-n valid-mode window sum
    # starting at tr[j] covers bars j+1..j+n and belongs at out[j+n] -- i.e.
    # out[n:] lines up with `sums` with NO extra offset. An earlier version
    # dropped the last element of `sums` here, which is exactly the kind of
    # off-by-one that changes nothing about the shape assertion failing loudly
    # (good) but would have changed every ATR value silently had the lengths
    # happened to match by accident.
    if len(tr) >= n:
        sums = np.convolve(tr, np.ones(n), mode="valid")
        out[n:] = sums / n
    return out


def _efficiency_ratio(c: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    step = np.abs(np.diff(c, prepend=c[0]))
    path = np.full(len(c), np.nan)
    csum = np.cumsum(step)
    path[n:] = csum[n:] - csum[:-n]
    net = np.abs(c - np.concatenate([np.full(n, np.nan), c[:-n]]))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(path > 0, net / path, 0.0)
    out[:n] = np.nan
    return out


def _rolling_median(x: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """Trailing median, index i uses x[:i+1] only — never x[i+1:]."""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        lo = max(0, i - window + 1)
        chunk = x[lo:i + 1]
        chunk = chunk[np.isfinite(chunk)]
        if len(chunk) >= min_periods:
            out[i] = float(np.median(chunk))
    return out


def _rolling_max(x: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        lo = max(0, i - window + 1)
        chunk = x[lo:i + 1]
        out[i] = float(np.nanmax(chunk)) if len(chunk) else np.nan
    return out


@dataclass(frozen=True)
class GoldTrendRead:
    """The reading at the LAST bar of whatever window was passed in.

    One instant, not an array — a live desk asks "what is it now", and
    returning history the caller must index into is how an off-by-one enters
    the brief silently.
    """
    strength: float      # 0..1, direction-agnostic
    direction: int        # -1 / 0 / +1
    dying: bool
    er: float
    expansion: float      # this bar's ATR vs its own trailing median

    def render(self) -> str:
        d = {-1: "DOWN", 0: "NONE", 1: "UP"}[self.direction]
        tag = " DYING" if self.dying else ""
        return (f"  STRENGTH {self.strength:.2f}  DIRECTION {d}{tag}  "
                f"(efficiency {self.er:.2f}, range expansion {self.expansion:.2f}x)")


def read(bars: Sequence[OhlcBar], *, n: int = 12, atr_n: int = 14,
         regime_n: int = 240, floor: float = 0.35,
         decay: float = 0.6, shock_k: float = 1.0) -> GoldTrendRead:
    """Score trendiness as of the LAST bar in `bars`.

    CAUSAL BY CONSTRUCTION: every array below is built from `bars` alone, in
    order, with trailing windows only. Nothing here can see a bar the caller
    did not pass in — the leak test asserts exactly that by corrupting bars
    the caller has not yet passed and confirming the read at an earlier cutoff
    does not move.

    Parameters match quant's trendday.read() exactly; see that module for why
    each one has the value it has. Not re-fitted here — a detector re-tuned per
    consumer is not the same detector, and its cross-instrument evidence would
    no longer apply.
    """
    if len(bars) < atr_n + 2:
        return GoldTrendRead(0.0, 0, False, 0.0, 1.0)

    h = np.array([b.high for b in bars], dtype=float)
    l = np.array([b.low for b in bars], dtype=float)
    c = np.array([b.close for b in bars], dtype=float)
    m = len(c)

    a = _atr(h, l, c, atr_n)
    er = _efficiency_ratio(c, n)
    med = _rolling_median(a, regime_n, min_periods=atr_n * 3)
    with np.errstate(invalid="ignore", divide="ignore"):
        expansion = np.where(med > 0, a / med, np.nan)

    net = c - np.concatenate([np.full(n, np.nan), c[:-n]]) if m > n \
        else np.full(m, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        displacement = np.where(a > 0, np.abs(net) / a, np.nan)

    step = np.sign(np.diff(c, prepend=c[0]))
    sgn = np.sign(net)
    up = np.full(m, np.nan)
    if m >= n:
        ups = (step > 0).astype(float)
        csum = np.cumsum(ups)
        up[n - 1:] = np.concatenate([[csum[n - 1] / n],
                                     (csum[n:] - csum[:-n]) / n])
    agree = np.full(m, np.nan)
    for i in range(m):
        if not np.isfinite(sgn[i]) or sgn[i] == 0 or not np.isfinite(up[i]):
            agree[i] = 0.0
        else:
            frac = up[i] if sgn[i] > 0 else 1.0 - up[i]
            agree[i] = max(0.0, 2.0 * (frac - 0.5))

    def squash(x, cap):
        v = x if np.isfinite(x) else 0.0
        return float(np.clip(v / cap, 0.0, 1.0))

    i = m - 1
    if not (np.isfinite(a[i]) and a[i] > 0 and np.isfinite(er[i])):
        return GoldTrendRead(0.0, 0, False, 0.0, 1.0)

    strength = (squash(er[i], 1.0) + squash(expansion[i], 2.0)
                + squash(displacement[i], 3.0) + squash(agree[i], 1.0)) / 4.0
    direction = int(np.sign(net[i])) if strength >= floor and np.isfinite(net[i]) else 0

    strength_series = np.zeros(m)
    for j in range(m):
        if not (np.isfinite(a[j]) and a[j] > 0 and np.isfinite(er[j])):
            continue
        strength_series[j] = (squash(er[j], 1.0) + squash(expansion[j], 2.0)
                              + squash(displacement[j], 3.0)
                              + squash(agree[j], 1.0)) / 4.0
    peak = _rolling_max(strength_series[:i + 1], n)[-1] if i + 1 >= 1 else strength
    faded = strength < decay * peak
    bar_move = c[i] - c[i - 1] if i > 0 else 0.0
    shock = (-direction * bar_move / a[i] >= shock_k) if direction != 0 else False
    dying = bool((faded or shock) and strength > 0)

    return GoldTrendRead(
        strength=round(strength, 4), direction=direction, dying=dying,
        er=round(float(er[i]) if np.isfinite(er[i]) else 0.0, 4),
        expansion=round(float(expansion[i]) if np.isfinite(expansion[i]) else 1.0, 4))
