from datetime import datetime, timedelta, timezone

from golddesk.features import (Bar, prior_trading_day_window, session_of,
                               session_window)


def bar(ts, high=10.0, low=9.0):
    return Bar(ts, 9.5, high, low, 9.5)


def test_london_open_tracks_dst_not_fixed_utc():
    # 08:00 London is 07:00 UTC in summer and 08:00 UTC in winter.
    assert session_of(datetime(2026, 7, 1, 7, tzinfo=timezone.utc)) == "LONDON"
    assert session_of(datetime(2026, 1, 5, 8, tzinfo=timezone.utc)) == "LONDON"


def test_new_york_rollover_tracks_dst():
    assert session_of(datetime(2026, 7, 1, 20, 30, tzinfo=timezone.utc)) == "ROLLOVER"
    assert session_of(datetime(2026, 1, 5, 21, 30, tzinfo=timezone.utc)) == "ROLLOVER"


def test_session_window_is_time_bucket_not_twenty_four_bars():
    t0 = datetime(2026, 7, 1, 5, tzinfo=timezone.utc)
    bars = [bar(t0 + timedelta(minutes=15 * i)) for i in range(20)]
    current = session_window(bars, len(bars) - 1)
    assert current
    assert all(session_of(b.ts) == session_of(bars[-1].ts) for b in current)
    assert len(current) != min(24, len(bars))


def test_prior_day_uses_new_york_rollover_group():
    t0 = datetime(2026, 7, 1, 16, tzinfo=timezone.utc)
    bars = [bar(t0 + timedelta(hours=i), high=10 + i, low=9 - i)
            for i in range(36)]
    prior = prior_trading_day_window(bars, len(bars) - 1)
    assert prior
    assert all(b.ts < bars[-1].ts for b in prior)
