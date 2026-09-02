"""Scheduled releases that reprice gold. Item #6.

WHY THIS IS COMPUTED, NOT FETCHED

`uncertainty.event_risk()` has always returned UNKNOWN because no calendar was
wired, and the obvious fix — call an economic-calendar API — makes the desk
depend on a network service to answer a question that is mostly arithmetic. The
releases that actually move gold are on FIXED, PUBLISHED RULES:

  NFP    first Friday of the month, 08:30 America/New_York
  CPI    a published monthly schedule, usually the second week, 08:30 ET
  FOMC   eight meetings a year, announced years ahead, decision 14:00 ET
  PPI / retail sales / PCE / ISM — same shape, monthly, fixed times

None of that needs a vendor. What DOES need one is the surprise: consensus,
prior, and the actual print. This module deliberately does not pretend to have
those. It answers exactly one question — how close are we to a scheduled
repricing — and leaves the content of the release to a source that has it.

THE DST TRAP, WHICH IS NOT A DETAIL

Every one of these times is quoted in New York local time, and the desk works in
UTC. 08:30 ET is 12:30 UTC for part of the year and 13:30 UTC for the rest. A
calendar that hardcodes one of them is wrong for roughly half the year, and
wrong in the direction that matters: it would place the blackout an hour away
from the actual release. The conversion goes through the tz database.

WHAT IT IS ALLOWED TO DO

Nothing. It is INFORMATION, surfaced on the uncertainty decomposition and the
signal. It does not gate, and there is deliberately no event blackout in the
executable path: "never trade near news" is exactly the kind of plausible
permanent rule the constitution requires to earn its keep, and it has not. If it
is ever promoted to a gate it gets a registry entry and a counterfactual like
everything else.
"""

from __future__ import annotations

import calendar as _cal
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)

CALENDAR_VERSION = "cal-2026-08-14-a"

try:                                   # stdlib since 3.9; no third-party dep
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:                      # pragma: no cover - exercised on odd images
    NY = None
    log.warning("zoneinfo unavailable — event times will be treated as UTC, "
                "which is wrong by an hour for part of the year")


@dataclass(frozen=True)
class Event:
    name: str
    when_utc: datetime
    importance: str            # HIGH | MEDIUM
    basis: str                 # how this datetime was derived, always

    def minutes_from(self, now: datetime) -> float:
        return (self.when_utc - now).total_seconds() / 60.0


def _ny(d: date, hh: int, mm: int) -> datetime:
    """A New York wall-clock time as UTC. DST handled by the tz database."""
    naive = datetime.combine(d, time(hh, mm))
    if NY is None:
        return naive.replace(tzinfo=timezone.utc)
    return naive.replace(tzinfo=NY).astimezone(timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth weekday of a month. weekday: Monday=0 .. Sunday=6."""
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, _cal.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


# FOMC decision dates. These are PUBLISHED YEARS AHEAD and are not derivable
# from a rule, so they are listed. A date that is not listed is not silently
# guessed — `next_event` reports the horizon it can actually see, so a caller
# can tell "no meeting soon" from "my table ran out".
FOMC_DECISIONS: dict[int, tuple[tuple[int, int], ...]] = {
    2025: ((1, 29), (3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (10, 29), (12, 10)),
    2026: ((1, 28), (3, 18), (4, 29), (6, 17), (7, 29), (9, 16), (11, 4), (12, 16)),
}
FOMC_TABLE_ENDS = date(2026, 12, 31)


#: AN OFFICIAL RELEASE CALENDAR, when the operator supplies one. The rules below
#: are exact for NFP, ISM and FOMC, and APPROXIMATE for CPI, PPI and PCE — those
#: agencies publish their dates and do not follow a clean weekday rule, so
#: "second Wednesday" is right most months and wrong in some.
#:
#: THE FIX IS NOT TO TYPE THE PUBLISHED DATES INTO THIS FILE. That is the same
#: defect as a hardcoded vendor model id: a claim about somebody else's schedule
#: that goes stale silently and is wrong in a way nothing tests. So the seam is a
#: FILE, taken verbatim when present, with the rule as the labelled fallback:
#:
#:   config/release_calendar.json
#:   {"CPI": ["2026-09-10T08:30"], "PPI": [...], "PCE": [...]}
#:
#: Times are LOCAL New York and go through the same conversion the rules use, so
#: a supplied calendar cannot reintroduce the DST error the rules avoid.
OFFICIAL_CALENDAR = (Path(__file__).resolve().parent.parent
                     / "config" / "release_calendar.json")


def _official(name: str, year: int, month: int,
              path: Optional[Path] = None) -> Optional[datetime]:
    """The published datetime for this release in this month, or None.

    Never raises: a missing or malformed file degrades to the rule, which is
    labelled APPROXIMATE, so the worst case is exactly today's behaviour with
    the reason on the row.
    """
    try:
        raw = json.loads(Path(path or OFFICIAL_CALENDAR).read_text(encoding="utf-8"))
        for s in raw.get(name) or []:
            when = datetime.fromisoformat(str(s))
            if when.year == year and when.month == month:
                return _ny(when.date(), when.hour, when.minute)
    except Exception as e:                                       # noqa: BLE001
        log.debug("no official calendar entry for %s: %s", name, e)
    return None


def month_events(year: int, month: int) -> list[Event]:
    """Every scheduled release in one month, from its rule or from the calendar.

    A release named in `config/release_calendar.json` is taken from there and
    the event's `basis` says so — so a brief always states whether the time it
    is reasoning about was PUBLISHED or GUESSED, in the same sentence as the
    time itself.
    """
    out: list[Event] = []

    # NFP — first Friday, 08:30 ET. The single most reliable intraday
    # repricing event for gold, and a pure calendar rule.
    nfp = _nth_weekday(year, month, 4, 1)
    out.append(Event("NFP", _ny(nfp, 8, 30), "HIGH",
                     "first Friday of the month, 08:30 America/New_York"))

    # CPI — BLS publishes an exact schedule; absent it, the second Wednesday is
    # the long-run central tendency. Labelled APPROXIMATE so nobody mistakes a
    # heuristic for a timetable.
    cpi = _nth_weekday(year, month, 2, 2)
    cpi_official = _official("CPI", year, month)
    out.append(Event("CPI", cpi_official or _ny(cpi, 8, 30), "HIGH",
                     "PUBLISHED — from config/release_calendar.json"
                     if cpi_official else
                     "APPROXIMATE — second Wednesday, 08:30 ET; drop the published "
                     "BLS dates into config/release_calendar.json to make it exact"))

    # PPI — the day after CPI in most months, same rationale and caveat.
    ppi_official = _official("PPI", year, month)
    out.append(Event("PPI", ppi_official or _ny(cpi + timedelta(days=1), 8, 30),
                     "MEDIUM",
                     "PUBLISHED — from config/release_calendar.json"
                     if ppi_official else "APPROXIMATE — day after CPI, 08:30 ET"))

    # PCE — the Fed's preferred inflation measure, released near month end.
    pce = _last_weekday(year, month, 4)
    pce_official = _official("PCE", year, month)
    out.append(Event("PCE", pce_official or _ny(pce, 8, 30), "MEDIUM",
                     "PUBLISHED — from config/release_calendar.json"
                     if pce_official else "APPROXIMATE — last Friday, 08:30 ET"))

    # ISM manufacturing — first business day, 10:00 ET.
    d = date(year, month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    out.append(Event("ISM", _ny(d, 10, 0), "MEDIUM",
                     "first business day of the month, 10:00 ET"))

    for (m, day) in FOMC_DECISIONS.get(year, ()):
        if m == month:
            out.append(Event("FOMC", _ny(date(year, m, day), 14, 0), "HIGH",
                             "published FOMC decision date, 14:00 ET"))
    return sorted(out, key=lambda e: e.when_utc)


class Calendar:
    """The seam. Deterministic by default; swappable for a real feed.

    `next_event(now)` returns (minutes_until, name) or None, which is exactly
    what uncertainty.event_risk() consumes. None means "nothing scheduled inside
    the horizon I can see", and `horizon_ends` says how far that is, so an empty
    answer near the end of the FOMC table is not mistaken for a quiet month.
    """

    def __init__(self, *, look_ahead_days: int = 30,
                 extra: Optional[Sequence[Event]] = None,
                 include: Optional[Sequence[str]] = None):
        self.look_ahead_days = look_ahead_days
        self.extra = list(extra or [])
        # Which releases count. Default is HIGH only: the desk trades M15
        # structure, and treating ISM as equivalent to NFP would mark most of
        # the month as elevated, which is the same as marking none of it.
        self.include = set(include or ("NFP", "CPI", "FOMC"))

    def between(self, start: datetime, end: datetime) -> list[Event]:
        out: list[Event] = []
        y, m = start.year, start.month
        for _ in range(4):                       # enough to span look_ahead_days
            out += month_events(y, m)
            m += 1
            if m > 12:
                y, m = y + 1, 1
        out += self.extra
        return sorted((e for e in out
                       if start <= e.when_utc <= end and e.name in self.include),
                      key=lambda e: e.when_utc)

    @property
    def horizon_ends(self) -> date:
        return FOMC_TABLE_ENDS

    def next_event(self, now: datetime) -> Optional[tuple[float, str]]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now.date() > FOMC_TABLE_ENDS:
            log.warning("event calendar is past its FOMC table (%s); FOMC "
                        "proximity is no longer being checked",
                        FOMC_TABLE_ENDS.isoformat())
        upcoming = self.between(now, now + timedelta(days=self.look_ahead_days))
        if not upcoming:
            return None
        e = upcoming[0]
        return (e.minutes_from(now), e.name)

    def render(self, now: datetime, days: int = 14) -> str:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        evs = self.between(now, now + timedelta(days=days))
        lines = [f"SCHEDULED RELEASES ({CALENDAR_VERSION}) — next {days} days",
                 "  These are INFORMATION. Nothing here gates a trade; 'do not",
                 "  trade near news' is a hypothesis that has not earned a gate."]
        if not evs:
            lines.append(f"  none in window (table ends {FOMC_TABLE_ENDS})")
        for e in evs:
            h = e.minutes_from(now) / 60.0
            lines.append(f"  {e.when_utc:%Y-%m-%d %H:%M} UTC  {e.name:<5} "
                         f"{e.importance:<6} in {h:6.1f}h   {e.basis}")
        return "\n".join(lines)


if __name__ == "__main__":
    print(Calendar().render(datetime.now(timezone.utc), days=21))
