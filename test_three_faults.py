r"""The three faults the desk had been reporting and nobody had closed.

All three share a shape: the check was RIGHT, said so every fifteen minutes, and
nothing could act on it.

  MACRO      Every brief read `MACRO CONTEXT: UNMEASURED` — gold, whose entire
             bid is macro, read with no dollar, no risk state, no rate context.
             One unofficial web endpoint (yfinance) was the only path, and it
             answered "possibly delisted" for DX-Y.NYB/^GSPC/^VIX on 2026-08-27
             and would not import at all on 2026-08-28. Covered by
             test_drivers_mt5.py.

  EXCURSION  "2 of 7 closed trades carry ZERO observations." The persistence bug
             it named was fixed days earlier and is pinned by
             test_observer_survives_restart.py — but the check scanned every
             closed trade EVER, so two pre-fix trades kept it BROKEN forever. A
             check that cannot go green is furniture within a week.

  INBOX      "last updated 178h ago and the chain runs daily", while Aurum-Sync
             reported exit 0 = SUCCESS. The script prints its explanation to
             stdout; Task Scheduler records only the exit code.

    python3 -m pytest test_three_faults.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.self_audit import check_excursion_survives
from golddesk.task_health import BENIGN_PER_TASK, DELIVERED_NOTHING, TaskInfo, audit

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _trade(obs, mfe=0.0, mae=0.0, opened="2026-08-28T10:00:00+00:00"):
    return {"kind": "TRADE_CLOSED", "observations": obs, "mfe_r": mfe,
            "mae_r": mae, "entry_t0": opened}


def _o(h):
    """A trade opened at hour h on the day, for ordering by OPEN not close."""
    return f"2026-08-28T{h:02d}:00:00+00:00"


# --------------------------------------------------------------------------
# EXCURSION: a fixed defect must be able to clear.

def test_pre_fix_trades_no_longer_hold_the_check_broken_forever():
    """THE LIVE CASE. Two trades closed before the observer persisted its state;
    five since, all carrying excursion. The old check reported BROKEN on this
    for days after the bug was gone."""
    rows = [_trade(0, opened=_o(1)), _trade(0, opened=_o(2))] + [
        _trade(120, 1.4, -0.6, opened=_o(h)) for h in range(5, 10)]
    f = check_excursion_survives(rows)
    assert f.ok, f.detail


def test_but_it_SAYS_the_old_ones_are_gone_rather_than_erasing_them():
    """Those trades' excursion is unrecoverable, and a green check that implies
    the data exists would be a different lie."""
    rows = [_trade(0, opened=_o(1)), _trade(0, opened=_o(2))] + [
        _trade(120, 1.4, -0.6, opened=_o(h)) for h in range(5, 10)]
    d = check_excursion_survives(rows).detail
    assert "2 earlier trade(s) predate the fix" in d
    assert "gone for good" in d


def test_a_REGRESSION_after_the_fix_still_fails():
    """The whole point of keeping the check. If the observer starts losing state
    again, that is a live defect and must be caught."""
    rows = [_trade(0, opened=_o(1)), _trade(120, 1.4, -0.6, opened=_o(2)),
            _trade(0, opened=_o(5)), _trade(90, 1.0, -0.3, opened=_o(6))]
    f = check_excursion_survives(rows)
    assert not f.ok
    assert "SINCE the observer started recording" in f.detail


def test_a_desk_that_has_NEVER_recorded_excursion_still_fails():
    """The case the check was originally written for. Self-calibrating must not
    mean self-excusing: with no good trade to calibrate against, every bare
    trade is a fault."""
    f = check_excursion_survives([_trade(0) for _ in range(4)])
    assert not f.ok
    assert "NONE of 4" in f.detail


def test_a_clean_history_reports_clean_with_no_caveat():
    d = check_excursion_survives([_trade(120, 1.4, -0.6) for _ in range(3)]).detail
    assert "predate the fix" not in d


def test_no_closed_trades_is_not_a_fault():
    assert check_excursion_survives([]).ok


# --------------------------------------------------------------------------
# INBOX: "ran" and "ran and delivered nothing" are different facts.

def _tasks(result):
    def read(name):
        if name != "Aurum-Sync":
            return TaskInfo(name, True, True, NOW - timedelta(minutes=5), 0)
        return TaskInfo(name, True, True, NOW - timedelta(minutes=5), result)
    return read


def _sync(findings):
    return next(f for f in findings if f.check == "Aurum-Sync")


def test_a_transport_that_delivered_nothing_says_so():
    """It exited 0 for this until 2026-08-28, so the task reported SUCCESS while
    the inbox went 178 hours stale against a daily chain."""
    f = _sync(audit(_tasks(3), now=NOW))
    assert "delivered NOTHING" in f.detail
    assert "UNMEASURED" in f.detail


def test_it_points_upstream_rather_than_at_the_transport():
    """The transport is fine. Sending somebody to fix it would waste the one
    thing this check exists to save."""
    f = _sync(audit(_tasks(3), now=NOW))
    assert "SOURCE was absent" in f.detail
    assert "break is upstream" in f.detail


def test_delivered_nothing_is_not_reported_as_a_crash():
    """It ran correctly. Flagging it BROKEN would train the operator to ignore
    the one code that means something."""
    assert _sync(audit(_tasks(3), now=NOW)).ok


def test_a_real_delivery_is_still_a_plain_pass():
    f = _sync(audit(_tasks(0), now=NOW))
    assert f.ok
    assert "delivered NOTHING" not in f.detail


def test_a_genuine_failure_is_still_a_failure():
    """The exemption is for ONE code, not for the task."""
    f = _sync(audit(_tasks(1), now=NOW))
    assert not f.ok
    assert "firing and FAILING" in f.detail


def test_the_code_is_declared_benign_for_this_task_only():
    """One script's vocabulary must never silently excuse another's real
    failure."""
    assert DELIVERED_NOTHING["Aurum-Sync"] == 3
    assert 3 in BENIGN_PER_TASK["Aurum-Sync"]
    assert 3 not in BENIGN_PER_TASK.get("AurumSignalDesk", frozenset())
    assert 3 not in BENIGN_PER_TASK.get("AurumSignalDesk-Update", frozenset())


def test_the_script_and_the_checker_agree_on_the_code():
    """Two files encoding the same convention is how a transport and its monitor
    start disagreeing about what a number means."""
    ps1 = (Path(__file__).parent / "deploy" / "windows"
           / "Sync-QuantFindings.ps1").read_text(encoding="utf-8")
    assert f"exit {DELIVERED_NOTHING['Aurum-Sync']}" in ps1
    assert "exit 0\n}" not in ps1, "the silent-success path is back"


def test_a_trade_that_OPENED_before_the_fix_is_not_blamed_on_current_code():
    """THE LIVE CASE, 2026-08-28. A trade opened under the broken observer and
    closed after the fix carries zero observations through no fault of the
    running desk, and no fix can retroactively give it a path. Ordering by CLOSE
    blamed the desk for a trade it inherited."""
    rows = [_trade(120, 1.4, -0.6, opened=_o(9)),     # observer working from 09
            _trade(0, opened=_o(6))]                  # opened at 06, closed later
    f = check_excursion_survives(rows)
    assert f.ok, f.detail
    assert "predate the fix" in f.detail


def test_a_trade_opened_AFTER_the_observer_worked_is_still_a_regression():
    rows = [_trade(120, 1.4, -0.6, opened=_o(9)),
            _trade(0, opened=_o(11))]
    f = check_excursion_survives(rows)
    assert not f.ok
    assert "SINCE the observer started recording" in f.detail


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
