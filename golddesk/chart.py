"""Clean chart rendering for the analyst's vision read.

Deliberately unannotated. Q3 in OPEN_QUESTIONS found that on the same bar, an
annotated render produced "broken major H1 support, retesting from below" while
the clean render produced "range-bound, no clean alignment" — the annotated read
reproduced the structural story its own annotations implied. n=1, but the
direction is bad enough that this renderer will not draw a level, a trendline,
an indicator, or a label. Shape only.

The LEVELS table in the brief stays authoritative for every number. The picture
is for shape: compression, expansion, wick character, where the bodies sit.

Cost note: images are the expensive part of a 24/7 desk. Pixels map to tokens
roughly linearly in area, so `width`/`height` here are a cost dial, not a
cosmetic choice. See estimate_image_tokens().
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Sequence

import matplotlib

matplotlib.use("Agg")  # headless — no display on a VPS
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402


# Anthropic's high-res tier tops out at 2576px on the long edge. You almost
# never want that for candles — 1000-1200px is legible and a third the cost.
DEFAULT_W, DEFAULT_H = 1100, 620

UP = "#2a2a2a"
DOWN = "#c9c9c9"
EDGE = "#2a2a2a"
BG = "#ffffff"
GRID = "#ececec"


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Chart:
    timeframe: str      # "H1", "M5" — told to the model as context
    png: bytes
    width: int
    height: int

    @property
    def approx_tokens(self) -> int:
        return estimate_image_tokens(self.width, self.height)


def estimate_image_tokens(width: int, height: int) -> int:
    """Anthropic bills images at roughly (w*h)/750 tokens, capped by the tier."""
    return min(math.ceil((width * height) / 750), 4784)


def render_clean_chart(
    bars: Sequence[Bar],
    timeframe: str,
    *,
    width: int = DEFAULT_W,
    height: int = DEFAULT_H,
    dpi: int = 100,
) -> Chart:
    """Plain candles on a price/index grid. No annotations of any kind.

    `bars` should be CLOSED bars only — pass the same series the desk's
    structure detection sees, or the picture and the numbers disagree.
    """
    if not bars:
        raise ValueError("no bars to render")

    fig_w, fig_h = width / dpi, height / dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    wicks, bodies, colors = [], [], []
    for i, b in enumerate(bars):
        rising = b.close >= b.open
        colors.append(UP if rising else DOWN)
        wicks.append([(i, b.low), (i, b.high)])
        lo, hi = (b.open, b.close) if rising else (b.close, b.open)
        bodies.append((i, lo, hi))

    ax.add_collection(LineCollection(wicks, colors=EDGE, linewidths=0.8, zorder=2))
    for (i, lo, hi), c in zip(bodies, colors):
        ax.add_patch(
            plt.Rectangle(
                (i - 0.32, lo), 0.64, max(hi - lo, 1e-9),
                facecolor=c, edgecolor=EDGE, linewidth=0.7, zorder=3,
            )
        )

    lows = [b.low for b in bars]
    highs = [b.high for b in bars]
    pad = (max(highs) - min(lows)) * 0.06 or 1.0
    ax.set_xlim(-1, len(bars))
    ax.set_ylim(min(lows) - pad, max(highs) + pad)

    # Price axis stays — it is a number the desk computed, not a reading of the
    # chart. Everything else is stripped.
    ax.grid(True, color=GRID, linewidth=0.6, zorder=1)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=8, colors="#555555")
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_color(GRID)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return Chart(timeframe=timeframe, png=buf.getvalue(), width=width, height=height)


SIGNAL_UP = "#1a7f37"      # green — long geometry
SIGNAL_DOWN = "#b42318"    # red — short geometry
SIGNAL_NEUTRAL = "#666666"


def render_signal_chart(
    bars: Sequence[Bar],
    timeframe: str,
    *,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    direction: str,
    width: int = DEFAULT_W,
    height: int = DEFAULT_H,
    dpi: int = 100,
) -> Chart:
    """The TELEGRAM chart, not the analyst chart.

    This renderer is deliberately kept OUT of the vision arm. Q3 found that
    annotations lead a model's read, so what the analyst sees stays clean
    (render_clean_chart). This one is drawn FOR THE HUMAN who has to place the
    order by hand: the compiled geometry — entry, stop, both targets — drawn
    where it belongs on the price axis. The lines restate numbers the compiler
    already decided; they can add no information and are therefore safe to
    show a human and unsafe to show the model whose read the compiler judges.
    """
    if not bars:
        raise ValueError("no bars to render")
    long = direction.upper() == "LONG"
    colour = SIGNAL_UP if long else SIGNAL_DOWN

    fig_w, fig_h = width / dpi, height / dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    wicks, bodies, colors = [], [], []
    for i, b in enumerate(bars):
        rising = b.close >= b.open
        colors.append(UP if rising else DOWN)
        wicks.append([(i, b.low), (i, b.high)])
        lo, hi = (b.open, b.close) if rising else (b.close, b.open)
        bodies.append((i, lo, hi))
    ax.add_collection(LineCollection(wicks, colors=EDGE, linewidths=0.8, zorder=2))
    for (i, lo, hi), c in zip(bodies, colors):
        ax.add_patch(
            plt.Rectangle((i - 0.32, lo), 0.64, max(hi - lo, 1e-9),
                          facecolor=c, edgecolor=EDGE, linewidth=0.7, zorder=3))

    lows = [min(b.low, stop, tp2) for b in bars]
    highs = [max(b.high, tp2) for b in bars]
    pad = (max(highs) - min(lows)) * 0.05 or 1.0
    ax.set_xlim(-1, len(bars) + 9)   # right margin so the labels never clip
    ax.set_ylim(min(lows) - pad, max(highs) + pad)

    n = len(bars)
    for price, label, ls, col in (
            (entry, f"ENTRY {entry:.2f}", "-", colour),
            (stop, f"SL {stop:.2f}", "--", SIGNAL_DOWN),
            (tp1, f"TP1 {tp1:.2f}", "--", SIGNAL_NEUTRAL),
            (tp2, f"TP2 {tp2:.2f}", "--", SIGNAL_UP)):
        ax.axhline(price, xmin=0, xmax=1, color=col, linestyle=ls,
                   linewidth=1.2, zorder=4)
        ax.text(n + 0.4, price, label, va="center", fontsize=9,
                color=col, fontweight="bold", zorder=5)

    ax.grid(True, color=GRID, linewidth=0.6, zorder=1)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=8, colors="#555555")
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_color(GRID)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return Chart(timeframe=timeframe, png=buf.getvalue(), width=width, height=height)
