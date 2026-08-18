"""Analogue matching cheats in three ways, and each one makes it score
beautifully while having learned nothing.
"""
from __future__ import annotations

import numpy as np
import pytest

from golddesk.analogue_seq import (
    MAX_MEAN_DISTANCE, MIN_HISTORY, AnalogueModel, build_specialist)

RNG = np.random.default_rng(21)


def walk(n=3000, drift=0.0):
    return 2000.0 + np.cumsum(RNG.normal(drift, 1.0, size=n))


def cyclic(n=3000, period=40, amp=20.0):
    """A series with a genuinely repeating shape, so a real analogue exists."""
    t = np.arange(n)
    return 2000.0 + amp * np.sin(2 * np.pi * t / period) + RNG.normal(0, 0.5, n)


# ------------------------------------------------------------- the three cheats

def test_a_window_cannot_match_itself_or_its_overlapping_siblings():
    """THE LEAK. The nearest neighbour of a window IS the window, and the
    next-nearest contain the exact bars being predicted."""
    m = AnalogueModel(window=24, horizon=4).fit(walk())
    gap = m.window + m.horizon
    for q in (100, 900, 2000):
        elig = m._eligible(q)
        assert not elig[np.abs(m._ends - q) < gap].any()


def test_shape_matches_across_wildly_different_price_levels():
    """Raw prices make 2026 match only 2026, because everything else is at a
    different level."""
    m = AnalogueModel(window=24, horizon=4).fit(cyclic())
    lo = cyclic(n=200)[:24]
    hi = lo + 2500.0
    a, _ = m.predict(lo)
    b, _ = m.predict(hi)
    assert a is not None and b is not None
    assert abs(a - b) < 1e-9, "a level shift changed the read"


def test_no_close_analogue_returns_UNAVAILABLE_rather_than_an_average():
    """With no close match the mean of k neighbours is still a number, and it is
    noise wearing a decimal point."""
    m = AnalogueModel(window=24, horizon=4, max_distance=0.01).fit(walk())
    out, why = m.predict(walk(n=100)[:24])
    assert out is None and "nothing in the desk's history looks like now" in why


# --------------------------------------------------------- it must be able to fail

def test_a_thin_library_refuses_to_answer():
    m = AnalogueModel(window=24, horizon=4).fit(walk(n=200))
    out, why = m.predict(walk(n=100)[:24])
    assert out is None and "no analogues in it" in why


def test_a_flat_window_has_no_shape_to_match():
    m = AnalogueModel(window=24, horizon=4).fit(cyclic())
    out, why = m.predict(np.full(24, 2000.0))
    assert out is None and "flat" in why


def test_too_few_bars_is_refused():
    m = AnalogueModel(window=24, horizon=4).fit(cyclic())
    out, why = m.predict(np.full(10, 2000.0))
    assert out is None and "required" in why


def test_a_random_walk_yields_a_weak_read():
    """THE TEST THAT MAKES A STRONG READ MEAN ANYTHING. On a driftless walk the
    analogues should not agree on direction."""
    m = AnalogueModel(window=24, horizon=4).fit(walk())
    reads = []
    for _ in range(30):
        out, _ = m.predict(walk(n=60)[:24])
        if out is not None:
            reads.append(out)
    assert reads, "the model refused everything; the test proves nothing"
    assert abs(float(np.mean(reads))) < 0.30, f"mean read {np.mean(reads):+.3f}"


def test_a_genuinely_repeating_series_is_read_with_conviction():
    """It must be able to find something, or the refusals prove nothing."""
    m = AnalogueModel(window=24, horizon=4).fit(cyclic())
    out, why = m.predict(cyclic(n=200)[:24])
    assert out is not None
    assert "agreeing on direction" in why


# ------------------------------------------------------------- the output

def test_the_read_is_bounded():
    """A specialist is only entitled to a bounded opinion."""
    m = AnalogueModel(window=24, horizon=4).fit(cyclic(amp=500.0))
    out, _ = m.predict(cyclic(n=200, amp=500.0)[:24])
    assert out is None or -1.0 <= out <= 1.0


def test_outcomes_are_in_volatility_units_not_dollars():
    """In dollars, high-volatility eras dominate the average for reasons
    unrelated to shape."""
    calm = AnalogueModel(window=24, horizon=4).fit(cyclic(amp=5.0))
    wild = AnalogueModel(window=24, horizon=4).fit(cyclic(amp=500.0))
    assert calm._outcomes.std() == pytest.approx(wild._outcomes.std(), rel=0.35)


def test_a_closer_analogue_counts_for_more_than_a_distant_one():
    """A flat average throws away the thing that makes one match better."""
    import inspect
    src = inspect.getsource(AnalogueModel.predict)
    assert "1.0 / (d[idx]" in src


# ----------------------------------------------------- it needs no weights

def test_the_specialist_needs_nothing_downloaded():
    """The whole point: no checkpoint, no licence, no GPU."""
    s = build_specialist(cyclic(), window=24, horizon=4)
    assert s.predict_fn is not None
    src = open("golddesk/analogue_seq.py").read()
    for banned in ("torch", "tensorflow", "huggingface", "from_pretrained",
                   "requests.get", "urllib"):
        assert banned not in src


def test_the_specialist_reads_a_snapshot_and_answers():
    from datetime import datetime, timedelta, timezone
    from golddesk.snapshot import SnapshotBuilder

    class Bar:
        def __init__(self, t, c):
            self.time, self.open, self.high, self.low, self.close = t, c, c + 1, c - 1, c

    closes = cyclic()
    t0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    bars = [Bar(t0 - timedelta(minutes=15 * (40 - i)), float(closes[i]))
            for i in range(40)]
    b = SnapshotBuilder("XAUUSD", "M15", t0)
    b.add_bars("m15", bars, "M15", count=40)
    s = build_specialist(closes, window=24, horizon=4)
    r = s.read(b.build())
    assert r.direction in ("LONG", "SHORT", "FLAT")


def test_an_unmatched_state_reads_UNAVAILABLE_not_FLAT():
    """A missing answer must not read downstream as 'the model sees nothing
    here' — that is an observation, and nobody made it."""
    from datetime import datetime, timedelta, timezone
    from golddesk.snapshot import SnapshotBuilder

    class Bar:
        def __init__(self, t, c):
            self.time, self.open, self.high, self.low, self.close = t, c, c + 1, c - 1, c

    t0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    bars = [Bar(t0 - timedelta(minutes=15 * (40 - i)), 2000.0 + i) for i in range(40)]
    b = SnapshotBuilder("XAUUSD", "M15", t0)
    b.add_bars("m15", bars, "M15", count=40)
    # A library too thin to answer.
    s = build_specialist(walk(n=100), window=24, horizon=4)
    r = s.read(b.build())
    assert not r.available and r.direction == "FLAT"
