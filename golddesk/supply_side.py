"""Supply-side structure: the cost floor, and the calendar dates that move physical demand.

THE HONEST VERSION OF "AISC IS A FLOOR"

The thesis is real: gold has a structural floor near miners' all-in sustaining cost, because
below it supply is destroyed. It is also, right now, irrelevant to this desk, and the module says
so rather than manufacturing a signal from it.

Aggregate AISC runs near $2,000-2,200/oz on Q2 2026 producer filings (Centerra $1,269, Aris
$1,986, Allied $2,192, IAMGOLD $2,271, Galiano $2,473). Gold printed 4,523.03/4,523.44 on the
desk's own live feed, 2026-08-20. The floor is therefore roughly FIFTY PERCENT BELOW SPOT.

**THE NUMBERS IN THIS PARAGRAPH ARE DATED ON PURPOSE, AND THEY MOVED FAST.** An earlier version
of this docstring said "AISC near $1,400, gold near $3,300" — both were stale within weeks, and
the conclusion happened to survive only because the ratio barely changed. `floor_context` reads
live spot and live ATR and never these figures; they are here to make the argument checkable, not
to be used. If they and the live quote disagree wildly, trust the quote and update this. Aurum trades M15/H1 structure with stops measured in ATR — a support level 60% away
is not in the same universe as the stop, and cannot inform a single entry. Rendering it as a
bullish "🔥 near cost support" line would be a confident falsehood in the prompt every day for
years.

So `floor_context()` computes the DISTANCE and refuses to characterise it as support unless the
distance is small enough to matter. The threshold is not a preference: it is the multiple of ATR
at which a level could plausibly interact with a trade the desk would actually take.

This is a WORLD-MODEL FACT, not a pulse. Its correct use is regime context — "gold is 60% above
the level at which supply destruction begins" is worth knowing once a quarter and worth nothing
on a Tuesday morning.

THE CALENDAR HALF IS DIFFERENT, AND IT IS THE PART THAT PAYS

Basel III's NSFR treatment of gold and the quarter-end reporting dates around it produce dated,
recurring, KNOWN-IN-ADVANCE demand effects. These need no feed at all — they are fixed dates on a
calendar, computable years ahead, with no fetch, no parse, no staleness and no vendor.

Everything here that is cheap is calendar. Everything expensive is a feed. That asymmetry is the
finding, and it is why this module ships the calendar working and the AISC side as a slot that
refuses until a real measurement is supplied.

WHAT IS DELIBERATELY NOT HERE

Mine closures, export bans and environmental shutdowns are unstructured news. There is no free
structured feed for them, and building a third path to "read the news" beside `news_nlp_parser`
and `geopolitical_pulse` would fragment the same capability across three modules. If those are
wanted, they belong in the news path with a supply-side vocabulary, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

SUPPLY_SIDE_VERSION = "supply-side-2026-08-20-a"

#: How close, in ATR multiples, a level must be before it can be called support for a desk whose
#: stops are ATR-scaled. Beyond this the level cannot interact with any trade being considered,
#: and naming it "support" is decoration.
SUPPORT_ATR_MULTIPLE = 20.0

#: Days either side of a quarter-end that carry the reporting effect.
QUARTER_END_WINDOW = 3


@dataclass(frozen=True)
class FloorContext:
    """Where spot sits relative to the cost floor, and whether that is actionable at all."""

    spot: float
    aisc: Optional[float]
    distance_pct: Optional[float]
    distance_atr: Optional[float]
    actionable: bool
    state: str                      # MEASURED | UNMEASURED
    why: str

    def to_prompt(self) -> str:
        if self.state == "UNMEASURED":
            return f"[SUPPLY FLOOR]\n  UNMEASURED: {self.why}\n[/SUPPLY FLOOR]"
        if not self.actionable:
            return (f"[SUPPLY FLOOR]\n"
                    f"  Cost floor ~${self.aisc:,.0f}/oz, {self.distance_pct:.0%} below spot "
                    f"(${self.spot:,.0f}). NOT ACTIONABLE at this distance.\n"
                    f"  {self.why}\n[/SUPPLY FLOOR]")
        return (f"[SUPPLY FLOOR]\n"
                f"  Spot ${self.spot:,.0f} is within {self.distance_atr:.0f} ATR of the "
                f"~${self.aisc:,.0f}/oz cost floor — supply destruction becomes a real bid here.\n"
                f"  {self.why}\n[/SUPPLY FLOOR]")


def floor_context(spot: float, atr: float, aisc: Optional[float]) -> FloorContext:
    """Distance from the cost floor, and an explicit verdict on whether it can matter.

    `aisc` is the AGGREGATE all-in sustaining cost in USD/oz — the number the World Gold Council
    publishes quarterly. Passing None is the normal case until that measurement is wired, and it
    produces UNMEASURED rather than a neutral-looking zero.
    """
    if aisc is None or aisc <= 0:
        return FloorContext(spot, None, None, None, False, "UNMEASURED",
                            "no aggregate AISC measurement supplied. Absent is not 'no floor' — "
                            "the floor exists and its distance is simply unknown")
    if spot <= 0 or atr <= 0:
        return FloorContext(spot, aisc, None, None, False, "UNMEASURED",
                            "spot or ATR unavailable, so the distance cannot be expressed in the "
                            "units a stop is measured in")
    dist_pct = (spot - aisc) / spot
    dist_atr = (spot - aisc) / atr
    actionable = dist_atr <= SUPPORT_ATR_MULTIPLE
    if actionable:
        why = (f"{dist_atr:.0f} ATR away — inside the {SUPPORT_ATR_MULTIPLE:.0f} ATR band where a "
               "level can interact with a trade this desk would take")
    else:
        why = (f"{dist_atr:,.0f} ATR away. A level this far cannot interact with an ATR-scaled "
               "stop; treating it as support would put a confident falsehood in every prompt. "
               "Regime context only — revisit quarterly, not per bar")
    return FloorContext(spot, aisc, dist_pct, dist_atr, actionable, "MEASURED", why)


# --------------------------------------------------------------------------
# The calendar half — no feed, no staleness, computable years ahead
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CalendarFlag:
    kind: Literal["QUARTER_END", "NSFR_REPORTING"]
    day: date
    days_away: int
    why: str


def _quarter_ends(year: int) -> list[date]:
    return [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)]


def calendar_flags(now: Optional[datetime] = None,
                   window: int = QUARTER_END_WINDOW) -> list[CalendarFlag]:
    """Dated, recurring supply/demand effects near the current date.

    Bank balance-sheet dates are the mechanism: Basel III's NSFR makes unallocated gold expensive
    to carry across a reporting date, so positioning shifts predictably into quarter-ends. Unlike
    every feed in the physical-pulse family, these dates are KNOWN YEARS AHEAD — there is nothing
    to fetch and nothing to go stale.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    out: list[CalendarFlag] = []
    for y in (today.year - 1, today.year, today.year + 1):
        for qe in _quarter_ends(y):
            delta = (qe - today).days
            if abs(delta) <= window:
                out.append(CalendarFlag(
                    "QUARTER_END", qe, delta,
                    f"quarter-end {qe.isoformat()} ({delta:+d}d). Basel III NSFR makes "
                    "unallocated gold expensive to carry across a bank reporting date; "
                    "positioning shifts are dated and recurring, not a forecast"))
    return sorted(out, key=lambda f: abs(f.days_away))


def to_prompt(floor: FloorContext, flags: list[CalendarFlag]) -> str:
    parts = [floor.to_prompt(), "[SUPPLY CALENDAR]"]
    if not flags:
        parts.append("  No dated supply/demand effect within the window.")
    for f in flags:
        parts.append(f"  {f.kind}: {f.why}")
    parts.append("[/SUPPLY CALENDAR]")
    return "\n".join(parts)
