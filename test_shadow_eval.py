"""Tests for the forward-evidence feed.

The load-bearing property is that a cell spec ROUND-TRIPS. If parsing is lossy,
the forward record scores a different strategy than the one screened, and the
promotion that follows is about something nobody searched for. So the parser is
required to refuse rather than guess.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from golddesk.shadow_eval import (UnparseableCell, evaluate, parse_cell,
                                  render)


# ------------------------------------------------------------------- parsing

def test_parses_a_windowed_session_cell():
    s = parse_cell("XAUUSD|session_breakout.asia|rr=2.5")
    assert s["symbol"] == "XAUUSD"
    assert s["family"] == "session_breakout"
    assert s["window"] == "asia"
    assert s["params"] == {"rr": 2.5}


def test_parses_mixed_types():
    s = parse_cell("NZDJPY|monday_gap|mode=fade,rr=2.5,signal_hour=7")
    assert s["params"] == {"mode": "fade", "rr": 2.5, "signal_hour": 7}
    assert isinstance(s["params"]["signal_hour"], int)
    assert isinstance(s["params"]["rr"], float)


def test_parses_a_cell_with_no_params():
    s = parse_cell("EURUSD|dow_effect")
    assert s["params"] == {} and s["window"] is None


def test_refuses_a_malformed_cell():
    with pytest.raises(UnparseableCell):
        parse_cell("XAUUSD")


def test_refuses_a_bad_parameter_rather_than_dropping_it():
    """A DROPPED PARAMETER IS A DIFFERENT STRATEGY. Guessing here means the
    forward record measures something nobody screened."""
    with pytest.raises(UnparseableCell):
        parse_cell("XAUUSD|monday_gap|rr")


def test_window_survives_the_round_trip():
    a = parse_cell("XAUUSD|session_breakout.asia|rr=2.0")
    b = parse_cell("XAUUSD|session_breakout.afternoon|rr=2.0")
    assert a["window"] != b["window"], "two different cells parsed the same"


# ------------------------------------------------------------------ evaluate

class _T:
    def __init__(self, r, ex):
        self.r_multiple = r
        self.exit_time = ex


class _Res:
    def __init__(self, trades):
        self.trades = trades


def _harness(trades):
    fam = type("F", (), {"family_monday_gap": staticmethod(lambda b, **k: [1])})
    return (lambda s: list(range(200)), fam, {},
            lambda s: None, lambda b, s, c: _Res(trades))


def test_counts_only_trades_that_exited_on_the_day():
    day = date(2026, 3, 2)
    tr = [_T(1.0, datetime(2026, 3, 2, 9, tzinfo=timezone.utc)),
          _T(2.0, datetime(2026, 3, 1, 9, tzinfo=timezone.utc))]
    load, fam, win, costs, run = _harness(tr)
    r, t, n = evaluate(["X|monday_gap|rr=2.0"], day, load, fam, win, costs, run)
    assert r["X|monday_gap|rr=2.0"] == 1.0
    assert t["X|monday_gap|rr=2.0"] == 1


def test_an_open_position_contributes_nothing_not_zero():
    """Booking an unknown as a zero drags every forward t toward the null."""
    day = date(2026, 3, 2)
    load, fam, win, costs, run = _harness([_T(1.0, None)])
    r, t, _ = evaluate(["X|monday_gap|rr=2.0"], day, load, fam, win, costs, run)
    assert r == {} and t == {}


def test_a_day_with_no_fills_is_absent_from_the_record():
    day = date(2026, 3, 5)
    tr = [_T(1.0, datetime(2026, 3, 2, 9, tzinfo=timezone.utc))]
    load, fam, win, costs, run = _harness(tr)
    r, t, _ = evaluate(["X|monday_gap|rr=2.0"], day, load, fam, win, costs, run)
    assert r == {}, "a quiet day must not advance a shadow clock"


def test_multiple_fills_sum_and_are_counted():
    day = date(2026, 3, 2)
    d = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    load, fam, win, costs, run = _harness([_T(1.0, d), _T(-0.5, d), _T(2.0, d)])
    r, t, _ = evaluate(["X|monday_gap|rr=2.0"], day, load, fam, win, costs, run)
    assert r["X|monday_gap|rr=2.0"] == pytest.approx(2.5)
    assert t["X|monday_gap|rr=2.0"] == 3


def test_non_finite_r_is_dropped():
    day = date(2026, 3, 2)
    d = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    load, fam, win, costs, run = _harness([_T(float("nan"), d), _T(1.0, d)])
    r, t, _ = evaluate(["X|monday_gap|rr=2.0"], day, load, fam, win, costs, run)
    assert t["X|monday_gap|rr=2.0"] == 1


def test_unparseable_cells_are_noted_not_silently_skipped():
    load, fam, win, costs, run = _harness([])
    _, _, n = evaluate(["nonsense"], date(2026, 3, 2), load, fam, win, costs, run)
    assert n and "nonsense" in n[0]


def test_missing_bars_are_noted_and_do_not_raise():
    def load(s):
        raise FileNotFoundError(f"{s}.parquet")
    fam = type("F", (), {"family_monday_gap": staticmethod(lambda b, **k: [1])})
    r, t, n = evaluate(["Z|monday_gap|rr=2.0"], date(2026, 3, 2), load, fam, {},
                       lambda s: None, lambda b, s, c: _Res([]))
    assert r == {} and any("bars unavailable" in x for x in n)


def test_render_says_a_quiet_day_is_normal():
    out = render({}, {}, [], date(2026, 3, 2))
    assert "normal day" in out and "advances no shadow clock" in out


def test_render_warns_that_unevaluable_is_not_flat():
    out = render({}, {}, ["X: boom"], date(2026, 3, 2))
    assert "NOT counted as a flat day" in out
