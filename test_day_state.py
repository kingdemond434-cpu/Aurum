"""day_state.read() classifies D from D-1/D-2 only, and never leaks D's own bars."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from golddesk.day_state import DayState, read


@dataclass(frozen=True)
class B:
    ts: datetime
    high: float
    low: float
    close: float


def _day_bars(day0: datetime, day_index: int, ny_hi: float, ny_lo: float,
              *, close_at: float | None = None,
              outside_hi: float | None = None, outside_lo: float | None = None):
    """One synthetic day: an NY-session (13:00-22:00 UTC) sweep plus optional
    bars outside that window so full-calendar-day hi/lo can differ from the
    NY-session hi/lo (needed to exercise FAILED_BREAK independently)."""
    d = day0 + timedelta(days=day_index)
    rows = [
        B(d.replace(hour=13, minute=0), ny_hi, ny_lo, ny_lo),
        B(d.replace(hour=17, minute=0), ny_hi, ny_lo,
          close_at if close_at is not None else (ny_hi + ny_lo) / 2),
        B(d.replace(hour=22, minute=0), ny_hi, ny_lo,
          close_at if close_at is not None else (ny_hi + ny_lo) / 2),
    ]
    if outside_hi is not None:
        rows.append(B(d.replace(hour=2, minute=0), outside_hi, outside_hi - 0.1, outside_hi))
    if outside_lo is not None:
        rows.append(B(d.replace(hour=2, minute=0), outside_lo + 0.1, outside_lo, outside_lo))
    return rows


DAY0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _quiet_history(n_days: int, base=2000.0, rng=2.0):
    bars = []
    for i in range(n_days):
        bars += _day_bars(DAY0, i, base + rng, base - rng)
    return bars


# --------------------------------------------------------------- fail-closed

def test_empty_is_none():
    assert read([]) is DayState.NONE


def test_one_day_is_none():
    assert read(_day_bars(DAY0, 0, 2001, 1999)) is DayState.NONE


def test_insufficient_trailing_history_is_none():
    # Only 5 prior days of NY-session data -- below the min_periods=10 floor.
    bars = _quiet_history(5)
    bars += _day_bars(DAY0, 5, 2010, 1990)   # D, no NY session needed for "today"
    assert read(bars) is DayState.NONE


# ------------------------------------------------------------ classification

def test_wide_prior_range_is_trend_day():
    bars = _quiet_history(15, rng=2.0)                       # median range ~4
    # One-directional: sweeps well above D-2's high and CLOSES there (trend
    # continuation, not a reversion) so FAILED_BREAK's revert-back-inside
    # condition never fires on either side.
    bars += _day_bars(DAY0, 15, 2020, 1999, close_at=2020)   # D-1: range 21, >> 1.5x median
    bars += _day_bars(DAY0, 16, 2000, 2000)                  # D, current day stub
    assert read(bars) is DayState.TREND_DAY


def test_narrow_prior_range_is_range_day():
    bars = _quiet_history(15, rng=10.0)                      # median range ~20
    bars += _day_bars(DAY0, 15, 2001, 1999)                  # D-1: range 2, << 0.75x median
    bars += _day_bars(DAY0, 16, 2000, 2000)
    assert read(bars) is DayState.RANGE_DAY


def test_typical_prior_range_is_normal_day():
    bars = _quiet_history(15, rng=2.0)
    # Exactly matches the quiet baseline's own range (4) -- equal to, never
    # beyond, D-2's hi/lo, so no side of FAILED_BREAK's sweep test can fire.
    bars += _day_bars(DAY0, 15, 2002, 1998)
    bars += _day_bars(DAY0, 16, 2000, 2000)
    assert read(bars) is DayState.NORMAL_DAY


def test_swept_and_closed_back_inside_is_failed_break():
    bars = _quiet_history(15, rng=2.0)
    # D-2: establishes a level at 1990/2010 (full-day hi/lo)
    bars += _day_bars(DAY0, 15, 2005, 1995, outside_hi=2010, outside_lo=1990)
    # D-1: sweeps ABOVE D-2's high (2010) intraday, but the NY session CLOSES
    # back below it -- a failed break to the upside.
    bars += _day_bars(DAY0, 16, 2005, 1995, close_at=2000, outside_hi=2015)
    bars += _day_bars(DAY0, 17, 2000, 2000)                  # D
    assert read(bars) is DayState.FAILED_BREAK


# ------------------------------------------------------------------- causal

def test_todays_own_bars_never_move_the_read():
    history = (_quiet_history(15, rng=2.0)
              + _day_bars(DAY0, 15, 2020, 1999, close_at=2020))
    a = read(history + _day_bars(DAY0, 16, 2000, 2000))
    b = read(history + _day_bars(DAY0, 16, 5_000_000, -5_000_000))
    assert a is b is DayState.TREND_DAY
