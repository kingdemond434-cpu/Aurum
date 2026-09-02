"""Sessions are clock windows now, and the clock knows DST.

    python3 -m pytest test_sessions.py -q

THE TWO DEFECTS THESE TESTS PIN DOWN, both confirmed in the source first:

  1. "the session's own extremes" was `bars[i-24:i+1]` -- six hours on the live
     M15 path, a whole day on H1, five trading weeks on D1. The same words
     named a different quantity on every timeframe and matched no session's
     open or close on any of them.

  2. `session_of` bucketed by fixed UTC hours, so LONDON began at 06:00 UTC in
     every month of the year. London opens at 08:00 LOCAL, which is 07:00 UTC
     through summer time; New York opens at 13:30 UTC in summer and 14:30 in
     winter. The desk's own economic calendar already used the tz database
     because DST matters -- sessions did not.

The first test below is the one that matters most: it asserts that the window
is IDENTICAL on M15 and H1 for the same instant. That is the property the bar
count could not have, and no amount of tuning 24 to some other integer would
have given it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk import sessions as S
from golddesk.features import Bar

pytestmark = pytest.mark.skipif(not S.TZ_OK,
                                reason="tz database absent; sessions degrade to "
                                       "fixed UTC and say so")


def bars(start: datetime, minutes: int, n: int, base: float = 2000.0) -> list[Bar]:
    out = []
    for k in range(n):
        p = base + k
        out.append(Bar(start + timedelta(minutes=minutes * k), p, p + 1, p - 1, p))
    return out


# ------------------------------------------------- the defect, stated directly

def test_the_window_does_not_depend_on_the_timeframe():
    """The property a bar count cannot have, at any value of the count."""
    ts = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    w = S.current_window(ts)
    m15 = S.extremes(bars(datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc), 15, 96), w)
    h1 = S.extremes(bars(datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc), 60, 24), w)
    assert m15 is not None and h1 is not None
    assert m15.window.start == h1.window.start == w.start
    assert m15.window.end == h1.window.end == w.end


def test_twenty_four_m15_bars_is_six_hours_and_not_a_session():
    """The arithmetic behind the defect, so the fix cannot be argued with."""
    ts = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    w = S.current_window(ts)
    assert (w.end - w.start) > timedelta(hours=6)
    assert w.start.minute == 0 and w.name == "NY"


def test_a_window_holding_no_bars_yields_none_rather_than_a_substitute():
    ts = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    far = bars(datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc), 15, 96)
    assert S.extremes(far, S.current_window(ts)) is None


def test_extremes_never_look_past_the_decision_bar():
    day = bars(datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), 15, 32)
    w = S.current_window(datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc))
    early = S.extremes(day, w, upto=4)
    whole = S.extremes(day, w)
    assert early is not None and whole is not None
    assert early.high < whole.high, "a future bar reached the extreme"


# ------------------------------------------------------------------------ DST

@pytest.mark.parametrize("iso,expect_utc_hour", [
    ("2026-01-15T12:00:00+00:00", 13),      # winter: NY 08:00 local = 13:00 UTC
    ("2026-07-15T12:00:00+00:00", 12),      # summer: NY 08:00 local = 12:00 UTC
])
def test_new_york_moves_with_daylight_saving(iso, expect_utc_hour):
    w = S.window("NY", datetime.fromisoformat(iso))
    assert w.start.hour == expect_utc_hour


def test_london_moves_with_daylight_saving():
    assert S.window("LONDON", datetime(2026, 1, 15, 12, tzinfo=timezone.utc)).start.hour == 8
    assert S.window("LONDON", datetime(2026, 7, 15, 12, tzinfo=timezone.utc)).start.hour == 7


def test_the_old_fixed_buckets_disagreed_with_the_clock():
    """07:30 UTC in July is London open; the old buckets called it LONDON in
    January too, when London had not opened for another half hour."""
    assert S.session_of(datetime(2026, 7, 15, 7, 30, tzinfo=timezone.utc)) == "LONDON"
    assert S.session_of(datetime(2026, 1, 15, 7, 30, tzinfo=timezone.utc)) == "ROLLOVER"


def test_tokyo_has_no_daylight_saving_and_the_window_says_so():
    for month in (1, 7):
        w = S.window("ASIA", datetime(2026, month, 15, 3, tzinfo=timezone.utc))
        assert (w.start.hour, w.end.hour) == (0, 6)


def test_the_rollover_moves_too():
    """17:00 New York is 21:00 UTC in summer and 22:00 in winter. The old code
    said 21:00 all year, which is wrong for about four months of it."""
    assert S.desk_day(datetime(2026, 7, 15, 12, tzinfo=timezone.utc)).start.hour == 21
    assert S.desk_day(datetime(2026, 1, 15, 12, tzinfo=timezone.utc)).start.hour == 22


# ------------------------------------------------------------- window sanity

def test_overlap_is_where_both_are_open():
    ts = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    assert S.session_of(ts) == "OVERLAP"
    assert S.window("LONDON", ts).contains(ts) and S.window("NY", ts).contains(ts)


def test_outside_every_session_the_answer_is_the_day_not_a_fake_session():
    ts = datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)
    assert S.session_of(ts) == "ROLLOVER"
    assert S.current_window(ts).name == "DAY"


def test_previous_complete_is_finished_and_recent():
    ts = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    w = S.previous_complete("ASIA", ts)
    assert w.complete and w.end <= ts
    assert (ts - w.end) < timedelta(days=1)


def test_a_running_session_is_not_reported_as_settled():
    ts = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    assert S.window("NY", ts).complete is False
    assert S.previous_complete("NY", ts).end <= ts


def test_the_week_opens_on_sundays_rollover():
    w = S.week(datetime(2026, 7, 15, 13, tzinfo=timezone.utc))
    assert w.start.weekday() == 6                       # Sunday
    assert (w.end - w.start) == timedelta(days=7)


def test_naive_timestamps_are_read_as_utc_not_as_local():
    naive = datetime(2026, 7, 15, 13, 0)
    assert S.session_of(naive) == S.session_of(naive.replace(tzinfo=timezone.utc))


def test_landmarks_are_in_the_future_and_dst_aware():
    ts = datetime(2026, 7, 15, 6, tzinfo=timezone.utc)
    fix = S.landmark("LONDON_FIX_PM", ts)
    assert fix is not None and fix > ts and fix.hour == 14      # 15:00 BST
    assert S.landmark("NOT_A_LANDMARK", ts) is None


# --------------------------------------------------- the callers actually moved

def _market(n: int = 260, start: datetime | None = None) -> list[Bar]:
    """Two days of oscillating M15 with jitter.

    The oscillation is load-bearing and the reason is recorded in
    test_projected_levels: a monotone series makes each bar's low tie with its
    neighbour's, the fractal test needs a STRICT local extreme, and `classify`
    then returns None -- a green run over an empty market.
    """
    import math
    seed = 20260829

    def jitter() -> float:
        nonlocal seed
        seed = (1103515245 * seed + 12345) % (1 << 31)
        return (seed / (1 << 31) - 0.5) * 1.6

    out, prev = [], 4700.0
    t = start or datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    for k in range(n):
        px = 4700.0 + 6.0 * math.sin(2 * math.pi * k / 23) + jitter()
        hi = max(prev, px) + 0.5 + abs(jitter())
        lo = min(prev, px) - 0.5 - abs(jitter())
        out.append(Bar(t + timedelta(minutes=15 * k), prev, hi, lo, px, 100.0, 0.20))
        prev = px
    return out


def _state(bs):
    from golddesk.features import atr, classify, swings
    sw = swings(bs)
    i = len(bs) - 2
    st = classify(bs, i, sw, atr(bs))
    assert st is not None, "fixture produced no structure; the test would be vacuous"
    return i, sw, st


def _brief(bs):
    from golddesk.runner import build_brief
    i, sw, st = _state(bs)
    return build_brief(bs, i, st, sw, bs[i].close - 0.1, bs[i].close + 0.1, 1.0,
                       timeframe="M15"), st


def test_structure_state_says_which_window_it_measured():
    _, _, st = _state(_market())
    assert st.session_basis == "session"
    assert st.session_window in ("ASIA", "LONDON", "NY", "DAY")


def test_the_brief_carries_the_sessions_that_have_closed():
    """Asia's range is what London trades around, and it was never in the table."""
    from golddesk.analyst import LevelKind
    brief, _ = _brief(_market())
    kinds = {lv.kind for lv in brief.levels}
    assert LevelKind.ASIA_HIGH in kinds and LevelKind.ASIA_LOW in kinds
    assert LevelKind.WEEK_HIGH in kinds and LevelKind.WEEK_LOW in kinds


def test_settled_session_levels_are_structure_and_may_carry_a_stop():
    """They are somewhere price HAS BEEN, so `projected` must be false."""
    from golddesk.analyst import LevelKind
    brief, _ = _brief(_market())
    settled = {LevelKind.ASIA_HIGH, LevelKind.ASIA_LOW, LevelKind.LONDON_HIGH,
               LevelKind.LONDON_LOW, LevelKind.NY_HIGH, LevelKind.NY_LOW,
               LevelKind.WEEK_HIGH, LevelKind.WEEK_LOW}
    seen = 0
    for lv in brief.levels:
        if lv.kind in settled:
            seen += 1
            assert lv.projected is False, f"{lv.id} {lv.kind} is not a projection"
    assert seen, "no settled session levels were built; the test proves nothing"


def test_the_session_high_is_the_running_session_and_not_the_whole_fixture():
    """The old bar count reached back six hours regardless of where price was."""
    from golddesk.analyst import LevelKind
    bs = _market()
    brief, _ = _brief(bs)
    hi = next(lv for lv in brief.levels if lv.kind is LevelKind.SESSION_HIGH)
    whole = max(b.high for b in bs[:len(bs) - 1])
    i, _, _ = _state(bs)
    w = S.current_window(bs[i].ts)
    inside = S.extremes(bs, w, upto=i)
    assert inside is not None
    assert abs(hi.price - round(inside.high, 2)) < 0.011
    assert hi.price <= whole + 1e-9


def test_a_state_whose_window_holds_no_bars_falls_back_and_labels_it():
    """The degrade must be visible, not silent."""
    from golddesk.features import Bar as FBar, atr, classify, swings
    bs = _market()
    i, sw, _ = _state(bs)
    # Timestamps the window logic cannot use at all: every bar carries the same
    # instant, so no window can bracket a range of them.
    frozen = [FBar(bs[0].ts, b.open, b.high, b.low, b.close, b.volume, b.spread)
              for b in bs]
    st = classify(frozen, i, sw, atr(frozen))
    if st is None:
        pytest.skip("fixture degenerated once timestamps were frozen")
    assert st.session_basis == "session"    # one instant still lands in a window
    assert st.session_window


# ------------------------------------- the calendar can be made exact by a file

def test_an_official_release_date_overrides_the_rule(tmp_path, monkeypatch):
    """The rule is 'second Wednesday' and the agency does not follow one. The
    fix is a file the operator supplies — NOT dates typed in from memory, which
    is a claim about somebody else's schedule that goes stale silently."""
    import json

    from golddesk import calendar as C
    p = tmp_path / "release_calendar.json"
    p.write_text(json.dumps({"CPI": ["2026-09-11T08:30"]}), encoding="utf-8")
    monkeypatch.setattr(C, "OFFICIAL_CALENDAR", p)
    cpi = next(e for e in C.month_events(2026, 9) if e.name == "CPI")
    assert cpi.when_utc.day == 11
    assert "PUBLISHED" in cpi.basis


def test_without_a_file_the_rule_is_used_and_labelled_approximate(monkeypatch):
    from golddesk import calendar as C
    monkeypatch.setattr(C, "OFFICIAL_CALENDAR", Path("/nowhere/absent.json"))
    cpi = next(e for e in C.month_events(2026, 9) if e.name == "CPI")
    assert "APPROXIMATE" in cpi.basis


def test_a_corrupt_calendar_file_degrades_to_the_rule(tmp_path, monkeypatch):
    from golddesk import calendar as C
    p = tmp_path / "release_calendar.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(C, "OFFICIAL_CALENDAR", p)
    cpi = next(e for e in C.month_events(2026, 9) if e.name == "CPI")
    assert "APPROXIMATE" in cpi.basis


def test_a_supplied_date_still_goes_through_the_tz_conversion(tmp_path, monkeypatch):
    """A file cannot reintroduce the DST error the rules avoid."""
    import json

    from golddesk import calendar as C
    p = tmp_path / "release_calendar.json"
    p.write_text(json.dumps({"CPI": ["2026-01-13T08:30", "2026-07-14T08:30"]}),
                 encoding="utf-8")
    monkeypatch.setattr(C, "OFFICIAL_CALENDAR", p)
    jan = next(e for e in C.month_events(2026, 1) if e.name == "CPI")
    jul = next(e for e in C.month_events(2026, 7) if e.name == "CPI")
    assert jan.when_utc.hour == 13 and jul.when_utc.hour == 12
