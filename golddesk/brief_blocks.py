"""Assemble the deterministic context blocks that go into a brief — one call, not three.

WHY A BUILDER AND NOT THREE CALL SITES

Each module renders its own block, and each has a refusal path. Left to individual call sites,
those refusals get wired inconsistently: one caller passes seasonality, another forgets, and the
prompt silently differs between the live desk and the backtest that validated it. A brief built
two ways is two experiments.

So this is the single place that knows which blocks exist and what happens when one is absent.

ABSENCE IS RENDERED, NEVER DROPPED

If a block cannot be built, its slot still appears saying so. Dropping it would make an absent
measurement indistinguishable from a measurement that came back neutral, and the model reading
the prompt has no way to tell the difference — it just sees one fewer heading and assumes nothing
is there to see. Every module here already refuses honestly on its own; this preserves that
refusal instead of swallowing it.

THE TIMEFRAME STATES ARE HERE; THE VERDICT IS NOT

`hierarchical_bias.assess` needs a proposed direction, which does not exist until the model has
answered. What goes in the prompt is the STATES — what each timeframe currently reads — so the
model can weigh them. The ruling happens in `compile_signal` afterwards. See the `blocks` field
on `MarketBrief` for why splitting it this way is forced rather than chosen.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .hierarchical_bias import TimeframeRead
from .seasonality import MonthStat
from .seasonality import load as _load_seasonality
from .seasonality import to_prompt as _seasonality_prompt
from .supply_side import calendar_flags, floor_context
from .supply_side import to_prompt as _supply_prompt

#: Shipped alongside the module: the measured table, with its provenance and sample sizes.
DEFAULT_SEASONALITY = Path(__file__).with_name("seasonality_measured.json")


def timeframe_block(reads: Sequence[TimeframeRead]) -> str:
    """The multi-timeframe STATES, with unavailable ones named rather than omitted."""
    if not reads:
        return ("[HIERARCHICAL BIAS]\n  NOT COMPUTED — no timeframe reads supplied. This is not "
                "alignment; nothing was checked.\n[/HIERARCHICAL BIAS]")
    lines = ["[HIERARCHICAL BIAS — states only; the ruling happens after the read]"]
    for r in reads:
        if r.state is None:
            lines.append(f"  {r.label}: UNAVAILABLE (not enough bars) — contributes nothing")
        else:
            lines.append(f"  {r.label}: {r.state.trend_direction} "
                         f"{r.state.trend_health}/{r.state.trend_maturity}, "
                         f"displacement {r.state.displacement_state}")
    lines.append("[/HIERARCHICAL BIAS]")
    return "\n".join(lines)


def seasonality_block(now: Optional[datetime] = None,
                      path: Optional[Path] = None) -> str:
    """The current month only, measured, with n attached.

    A missing table is reported as missing. It is emphatically not neutral: 'no seasonal effect'
    and 'nobody measured' are different claims and only one of them is defensible.
    """
    p = path or DEFAULT_SEASONALITY
    try:
        stats: list[MonthStat] = _load_seasonality(p)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        kind = type(exc).__name__
        return (f"[SEASONALITY]\n  UNAVAILABLE — no measured table at {p.name} ({kind}). "
                "Absent is not 'no seasonal effect'; rebuild with seasonality.build()."
                "\n[/SEASONALITY]")
    return _seasonality_prompt(stats, now=now)


def supply_block(spot: float, atr: float, aisc: Optional[float] = None,
                 now: Optional[datetime] = None) -> str:
    """Cost-floor distance plus the dated calendar effects.

    `aisc=None` is the normal state until a real World Gold Council figure is wired, and it
    renders UNMEASURED. The calendar half needs no feed and always works.
    """
    return _supply_prompt(floor_context(spot, atr, aisc), calendar_flags(now=now))


def build(*, spot: float, atr: float,
          tf_reads: Sequence[TimeframeRead] = (),
          aisc: Optional[float] = None,
          now: Optional[datetime] = None,
          seasonality_path: Optional[Path] = None) -> tuple[str, ...]:
    """Every block, in prompt order. Pass straight to `MarketBrief(blocks=...)`.

    Ordered widest-context-first: seasonality (this month), supply (this quarter), timeframes
    (right now). A model reading top-down meets the slow-moving frame before the fast one, which
    is the order the desk wants it to reason in.
    """
    return (
        seasonality_block(now=now, path=seasonality_path),
        supply_block(spot=spot, atr=atr, aisc=aisc, now=now),
        timeframe_block(tf_reads),
    )
