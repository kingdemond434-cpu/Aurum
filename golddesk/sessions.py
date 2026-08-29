"""Sessions defined by the clock, not by a bar count — and the clock knows DST.

TWO DEFECTS, BOTH CONFIRMED IN THE SOURCE BEFORE THIS FILE WAS WRITTEN.

THE FIRST is a label that measures something else. `features.classify` computed
"the session's own extremes" from `bars[i-24:i+1]`, and `runner.build_brief`
built the SESSION_HIGH and SESSION_LOW levels the same way. Twenty-four bars is
a bar count, not a session:

    M15   24 bars = 6 hours          — not a session, and it slides forward
    H1    24 bars = a full day       — three sessions, not one
    D1    24 bars = five trading weeks

So the quantity behind the words SESSION_HIGH depended on the timeframe the
desk happened to be running, and on nothing else. On the live M15 path it was a
rolling six-hour window that never aligned with London's open or New York's
close. The analyst was told "session", reasoned about a session, and cited a
level that was neither. Worse than a wrong number: a wrong number wearing the
right name, which is the class of defect this desk keeps finding.

THE SECOND is DST. `session_of` bucketed by fixed UTC hours, so LONDON began at
06:00 UTC all year. London's open is 08:00 LOCAL — 08:00 UTC in winter and
07:00 UTC in summer — and New York's is 13:30 UTC in summer, 14:30 in winter.
The desk's own economic calendar already resolves New York event times through
the tz database because DST matters there; sessions got fixed integers. Both
statements cannot be right, and it is the calendar that is right.

WHAT THIS FILE DOES ABOUT IT

Windows are built from TIMESTAMPS in the exchange's own local time, so they are
identical on M1 and on H1, and they move with DST because the tz database moves
them. Extremes are then taken over the bars whose timestamps fall inside the
window. A window with no bars in it yields None — never a silent fallback to
whatever bars happened to be nearby, which is the same defect one layer down.

WHEN THE TZ DATABASE IS MISSING

`zoneinfo` is stdlib but its data is not present on every Windows image. The
degrade is fixed UTC offsets — and every Window says `basis="fixed-utc"` when
it was built that way, so a boundary that may be an hour wrong for part of the
year is visible as such rather than presented as a measurement. Same rule the
calendar module already follows.

NOTHING HERE REFUSES ANYTHING. It computes windows and extremes. More correctly
named reference points mean MORE places a trade can legitimately be located,
which is the direction the standing order points.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

SESSIONS_VERSION = "sess-2026-08-29-a"

try:                                   # stdlib since 3.9; the DATA can be absent
    from zoneinfo import ZoneInfo
    _TZ: dict[str, Any] = {"LONDON": ZoneInfo("Europe/London"),
                           "NY": ZoneInfo("America/New_York"),
                           "ASIA": ZoneInfo("Asia/Tokyo")}
    TZ_OK = True
except Exception:                      # pragma: no cover - exercised on odd images
    _TZ = {}
    TZ_OK = False
    log.warning("zoneinfo unavailable — session boundaries fall back to fixed "
                "UTC offsets and will be an hour wrong through DST")

#: Session definitions in the exchange's OWN local clock. These are the hours
#: the liquidity actually keeps; the tz database turns them into UTC.
#:
#: ASIA is Tokyo's cash session. Japan has never observed DST, so this one is
#: 00:00-06:00 UTC in every month of the year — it is here for uniformity and
#: because the day boundary below still has to be right.
#: LONDON is the LBMA/LSE day, which brackets both gold fixes.
#: NY is the New York cash day; COMEX_OPEN below marks the pit open inside it.
_HOURS: dict[str, tuple[str, time, time]] = {
    "ASIA":   ("ASIA",   time(9, 0),  time(15, 0)),
    "LONDON": ("LONDON", time(8, 0),  time(16, 30)),
    "NY":     ("NY",     time(8, 0),  time(17, 0)),
}

#: Fallback UTC hours, used ONLY when the tz database is missing. These are the
#: STANDARD-time offsets; through summer time each is an hour late, which is
#: exactly why every Window built this way is stamped fixed-utc.
_FALLBACK: dict[str, tuple[time, time]] = {
    "ASIA":   (time(0, 0),  time(6, 0)),
    "LONDON": (time(8, 0),  time(16, 30)),
    "NY":     (time(13, 0), time(22, 0)),
}

#: The desk's trading day rolls at 17:00 New York — the same instant the D1 bar
#: opens. In UTC that is 21:00 through summer and 22:00 through winter, and the
#: old fixed "21:00 UTC" was therefore wrong for roughly four months a year.
ROLLOVER_LOCAL = time(17, 0)

#: Instants worth naming. Not windows: single points the analyst can be told the
#: distance to, and around which behaviour is known to differ.
LANDMARKS: dict[str, tuple[str, time]] = {
    "LONDON_FIX_AM": ("LONDON", time(10, 30)),
    "LONDON_FIX_PM": ("LONDON", time(15, 0)),
    "COMEX_OPEN": ("NY", time(8, 20)),
}


@dataclass(frozen=True)
class Window:
    """One named span of wall-clock time, in UTC, and how it was derived."""
    name: str
    start: datetime
    end: datetime
    complete: bool             # has it finished, as of the timestamp asked about
    basis: str                 # "tz" — from the database; "fixed-utc" — degraded

    def contains(self, ts: datetime) -> bool:
        return self.start <= _utc(ts) < self.end

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "complete": self.complete,
                "basis": self.basis}


def _utc(ts: datetime) -> datetime:
    """Any datetime as UTC. A naive one is READ as UTC, which is what the desk's
    bar timestamps are; it is not silently reinterpreted as local time."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None \
        else ts.astimezone(timezone.utc)


def _at(zone: str, d: date, t: time) -> datetime:
    """A local clock time on a local date, as UTC. DST-aware when it can be."""
    tz = _TZ.get(zone)
    if tz is None:
        return datetime.combine(d, t, tzinfo=timezone.utc)
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)


def _local_date(zone: str, ts: datetime) -> date:
    tz = _TZ.get(zone)
    u = _utc(ts)
    return (u.astimezone(tz) if tz is not None else u).date()


def _basis() -> str:
    return "tz" if TZ_OK else "fixed-utc"


def _bounds(name: str, d: date) -> tuple[datetime, datetime]:
    if TZ_OK:
        zone, start, end = _HOURS[name]
        s, e = _at(zone, d, start), _at(zone, d, end)
    else:
        start, end = _FALLBACK[name]
        s = datetime.combine(d, start, tzinfo=timezone.utc)
        e = datetime.combine(d, end, tzinfo=timezone.utc)
    if e <= s:                                    # a session that crosses midnight
        e += timedelta(days=1)
    return s, e


def window(name: str, ts: datetime) -> Window:
    """The named session's window containing `ts`, or the most recent one before it.

    "Most recent" rather than "today's" on purpose. At 03:00 UTC the New York
    session that matters is yesterday's, and returning a window that has not
    begun would make every extreme drawn from it empty.
    """
    if name not in _HOURS:
        raise KeyError(name)
    u = _utc(ts)
    d = _local_date(_HOURS[name][0] if TZ_OK else "NY", u)
    for back in (0, 1, 2):
        s, e = _bounds(name, d - timedelta(days=back))
        if u >= s:
            return Window(name, s, e, complete=u >= e, basis=_basis())
    s, e = _bounds(name, d - timedelta(days=2))
    return Window(name, s, e, complete=True, basis=_basis())


def previous_complete(name: str, ts: datetime) -> Window:
    """The last window of this session that had FINISHED by `ts`.

    During London, "the Asian range" means the one that closed this morning —
    not the one still forming, and not one from two days ago. A running window
    quoted as a completed range is a level that can still move, which is not
    what a level is.
    """
    w = window(name, ts)
    if w.complete:
        return w
    d = _local_date(_HOURS[name][0] if TZ_OK else "NY", w.start) - timedelta(days=1)
    for back in range(0, 5):
        s, e = _bounds(name, d - timedelta(days=back))
        if _utc(ts) >= e:
            return Window(name, s, e, complete=True, basis=_basis())
    return Window(name, w.start - timedelta(days=1), w.end - timedelta(days=1),
                  True, _basis())


def desk_day(ts: datetime) -> Window:
    """The trading day containing `ts`, bounded by the 17:00 New York rollover."""
    u = _utc(ts)
    d = _local_date("NY", u)
    start = _at("NY", d, ROLLOVER_LOCAL) if TZ_OK else \
        datetime.combine(d, time(22, 0), tzinfo=timezone.utc)
    if u < start:
        d = d - timedelta(days=1)
        start = _at("NY", d, ROLLOVER_LOCAL) if TZ_OK else \
            datetime.combine(d, time(22, 0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return Window("DAY", start, end, complete=u >= end, basis=_basis())


def prior_day(ts: datetime) -> Window:
    """The trading day BEFORE the one containing `ts`, on the same rollover.

    Weekends are not skipped here and that is deliberate: the previous calendar
    trading day of a Monday morning is Sunday's thin open, and pretending it was
    Friday would attach Friday's name to a range that includes Sunday's gap.
    Bars decide what is in it; if a window holds no bars, `extremes` says None.
    """
    d = desk_day(ts)
    return Window("PRIOR_DAY", d.start - timedelta(days=1), d.start, True, d.basis)


def week(ts: datetime) -> Window:
    """The trading week containing `ts`, opening at Sunday's 17:00 New York."""
    d = desk_day(ts)
    # Monday is 0; the week opens on the rollover that precedes Monday's session.
    back = (d.start.astimezone(timezone.utc).weekday() + 1) % 7
    start = d.start - timedelta(days=back)
    return Window("WEEK", start, start + timedelta(days=7),
                  complete=_utc(ts) >= start + timedelta(days=7), basis=d.basis)


def session_of(ts: datetime) -> str:
    """Which session a timestamp falls in. DST-aware; same vocabulary as before.

    OVERLAP is where London and New York are both open — the highest-liquidity
    span of the gold day, and the one the old fixed buckets placed an hour off
    for eight months of the year.
    """
    u = _utc(ts)
    in_ldn = window("LONDON", u).contains(u)
    in_ny = window("NY", u).contains(u)
    if in_ldn and in_ny:
        return "OVERLAP"
    if in_ny:
        return "NY"
    if in_ldn:
        return "LONDON"
    if window("ASIA", u).contains(u):
        return "ASIA"
    return "ROLLOVER"


def landmark(name: str, ts: datetime) -> Optional[datetime]:
    """The next occurrence of a named instant, in UTC, or None if unknown."""
    spec = LANDMARKS.get(name)
    if spec is None:
        return None
    zone, t = spec
    u = _utc(ts)
    for fwd in (0, 1, 2, 3):
        when = _at(zone, _local_date(zone if TZ_OK else "NY", u) + timedelta(days=fwd), t)
        if when >= u:
            return when
    return None


# --------------------------------------------------------------------------
# Extremes over a window — and None when the window holds nothing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Extremes:
    window: Window
    high: float
    low: float
    n_bars: int

    @property
    def span(self) -> float:
        return self.high - self.low


def extremes(bars: Sequence[Any], w: Window, *, upto: Optional[int] = None
             ) -> Optional[Extremes]:
    """High and low over the bars whose timestamps fall inside `w`.

    `upto` is the index of the bar being decided on, exclusive of anything after
    it. NO LOOKAHEAD: a window that extends past the decision moment yields the
    extremes of the part that had actually printed, which is what was knowable.

    Returns None — never a substitute range — when no bar falls inside. An empty
    window is a real state (the desk was started mid-session, the feed has a
    hole) and answering it with the nearest bars available would reintroduce the
    exact defect this module exists to remove.
    """
    hi = lo = None
    n = 0
    end = len(bars) if upto is None else min(upto + 1, len(bars))
    for b in bars[:end]:
        ts = getattr(b, "ts", None)
        if not isinstance(ts, datetime):
            continue
        if not w.contains(ts):
            continue
        n += 1
        hi = b.high if hi is None or b.high > hi else hi
        lo = b.low if lo is None or b.low < lo else lo
    if hi is None or lo is None:
        return None
    return Extremes(w, float(hi), float(lo), n)


def current_window(ts: datetime) -> Window:
    """The window whose range "the session high" honestly refers to right now.

    In OVERLAP both London and New York are open; the range that is actually
    forming is New York's, because it began most recently. Outside every session
    NOTHING is open, and the answer is the trading day so far — returned under
    the name DAY rather than dressed up as a session that is not running. The
    caller reads `.name`, so the label always matches the arithmetic.
    """
    s = session_of(ts)
    if s in _HOURS:
        return window(s, ts)
    if s == "OVERLAP":
        return window("NY", ts)
    return desk_day(ts)


def brief_windows(ts: datetime) -> dict[str, Window]:
    """The windows worth putting reference points on, for one decision moment.

    The current one is still forming and the rest are settled; the difference is
    carried on each Window's `complete` rather than left to a comment.
    """
    return {"CURRENT": current_window(ts),
            "ASIA": previous_complete("ASIA", ts),
            "LONDON": previous_complete("LONDON", ts),
            "NY": previous_complete("NY", ts),
            "PRIOR_DAY": prior_day(ts),
            "WEEK": week(ts)}
