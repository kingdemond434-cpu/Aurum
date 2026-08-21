"""LiveFeed.connect() must leave the feed actually usable, not just 'connected'.

No test anywhere exercised golddesk/feed.py's LiveFeed/ServerClock directly --
the module's own docstring claims a SimulatedMt5Client exists to exercise this
"in full without a terminal", but that class was never built. That gap is
exactly how a fresh install could never get past its first _warm(): connect()
reported success while the server clock stayed unmeasured, and bars() (which
_warm() calls first) needs the clock to already be known.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from golddesk.feed import FeedError, LiveFeed, FeedConfig


class FakeClient:
    """The minimal Mt5Client surface, in full control of what each call returns."""

    def __init__(self, tick=(4500.0, 4500.5, "2026-08-21T12:00:00"), bars_ok=True):
        self._tick = tick
        self._bars_ok = bars_ok
        self.tick_calls = 0

    def initialize(self, path, login, password, server) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple:
        return (0, "no error")

    def symbol_info(self, symbol):
        return object() if symbol == "XAUUSD" else None

    def symbol_info_tick(self, symbol):
        self.tick_calls += 1
        if self._tick is None:
            return None
        bid, ask, iso = self._tick

        class T:
            pass
        t = T()
        t.bid, t.ask, t.time = bid, ask, datetime.fromisoformat(iso)
        return t

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if not self._bars_ok:
            return None
        rows = []
        for i in range(count):
            rows.append({"time": datetime(2026, 8, 21, 11, i % 59, tzinfo=None),
                        "open": 4500.0, "high": 4501.0, "low": 4499.0,
                        "close": 4500.5, "tick_volume": 10.0, "spread": 5})
        return rows

    def account_info(self):
        return object()


def test_connect_measures_the_clock_before_returning():
    """The bug: connect() used to succeed while clock.known stayed False."""
    feed = LiveFeed(FakeClient(), FeedConfig(symbol="XAUUSD"))
    feed.connect()
    assert feed.clock.known, (
        "connect() returned without measuring the server clock -- the first "
        "bars() call after this (which _warm() makes immediately) would "
        "raise 'server clock not yet measured' with no way to recover, "
        "because nothing else calls raw_tick() before that point")


def test_bars_does_not_raise_immediately_after_connect():
    """The actual failure mode: _warm()'s first call, right after connect()."""
    feed = LiveFeed(FakeClient(), FeedConfig(symbol="XAUUSD"))
    feed.connect()
    bars = feed.bars("M15", count=5)
    assert len(bars) == 5


def test_connect_retries_if_no_tick_is_available_to_measure_the_clock():
    """A connected-but-tick-less state must not be reported as success -- it
    would look identical to the bug this guards against, just delayed."""
    feed = LiveFeed(FakeClient(tick=None), FeedConfig(symbol="XAUUSD",
                    reconnect_attempts=1, reconnect_backoff_s=0.01))
    with pytest.raises(FeedError):
        feed.connect()
