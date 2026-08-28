r"""A stop of 2.49 ATR that is 0.27 of the bar the market just printed.

WHAT PROMPTED THIS. On 2026-08-28 gold fell about 140 points in an afternoon.
The desk was SHORT repeatedly on the way down — the direction was right nearly
every time — and it lost money, because each stop was taken on a small retrace
before price continued another sixty points its way.

THE HYPOTHESIS, stated so it can be wrong. Stops sit beyond structure by
`stop_atr_buffer * ATR`, and ATR is a TRAILING mean, so in an expansion it lags
by construction. The stop is then narrowest, relative to what price is actually
doing, exactly when trends travel furthest. The live signal makes it concrete:

    stop 20.95 points  ->  stop_in_atr 2.49   (looks generous)
                       ->  stop_in_range 0.27 (an ordinary retrace reaches it)

WHY THIS MEASURES AND DOES NOT FIX. "Give gold more room" feels right, is
trivial, and is exactly what an overfit looks like from the inside. Fourteen
resolved trades and one dramatic afternoon is a sample of one regime, and the
same reasoning would widen stops into every chop day after it.

    python3 -m pytest test_stop_regime.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.stop_regime import EXPANDING_ABOVE, MIN_STOPPED, assess, measure


# --------------------------------------------------------------------------
# Two views of the same distance.

def test_the_live_case_looks_generous_in_atr_and_tight_in_range():
    """THE WHOLE POINT, from the real signal. Both numbers describe one stop and
    they disagree, which is the disagreement worth recording."""
    m = measure(20.95, atr=8.4, bar_range=77.0, range_vs_mean=3.2)
    assert m["stop_in_atr"] > 2.0
    assert m["stop_in_range"] < 0.3


def test_calm_tape_makes_the_two_views_agree():
    """When the market behaves like its trailing average, a stop measured either
    way says the same thing — which is why the disagreement is informative."""
    m = measure(8.0, atr=8.0, bar_range=9.0, range_vs_mean=1.0)
    assert m["stop_in_atr"] == pytest.approx(1.0)
    assert 0.8 < m["stop_in_range"] < 1.0
    assert m["expanding"] is False


def test_expansion_is_flagged_from_the_bar_against_its_own_mean():
    assert measure(10, 5, 20, EXPANDING_ABOVE + 0.1)["expanding"] is True
    assert measure(10, 5, 20, EXPANDING_ABOVE - 0.1)["expanding"] is False


def test_missing_inputs_produce_missing_fields_not_zeros():
    """UNMEASURED is not zero. A stop_in_range of 0.0 would read as an
    infinitely tight stop, which is the opposite of 'we could not measure it'."""
    m = measure(10.0, atr=None, bar_range=None, range_vs_mean=None)
    assert "stop_in_atr" not in m and "stop_in_range" not in m
    assert m["stop_distance"] == 10.0


def test_a_zero_atr_does_not_divide_by_zero():
    assert "stop_in_atr" not in measure(10.0, 0.0, 20.0, 1.0)


# --------------------------------------------------------------------------
# It refuses to conclude from a thin sample.

def _sig(t0, expanding, stop_in_range=0.3):
    return {"kind": "SIGNAL", "t0": t0,
            "decision": {"stop_regime": {"expanding": expanding,
                                         "stop_in_range": stop_in_range}}}


def _closed(t0, reason="STOP", valid=True):
    return {"kind": "TRADE_CLOSED", "entry_t0": t0, "reason": reason,
            "evidence_valid": valid, "realised_r": -1.0}


def test_a_thin_sample_is_UNMEASURED_and_says_the_question_is_open():
    rows = [_sig("t1", True), _closed("t1")]
    v = assess(rows)
    assert v.verdict == "UNMEASURED"
    assert "OPEN QUESTION, not a finding" in v.render()


def test_the_unmeasured_message_warns_against_the_obvious_fix():
    assert "overfits" in assess([]).render()


def test_it_needs_BOTH_regimes_to_compare():
    """A comparison against nothing is not a comparison. All-expanding data
    cannot say whether expansions are different."""
    rows = []
    for i in range(MIN_STOPPED + 4):
        rows += [_sig(f"t{i}", True), _closed(f"t{i}")]
    assert assess(rows).verdict == "UNMEASURED"


# --------------------------------------------------------------------------
# With both regimes present it compares, and only compares.

def _both(n_exp_stop, n_exp_win, n_calm_stop, n_calm_win):
    rows, k = [], 0
    for _ in range(n_exp_stop):
        rows += [_sig(f"e{k}", True, 0.25), _closed(f"e{k}", "STOP")]; k += 1
    for _ in range(n_exp_win):
        rows += [_sig(f"e{k}", True, 0.25), _closed(f"e{k}", "TARGET")]; k += 1
    for _ in range(n_calm_stop):
        rows += [_sig(f"c{k}", False, 0.90), _closed(f"c{k}", "STOP")]; k += 1
    for _ in range(n_calm_win):
        rows += [_sig(f"c{k}", False, 0.90), _closed(f"c{k}", "TARGET")]; k += 1
    return rows


def test_it_reports_the_two_stop_out_rates_side_by_side():
    v = assess(_both(6, 2, 3, 9))
    assert v.verdict == "MEASURED"
    assert v.stopped_rate_expanding == pytest.approx(6 / 8)
    assert v.stopped_rate_calm == pytest.approx(3 / 12)


def test_it_reports_how_tight_the_stops_were_in_each_regime():
    v = assess(_both(6, 2, 3, 9))
    assert v.median_stop_in_range_expanding == pytest.approx(0.25)
    assert v.median_stop_in_range_calm == pytest.approx(0.90)
    assert "the smaller number is the tighter stop" in v.render()


def test_it_calls_itself_a_comparison_rather_than_a_verdict():
    """A wider stop is only justified if the trades it saves outweigh the larger
    loss on the ones it does not, and this measurement cannot see that half."""
    r = assess(_both(6, 2, 3, 9)).render()
    assert "COMPARISON, not a verdict" in r
    assert "outweigh the larger loss" in r


def test_quarantined_trades_are_excluded():
    """Same rule as everywhere else: a row whose path was never observed cannot
    testify about whether its stop was reached."""
    rows = _both(6, 2, 3, 9)
    rows += [_sig("bad", True, 0.1), _closed("bad", "STOP", valid=False)]
    assert assess(rows).stopped_rate_expanding == pytest.approx(6 / 8)


def test_signals_with_no_regime_context_are_skipped_not_defaulted():
    """Older rows predate the field. Defaulting them into 'calm' would invent a
    comparison group out of missing data."""
    rows = _both(6, 2, 3, 9)
    rows += [{"kind": "SIGNAL", "t0": "old", "decision": {}},
             _closed("old", "STOP")]
    assert assess(rows).n_stopped == 9


# --------------------------------------------------------------------------
# Wired, and never fatal.

def test_the_daily_cycle_runs_it():
    import aurum_cycle
    assert any(n == "stop_regime" for n, _ in aurum_cycle.STEPS)


def test_recording_it_can_never_take_down_a_signal():
    """Measurement attached to a decision must not be able to kill the decision
    it describes."""
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._stop_regime)
    assert "except Exception" in src and "return {}" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
