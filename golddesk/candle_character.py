"""Candle CHARACTER as numbers — the half of a chart that is not a level.

WHY THIS EXISTS INSTEAD OF SENDING THE CHART

ANALYST_SYSTEM tells the model to use the chart for exactly four things: "compression and
expansion, wick character, whether bodies are closing at the extremes or the middle, whether a
move looks impulsive or grinding". Every one of those is a ratio of numbers the desk already
holds. The picture was never carrying information the bars do not; it was carrying a RENDERING
of it, and a rendering the model then has to re-measure by eye.

Two things follow, and both matter more than convenience:

  IT IS FREE. Images require the `anthropic:` provider — the Claude Code CLI takes no image
  input at all — which means metered per-read billing instead of the subscription. Measured
  against this desk's actual capital base that is not a close call: a trivial read already
  reports 26,488 cache-creation tokens, and at --wake-every-bar on M15 the chart arm prices in
  at multiples of the account it would be advising on. These numbers ride the existing brief at
  zero marginal cost.

  IT CANNOT WRITE THE ANSWER. The system prompt records the desk's own measurement: "On the same
  bar, an annotated render made this desk report 'broken major support, retesting from below'
  while the clean render of the same data reported 'range-bound, no clean alignment'. The
  annotations wrote the answer." A number has no such degree of freedom. `body_pct 0.31` is the
  same fact to every reader; a candle drawn with a different aspect ratio is not.

WHAT IT DELIBERATELY DOES NOT DO

No verdict. Every value here is a measurement with no vote on direction, the same standing as
`trend`, `macro` and every Context field — the model reasons over it. There is no
"IMPULSIVE"/"GRINDING" label because the threshold that produced such a label would be a
parameter nobody measured, and it would be the label rather than the evidence that the model
then reasoned from. Ratios go in; the reading is the model's job.

CAUSALITY IS THE CALLER'S HALF. Pass bars[:i+1] and nothing later, exactly as `gold_trend.read`
and `day_state.read` require. Nothing here looks past the end of what it is given, so the
guarantee holds as long as the caller upholds it.
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence


class OhlcBar(Protocol):
    open: float
    high: float
    low: float
    close: float


#: Bars of trailing context used for the expansion and impulsiveness comparisons. 20 is the
#: same window `features.atr` already uses on this desk, so "recent" means one thing across the
#: brief rather than one thing per module.
LOOKBACK = 20

#: Below this, the comparisons have no denominator worth the name and the block says UNMEASURED
#: rather than dividing by a two-bar "average". Absence is rendered, never dropped
#: (brief_blocks' own law) — a missing measurement must stay distinguishable from a neutral one.
MIN_BARS = 6


def _safe_ratio(numerator: float, denominator: float) -> Optional[float]:
    """None, never 0.0, when the denominator is degenerate.

    A doji has high == low and a genuinely zero range. Returning 0.0 there would report "the
    body is 0% of the range" — a measurement — when the truth is that the ratio is undefined.
    The renderer prints those as UNMEASURED, which is the honest word for it (L1.28a's rule on
    this desk's sibling: absence must never resolve to a clean number).
    """
    if denominator <= 0:
        return None
    return numerator / denominator


def measure(bars: Sequence[OhlcBar]) -> dict[str, Optional[float]]:
    """Character ratios for the LAST bar, against the preceding `LOOKBACK`.

    Returns a dict of plain floats (or None where undefined) so the caller can render, log or
    journal them without this module deciding the format.
    """
    if len(bars) < MIN_BARS:
        return {}

    last = bars[-1]
    rng = last.high - last.low
    body = abs(last.close - last.open)
    upper = last.high - max(last.open, last.close)
    lower = min(last.open, last.close) - last.low

    prior = bars[-(LOOKBACK + 1):-1]
    ranges = [b.high - b.low for b in prior]
    mean_range = sum(ranges) / len(ranges) if ranges else 0.0

    bodies = [abs(b.close - b.open) for b in prior]
    prior_body_pcts = [
        r for r in (_safe_ratio(bd, rg) for bd, rg in zip(bodies, ranges)) if r is not None
    ]
    mean_body_pct = (sum(prior_body_pcts) / len(prior_body_pcts)) if prior_body_pcts else None

    body_pct = _safe_ratio(body, rng)
    if body_pct is None or not mean_body_pct:
        body_pct_vs_mean = None
    else:
        body_pct_vs_mean = body_pct / mean_body_pct

    return {
        # Wick character. Three ratios of the same range, so they are directly comparable and
        # sum to 1 — a reader can see at a glance where the bar spent itself.
        "body_pct": body_pct,
        "upper_wick_pct": _safe_ratio(upper, rng),
        "lower_wick_pct": _safe_ratio(lower, rng),
        # Where the close sits in the bar. 1.0 = closed on the high, 0.0 = on the low, 0.5 =
        # the middle. This is the "bodies closing at the extremes or the middle" question,
        # asked of the close specifically because that is the price that settled.
        "close_position": _safe_ratio(last.close - last.low, rng),
        # Compression vs expansion: this bar's range against the recent mean. >1 expanding,
        # <1 compressing. No threshold is applied — see the module docstring.
        "range_vs_mean": _safe_ratio(rng, mean_range),
        # Impulsive vs grinding, as the desk can actually measure it: an impulsive bar spends
        # its range on body rather than wick. This is that bar's body share against the recent
        # mean body share, so it is normalised to how this market has been behaving rather than
        # to a constant chosen here.
        "body_pct_vs_mean": body_pct_vs_mean,
    }


def block(bars: Sequence[OhlcBar]) -> str:
    """The rendered brief block. UNMEASURED when the inputs cannot support the measurement."""
    vals = measure(bars)
    if not vals:
        return ("[CANDLE_CHARACTER]\n"
                f"  UNMEASURED — fewer than {MIN_BARS} bars available\n"
                "[/CANDLE_CHARACTER]")

    def fmt(key: str) -> str:
        v = vals.get(key)
        return "UNMEASURED" if v is None else f"{v:.2f}"

    return (
        "[CANDLE_CHARACTER]  last closed bar, ratios only — no verdict\n"
        f"  BODY_PCT                       {fmt('body_pct')}   (share of range spent on body)\n"
        f"  UPPER_WICK_PCT                 {fmt('upper_wick_pct')}\n"
        f"  LOWER_WICK_PCT                 {fmt('lower_wick_pct')}\n"
        f"  CLOSE_POSITION                 {fmt('close_position')}   "
        f"(1.00 = closed on the high, 0.00 = on the low)\n"
        f"  RANGE_VS_MEAN_{LOOKBACK:<2}               {fmt('range_vs_mean')}   "
        f"(>1 expanding, <1 compressing)\n"
        f"  BODY_PCT_VS_MEAN_{LOOKBACK:<2}            {fmt('body_pct_vs_mean')}   "
        f"(>1 more impulsive than recent, <1 more grinding)\n"
        "[/CANDLE_CHARACTER]"
    )
