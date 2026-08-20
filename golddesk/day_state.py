"""Prior-NY-session displacement state, ported from quant's day_states().

WHERE THIS CAME FROM

quant/desks/mt5/research/run_hunt12.py, function day_states(h1), at the branch
tip that actually produced hunt14's XAUUSD/asia claim (n=760, +0.227R,
deflated t=2.87 -- see golddesk/quant_findings.py). NOT trendday.py, despite
that being quant_findings.py's original source guess -- trendday.py is an
unrelated per-bar trend-strength detector (see gold_trend.py, which already
ports it). day_states() lives in run_hunt12.py and is imported by
run_hunt14.py; this module ports THAT function.

WHAT IT LABELS

The state of calendar day D, derived entirely from D-1 and D-2 -- causally
sound for gating a signal that fires ON day D (hunt14's 07:00 UTC Asia
window). Classification:

  rng_prior = D-1's NY-session (13:00-22:00 UTC) high-low range
  rng_med   = trailing 20-date median of that range, ending at D-1 inclusive,
              min_periods 10
  TREND_DAY   if rng_prior > 1.5 * rng_med
  RANGE_DAY   if rng_prior < 0.75 * rng_med
  NORMAL_DAY  otherwise
  NONE        if rng_prior or rng_med is unavailable (insufficient history)

  FAILED_BREAK overrides the above: fires when D-1's full calendar-day range
  swept the level set by D-2's full calendar-day range and closed back
  inside it --
    (d1_hi > d2_hi and d1_ny_close < d2_hi) or
    (d1_lo < d2_lo and d1_ny_close > d2_lo)

A DELIBERATE DISCREPANCY, NAMED RATHER THAN SILENTLY PICKED

quant's `master` branch keeps an older version of this function whose median
window EXCLUDES the day being classified. The branch that actually produced
hunt14's reported numbers (`claude/llm-auto-upgrade-verify-gcjac3`, rewritten
by a Codex commit) uses a self-inclusive rolling median instead. This ports
the self-inclusive version because that is the code whose output hunt14.json
reports -- porting the excluding version would classify some days
differently and silently break the transfer test's premise that Aurum is
testing THE SAME finding. `reports/hunt12.json` and `reports/hunt14.json`
are gitignored in quant and were not independently re-derivable at port
time; only the code that would produce them was.

WHY THE PORT AND NOT AN IMPORT

Same reason as gold_trend.py: Aurum and quant share no install boundary, and
a live cross-repo import would make a quant refactor a silent Aurum outage.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional, Protocol, Sequence, runtime_checkable

__all__ = ["DayState", "OhlcTsBar", "read"]

_MEDIAN_WINDOW = 20
_MEDIAN_MIN_PERIODS = 10
_TREND_MULT = 1.5
_RANGE_MULT = 0.75
_NY_START_H, _NY_END_H = 13, 22   # UTC, inclusive both ends


class DayState(str, Enum):
    TREND_DAY = "TREND_DAY"
    NORMAL_DAY = "NORMAL_DAY"
    RANGE_DAY = "RANGE_DAY"
    FAILED_BREAK = "FAILED_BREAK"
    NONE = "NONE"


@runtime_checkable
class OhlcTsBar(Protocol):
    """golddesk.features.Bar and golddesk.ledger.Bar both satisfy this."""
    ts: datetime
    high: float
    low: float
    close: float


def _utc_date(ts: datetime) -> date:
    return (ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).date()


def _utc_hour_min(ts: datetime) -> tuple[int, int]:
    u = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return u.hour, u.minute


def _ny_session_stats(rows: Sequence[OhlcTsBar]) -> Optional[tuple[float, float, float]]:
    """(high, low, session close) over 13:00-22:00 UTC inclusive, or None."""
    ny = [b for b in rows if _NY_START_H <= _utc_hour_min(b.ts)[0] <= _NY_END_H]
    # pandas between_time("13:00","22:00") includes 22:00 itself but not 22:01+.
    ny = [b for b in ny if not (_utc_hour_min(b.ts)[0] == _NY_END_H
                                and _utc_hour_min(b.ts)[1] > 0)]
    if not ny:
        return None
    ny_sorted = sorted(ny, key=lambda b: b.ts)
    return (max(b.high for b in ny), min(b.low for b in ny), ny_sorted[-1].close)


def _day_stats(rows: Sequence[OhlcTsBar]) -> Optional[tuple[float, float]]:
    if not rows:
        return None
    return (max(b.high for b in rows), min(b.low for b in rows))


def read(bars: Sequence[OhlcTsBar]) -> DayState:
    """The state gating a signal on the calendar day AFTER the last bar.

    CAUSAL BY CONSTRUCTION: only ever reads bars from calendar days strictly
    before the last bar's own date. Pass every bar available up to (and
    including) the current moment; the function itself throws away same-day
    bars so a caller cannot leak "today" into its own gate by accident.
    """
    if not bars:
        return DayState.NONE
    by_date: dict[date, list[OhlcTsBar]] = {}
    for b in bars:
        by_date.setdefault(_utc_date(b.ts), []).append(b)
    dates = sorted(by_date)
    if len(dates) < 2:
        return DayState.NONE

    today = dates[-1]
    prior = [d for d in dates if d < today]
    if len(prior) < 2:
        return DayState.NONE
    d1, d2 = prior[-1], prior[-2]

    d1_ny = _ny_session_stats(by_date[d1])
    if d1_ny is None:
        return DayState.NONE
    d1_ny_hi, d1_ny_lo, d1_ny_close = d1_ny
    rng_prior = d1_ny_hi - d1_ny_lo

    window_ranges = []
    for d in prior[-_MEDIAN_WINDOW:]:
        ny = _ny_session_stats(by_date[d])
        if ny is not None:
            window_ranges.append(ny[0] - ny[1])
    if len(window_ranges) < _MEDIAN_MIN_PERIODS:
        return DayState.NONE
    window_ranges.sort()
    n = len(window_ranges)
    rng_med = (window_ranges[n // 2] if n % 2 else
              (window_ranges[n // 2 - 1] + window_ranges[n // 2]) / 2.0)
    if rng_med <= 0:
        return DayState.NONE

    d1_day = _day_stats(by_date[d1])
    d2_day = _day_stats(by_date[d2])
    if d1_day is not None and d2_day is not None:
        d1_hi, d1_lo = d1_day
        d2_hi, d2_lo = d2_day
        if ((d1_hi > d2_hi and d1_ny_close < d2_hi)
                or (d1_lo < d2_lo and d1_ny_close > d2_lo)):
            return DayState.FAILED_BREAK

    if rng_prior > _TREND_MULT * rng_med:
        return DayState.TREND_DAY
    if rng_prior < _RANGE_MULT * rng_med:
        return DayState.RANGE_DAY
    return DayState.NORMAL_DAY
