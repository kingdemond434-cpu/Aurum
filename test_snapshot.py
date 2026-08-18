"""If a snapshot can contain one field from the future, every number computed
downstream of it is worthless and looks fine. These tests are almost entirely
about that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from golddesk.snapshot import (
    CausalSnapshot, Decision, League, LookaheadError, Observation,
    SnapshotBuilder, assert_same_content, paired_states)

UTC = timezone.utc
T0 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@dataclass
class B:
    time: datetime
    open: float
    high: float
    low: float
    close: float


def series(n=10, tf_minutes=15, end=T0):
    """Bars whose OPEN times run up to `end` — so the last one is still forming."""
    return [B(end - timedelta(minutes=tf_minutes * (n - 1 - i)),
              2000 + i, 2002 + i, 1998 + i, 2001 + i) for i in range(n)]


# ------------------------------------------------------------- the whole point

def test_an_observation_from_the_future_is_refused():
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    with pytest.raises(LookaheadError):
        b.add("cpi", 3.1, T0 + timedelta(seconds=1))


def test_an_observation_exactly_at_as_of_is_allowed():
    """A bar that closes precisely at the decision instant IS knowable. An
    off-by-one the strict way silently drops the most informative bar."""
    SnapshotBuilder("XAUUSD", "M15", T0).add("close", 2000.0, T0)


def test_a_revised_value_is_refused_even_when_its_period_is_historical():
    """CPI for last month is safely in the past. The REVISION published tomorrow
    is not, and a brain shown it is being told the answer."""
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    with pytest.raises(LookaheadError) as e:
        b.add("cpi_yoy", 3.1, observed_utc=T0 - timedelta(days=20),
              vintage_utc=T0 + timedelta(days=5))
    assert "vintage" in str(e.value)


def test_the_vintage_actually_known_at_the_time_is_allowed():
    SnapshotBuilder("XAUUSD", "M15", T0).add(
        "cpi_yoy", 3.4, observed_utc=T0 - timedelta(days=20),
        vintage_utc=T0 - timedelta(days=20))


def test_the_forming_bar_is_dropped():
    """A bar labelled 12:00 on M15 is knowable at 12:15. The most common
    backtest leak there is, and the series still ends where a reviewer expects."""
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    b.add_bars("m15", series(), "M15")
    snap = b.build()
    assert snap.get("m15.n_closed") == 9, "the forming bar was included"


def test_the_bar_that_closed_exactly_at_as_of_is_kept():
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    b.add_bars("m15", series(end=T0 - timedelta(minutes=15)), "M15")
    assert b.build().get("m15.n_closed") == 10


def test_bar_zero_is_the_most_recent_CLOSED_bar():
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    b.add_bars("m15", series(), "M15")
    snap = b.build()
    # series() closes: the last CLOSED bar opened at T0-15m, i.e. index 8 -> close 2009
    assert snap.get("m15.0.close") == 2009.0


def test_an_unknown_timeframe_is_refused_rather_than_guessed():
    """Without a period there is no way to tell a closed bar from a forming one,
    and guessing wrong is a silent leak."""
    with pytest.raises(ValueError, match="unknown timeframe"):
        SnapshotBuilder("XAUUSD", "M15", T0).add_bars("x", series(), "M7")


def test_a_naive_datetime_is_refused():
    """Stripping tzinfo to make a comparison work shifts every causality check
    by the offset."""
    with pytest.raises(ValueError, match="naive"):
        SnapshotBuilder("XAUUSD", "M15", datetime(2026, 8, 17, 12, 0))


def test_timezones_are_compared_as_instants_not_wall_clocks():
    """13:00+02:00 IS 11:00Z, which is before noon UTC and must be allowed."""
    tz = timezone(timedelta(hours=2))
    SnapshotBuilder("XAUUSD", "M15", T0).add(
        "x", 1.0, datetime(2026, 8, 17, 13, 0, tzinfo=tz))


def test_add_if_known_records_the_skip_rather_than_hiding_it():
    """A field absent because it was not yet knowable is a different fact from
    one absent because nobody supplied it."""
    b = SnapshotBuilder("XAUUSD", "M15", T0)
    b.add_if_known("nfp", 200_000, T0 + timedelta(hours=2))
    assert b.build().get("nfp") is None
    assert b.refused and b.refused[0][0] == "nfp"


# --------------------------------------------------------------- the two ids

def test_state_id_matches_the_existing_pairing_key():
    """This module adds a check; it must not fork the join key."""
    from golddesk.competition import state_id
    snap = SnapshotBuilder("XAUUSD", "M15", T0).build()
    assert snap.state_id == state_id("XAUUSD", "M15", T0)


def test_same_facts_in_a_different_order_hash_the_same():
    """The question is what the brain saw, not what order the builder ran in."""
    a = SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.0, T0).add("y", 2.0, T0)
    b = SnapshotBuilder("XAUUSD", "M15", T0).add("y", 2.0, T0).add("x", 1.0, T0)
    assert a.build().content_hash == b.build().content_hash


def test_a_changed_value_changes_the_content_hash():
    a = SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.0, T0).build()
    b = SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.1, T0).build()
    assert a.content_hash != b.content_hash


def test_an_extra_field_changes_the_content_hash():
    """The exact failure this module exists to catch: same moment, more shown."""
    a = SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.0, T0).build()
    b = (SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.0, T0)
         .add("secret", 9.9, T0).build())
    assert a.state_id == b.state_id, "same moment"
    assert a.content_hash != b.content_hash, "but not the same comparison"


def test_a_version_bump_alone_does_not_invalidate_a_league():
    a = SnapshotBuilder("XAUUSD", "M15", T0, built_by="v1").add("x", 1.0, T0).build()
    b = SnapshotBuilder("XAUUSD", "M15", T0, built_by="v2").add("x", 1.0, T0).build()
    assert a.content_hash == b.content_hash


# ------------------------------------------------------------ serialisation

def test_a_round_trip_preserves_content_exactly():
    s = (SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.0, T0)
         .add("cpi", 3.4, T0 - timedelta(days=9),
              vintage_utc=T0 - timedelta(days=9)).build())
    back = CausalSnapshot.from_json(s.to_json())
    assert back.content_hash == s.content_hash
    assert back.state_id == s.state_id


def test_an_edited_snapshot_refuses_to_load():
    """Snapshots are shipped to external brains. One that can be edited in
    transit and still score is not evidence."""
    s = SnapshotBuilder("XAUUSD", "M15", T0).add("x", 1.0, T0).build()
    d = s.to_dict()
    d["observations"][0]["value"] = 99.0
    with pytest.raises(ValueError, match="content_hash mismatch"):
        CausalSnapshot.from_dict(d)


def test_the_rendered_view_carries_no_desk_objects():
    """A competitor that must import golddesk to be scored can read the ledger."""
    s = SnapshotBuilder("XAUUSD", "M15", T0).add("atr", 4.25, T0).build()
    txt = s.render()
    assert "golddesk" not in txt and "object at 0x" not in txt
    assert "atr" in txt and "4.25" in txt


def test_render_orders_by_key():
    """Two brains handed the same facts in a different order could disagree for
    that reason alone."""
    s = (SnapshotBuilder("XAUUSD", "M15", T0)
         .add("zeta", 1.0, T0).add("alpha", 2.0, T0).build())
    txt = s.render()
    assert txt.index("alpha") < txt.index("zeta")


# ------------------------------------------------------------------ the league

def _snap(x=1.0, extra=False):
    b = SnapshotBuilder("XAUUSD", "M15", T0).add("x", x, T0)
    if extra:
        b.add("bonus", 7.0, T0)
    return b.build()


def test_two_arms_on_identical_content_are_comparable():
    lg = League()
    s = lg.offer(_snap())
    lg.record("claude", s, "LONG", "structure")
    lg.record("rule", s, "FLAT", "no trigger")
    rep = lg.report()
    assert rep["content_consistent"] and rep["paired_states"] == 1


def test_two_arms_shown_different_content_are_caught():
    """THE FAILURE state_id CANNOT SEE. Same coordinate, different facts: the
    join is clean, the comparison is a fiction."""
    a, b = _snap(), _snap(extra=True)
    assert a.state_id == b.state_id
    ds = [Decision("claude", a.state_id, a.content_hash, "LONG"),
          Decision("rule", b.state_id, b.content_hash, "FLAT")]
    ok, why = assert_same_content(ds)
    assert not ok
    assert "NOT a fair comparison" in why


def test_a_corrupt_state_is_excluded_not_averaged():
    a, b = _snap(), _snap(extra=True)
    ds = [Decision("claude", a.state_id, a.content_hash, "LONG"),
          Decision("rule", b.state_id, b.content_hash, "FLAT")]
    assert paired_states(ds, ["claude", "rule"]) == []


def test_a_brain_that_skipped_a_state_does_not_get_it_counted():
    """Dropping the hard ones and keeping the easy ones is how an arm wins on
    paper."""
    s1 = _snap()
    s2 = SnapshotBuilder("XAUUSD", "M15", T0 + timedelta(minutes=15)).add("x", 2.0, T0).build()
    ds = [Decision("claude", s1.state_id, s1.content_hash, "LONG"),
          Decision("rule", s1.state_id, s1.content_hash, "FLAT"),
          Decision("claude", s2.state_id, s2.content_hash, "SHORT")]
    assert paired_states(ds, ["claude", "rule"]) == [s1.state_id]


def test_a_brain_cannot_stamp_its_own_content_hash():
    """One that could would claim to have seen whatever made its record pair."""
    lg = League()
    s = lg.offer(_snap())
    d = lg.record("claude", s, "LONG", "x", content_hash="forged")
    assert d.content_hash == s.content_hash
    assert d.meta.get("content_hash") == "forged", "the attempt is kept as metadata"


def test_one_brain_is_not_a_league():
    lg = League()
    lg.record("claude", lg.offer(_snap()), "LONG")
    assert "at least two" in lg.report()["verdict"]


def test_the_league_serialises_to_a_reviewable_record():
    lg = League()
    s = lg.offer(_snap())
    lg.record("claude", s, "LONG", "swept the low then reclaimed")
    txt = lg.to_jsonl()
    assert '"kind": "snapshot"' in txt and '"kind": "decision"' in txt
    assert "swept the low" in txt
