"""The numbers behind the labels reach the analyst, and reach the ledger.

    python3 -m pytest test_continuous.py -q

THE COMPRESSION THIS UNDOES. The brief handed the intelligent layer four
categories — UP / STRONG / NORMAL / MEDIUM — and nothing else. Two states with
identical labels can be economically nothing alike:

    3.2 ATR impulse, 41% retraced, efficiency 0.71, range expanding
    1.6 ATR impulse, 60% retraced, efficiency 0.28, range compressing

The desk computed every one of those numbers on the way to producing the four
words, then discarded them at the boundary where the reasoning happens.

The test that matters most is the last one in the first section: two states
that produce the SAME labels must produce DIFFERENT continuous blocks. If they
do not, this file has added a section to the prompt and nothing else.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.continuous import Continuous, measure
from golddesk.features import Bar, atr, classify, swings


def series(fn, n=120, start=None) -> list[Bar]:
    """A price path with JITTER, and the jitter is load-bearing.

    A monotone series makes each bar's low tie with its neighbour's, the fractal
    swing test needs a STRICT local extreme, and `classify` then returns None —
    a green run over an empty market. test_projected_levels records the same
    trap; this is the same fixture discipline.
    """
    t = start or datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    seed = 20260829

    def jitter() -> float:
        nonlocal seed
        seed = (1103515245 * seed + 12345) % (1 << 31)
        return (seed / (1 << 31) - 0.5) * 1.2

    out, prev = [], fn(0)
    for k in range(n):
        px = fn(k) + jitter()
        out.append(Bar(t + timedelta(minutes=15 * k), prev,
                       max(prev, px) + 0.4 + abs(jitter()),
                       min(prev, px) - 0.4 - abs(jitter()), px, 100.0, 0.2))
        prev = px
    return out


def state(bars):
    sw = swings(bars)
    i = len(bars) - 2
    return i, classify(bars, i, sw, atr(bars))


# ------------------------------------------------------- it measures something

def test_a_straight_line_is_efficient_and_chop_is_not():
    """The number TREND=UP/HEALTH=STRONG loses completely: whether the market
    got there in a line or in a fight.

    Tested on the arithmetic directly, and deliberately. A genuinely one-way
    series produces NO fractal swings — every bar is the highest so far, so
    there is never a confirmed local high — and `classify` correctly returns
    None on it. Routing this through classify would therefore have tested a
    fixture's ability to oscillate rather than the measurement.
    """
    from golddesk.continuous import _efficiency
    line = [4700.0 + 2.0 * k for k in range(20)]
    chop = [4700.0 + 20.0 * math.sin(k / 2.0) for k in range(20)]
    assert _efficiency(line) == 1.0
    assert _efficiency(chop) < 0.3


def test_efficiency_separates_two_real_states_that_both_have_structure():
    grind = series(lambda k: 4700.0 + 2.0 * k + 30.0 * math.sin(k / 1.9))
    drift = series(lambda k: 4700.0 + 0.5 * k + 8.0 * math.sin(k / 5.0))
    i_g, st_g = state(grind)
    i_d, st_d = state(drift)
    assert st_g is not None and st_d is not None, "fixtures lost their structure"
    e_g = measure(grind, i_g, st_g).efficiency
    e_d = measure(drift, i_d, st_d).efficiency
    assert e_g is not None and e_d is not None and e_g != e_d


def test_distances_are_in_atr_not_in_dollars():
    """18 points from the session high means something different at gold 2000 in
    a quiet Asian hour than at 4700 in an expansion."""
    bars = series(lambda k: 4700.0 + 0.5 * k + 8.0 * math.sin(k / 5.0))
    i, st = state(bars)
    assert st is not None, "fixture produced no structure"
    c = measure(bars, i, st, session_high=bars[i].close + 2 * st.atr,
                session_low=bars[i].close - st.atr)
    assert c.dist_session_high_atr == pytest.approx(2.0, abs=0.01)
    assert c.dist_session_low_atr == pytest.approx(1.0, abs=0.01)


def test_two_states_with_the_same_labels_differ_here():
    """If they did not, this is a longer prompt and nothing else."""
    wide = series(lambda k: 4700.0 + 2.0 * k + 32.0 * math.sin(k / 1.9))
    tight = series(lambda k: 4700.0 + 2.0 * k + 26.0 * math.sin(k / 1.9))
    i_w, st_w = state(wide)
    i_t, st_t = state(tight)
    assert st_w is not None and st_t is not None, "fixtures lost their structure"

    def labels(s):
        return (s.trend_direction, s.trend_health, s.volatility_state,
                s.pullback_depth)
    assert labels(st_w) == labels(st_t), (
        f"the fixtures stopped colliding ({labels(st_w)} vs {labels(st_t)}); "
        f"this test proves nothing unless the LABELS are identical")

    a, b = measure(wide, i_w, st_w), measure(tight, i_t, st_t)
    assert a.efficiency != b.efficiency
    assert a.impulse_atr != b.impulse_atr


# ------------------------------------------- absence is absence, never a zero

def test_an_unmeasurable_field_is_none_and_not_zero():
    """A retracement of 0.0 is a market at its extreme; None is a market whose
    structure could not be read. They must never render the same."""
    c = measure([], 0, None)
    assert c.efficiency is None and c.retracement is None
    assert c.to_dict() == {"version": c.to_dict()["version"]}


def test_unmeasured_is_printed_rather_than_omitted():
    """An omitted line is invisible: the reader cannot tell a measurement of
    zero from one that was never taken."""
    text = Continuous().render()
    assert text.count("UNMEASURED") >= 10


def test_it_never_raises_on_junk():
    """This is an enrichment on a brief; a throw here would cost a signal."""
    class Junk:
        pass
    assert measure([Junk()], 0, Junk()) is not None
    assert measure(None or [], 5, None) is not None
    assert measure([Junk()], 99, Junk()).efficiency is None


def test_a_degraded_session_window_is_carried_not_hidden():
    class St:
        atr = 1.0
        swing_high = swing_low = None
        trend_direction = "NONE"
        session_window, session_basis = "DAY", "bars-24"
    c = measure(series(lambda k: 4700.0 + k), 10, St())
    assert c.session_basis == "bars-24"
    assert "DEGRADED" in c.render()


# ----------------------------------------------------------------- it is WIRED

def test_the_brief_carries_the_uncompressed_state():
    from test_sessions import _brief, _market
    brief, _ = _brief(_market())
    assert brief.continuous is not None
    text = brief.render()
    assert "UNCOMPRESSED" in text
    assert "EFFICIENCY" in text


def test_the_labels_are_still_there_beside_the_numbers():
    """Replacing them would break every threshold, gate and cohort the record
    is grouped by."""
    from test_sessions import _brief, _market
    brief, _ = _brief(_market())
    text = brief.render()
    assert "MEASURED CONTEXT" in text and "UNCOMPRESSED" in text
    assert text.index("MEASURED CONTEXT") < text.index("UNCOMPRESSED")


def test_the_signal_row_records_it():
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._enter) if hasattr(live.LiveDesk, "_enter") \
        else inspect.getsource(live)
    assert '"continuous"' in src


def test_the_ranker_will_test_these_features():
    from golddesk.ranker import FEATURES
    names = {f.name for f in FEATURES}
    assert {"efficiency", "impulse_atr", "retracement", "vol_z"} <= names


def test_the_ranker_reads_them_from_where_the_ledger_writes_them():
    from golddesk.ranker import FEATURES
    row = {"continuous": {"efficiency": 0.42, "impulse_atr": 3.1}}
    f = next(x for x in FEATURES if x.name == "efficiency")
    assert f.read(row) == 0.42
    assert f.scoreable is False, ("efficiency is computed while the brief is "
                                 "built, not at selection; claiming otherwise "
                                 "would weight a value that is always None")
