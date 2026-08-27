r"""Is the analyst still answering, and is anything still watching?

TWO GAPS, both named honestly before they were closed.

ANALYST HEALTH. self_audit asks "is the desk wired"; capture asks "is it still
exploiting". Neither can see the analyst degrade, and it degrades in ways that
look exactly like a careful desk in a quiet market: it stops answering (a wedged
session, an expired login), it answers slower until reads cross the timeout and
vanish, or it quietly answers as a different model. None of those raises.

TASK HEALTH. Every check runs INSIDE a scheduled task. A stopped check looks
exactly like a passing one, so a watchdog that cannot detect its own absence is
one you cannot rely on.

WHAT NEITHER CAN DO, pinned by tests rather than left implied: tell whether a
read is CORRECT (that needs resolved trades), and notice a reboot that never
reached a desktop (nothing is running to notice).

    python3 -m pytest test_analyst_task_health.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst_health import (BLIND_FRACTION, DEFAULT_BUDGET_S,
                                     MIN_READS, MIN_WAKES)
from golddesk.analyst_health import audit as ah_audit
from golddesk.analyst_health import render as ah_render
from golddesk.task_health import STALE_MULTIPLE, TaskInfo
from golddesk.task_health import audit as th_audit
from golddesk.task_health import render as th_render

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def _by(fs, name):
    return next(f for f in fs if f.check == name)


def _ans(n, model="claude-opus-5", lat_ms=60000, when=None):
    return [{"kind": "REFUSAL_MODEL", "ts": (when or NOW).isoformat(),
             "decision": {"model": model, "provider": "claudecode",
                          "latency_ms": lat_ms}} for _ in range(n)]


def _blind(n, when=None):
    return [{"kind": "BLIND", "ts": (when or NOW).isoformat()} for _ in range(n)]


# ======================================================== analyst health

def test_an_analyst_that_stopped_answering_is_caught():
    """The check that would have caught 2026-08-27 in the hour it started."""
    f = _by(ah_audit(_ans(15) + _blind(15), now=NOW), "analyst answering")
    assert not f.ok
    assert "BLIND on those bars, not selective" in f.detail


def test_an_occasional_timeout_is_not_a_fault():
    """A check that fires on one dropped call is one nobody reads."""
    n = MIN_WAKES * 5
    f = _by(ah_audit(_ans(n) + _blind(2), now=NOW), "analyst answering")
    assert f.ok


def test_the_answer_rate_is_UNMEASURED_on_a_thin_window():
    f = _by(ah_audit(_ans(3), now=NOW), "analyst answering")
    assert f.ok and "UNMEASURED" in f.detail


def test_latency_drifting_into_the_budget_is_caught():
    """The failure nobody sees: every read that crosses the timeout is lost, the
    desk simply decides less, and less deciding reads as discipline."""
    f = _by(ah_audit(_ans(MIN_READS, lat_ms=540_000), now=NOW), "analyst latency")
    assert not f.ok
    assert "lost silently" in f.detail


def test_healthy_latency_passes():
    assert _by(ah_audit(_ans(MIN_READS, lat_ms=45_000), now=NOW), "analyst latency").ok


def test_latency_uses_the_median_not_the_mean():
    """One 600s outlier must not define the reading; the question is where the
    BULK of reads sit."""
    rows = _ans(MIN_READS, lat_ms=30_000) + _ans(1, lat_ms=600_000)
    assert _by(ah_audit(rows, now=NOW), "analyst latency").ok


def test_a_silent_model_swap_is_caught():
    """A fallback is supposed to be visible. One nobody notices is a permanent
    downgrade wearing the configured name."""
    f = _by(ah_audit(_ans(5, model="claude-haiku-4-5"), now=NOW,
                     expected_model="claude-opus-5"), "analyst model")
    assert not f.ok
    assert "permanent downgrade" in f.detail


def test_two_models_answering_in_one_window_is_caught():
    """Reads from different models are not comparable evidence and land in the
    same cohort."""
    rows = _ans(5, model="claude-opus-5") + _ans(5, model="claude-sonnet-5")
    f = _by(ah_audit(rows, now=NOW), "analyst model")
    assert not f.ok
    assert "not comparable evidence" in f.detail


def test_the_report_states_what_it_cannot_measure():
    """It cannot tell whether a read is CORRECT, and must say so rather than
    letting a green board imply the analyst is good."""
    text = ah_render(ah_audit(_ans(15) + _blind(15), now=NOW))
    assert "can tell whether a read is CORRECT" in text
    assert "resolved trades" in text


# =========================================================== task health

def _reader(overrides=None):
    over = overrides or {}
    def read(name):
        if name in over:
            return over[name]
        return TaskInfo(name, True, True, NOW, 0)
    return read


def test_all_healthy_passes():
    fs = th_audit(_reader(), now=NOW)
    assert all(f.ok for f in fs)
    assert "every watchdog is running" in th_render(fs)


def test_a_disabled_task_is_caught_AND_marked_fixable():
    name = "AurumSignalDesk-SelfHeal"
    f = _by(th_audit(_reader({name: TaskInfo(name, True, False, NOW, 0)}),
                     now=NOW), name)
    assert not f.ok and f.fixable
    assert "switched off" in f.detail


def test_a_missing_task_is_caught_and_NOT_fixable():
    """Registering a task changes machine configuration, can prompt, and can
    fail leaving the desk worse off. That stays the operator's act."""
    name = "AurumSignalDesk-Cycle"
    f = _by(th_audit(_reader({name: TaskInfo(name, False, False, None, None)}),
                     now=NOW), name)
    assert not f.ok and not f.fixable
    assert "Install-AurumStartup" in f.detail


def test_a_task_failing_every_run_is_caught():
    name = "AurumSignalDesk-Update"
    f = _by(th_audit(_reader({name: TaskInfo(name, True, True, NOW, 1)}),
                     now=NOW), name)
    assert not f.ok
    assert "no restart fixes" in f.detail


def test_a_running_task_is_not_reported_as_failing():
    """267009 is 'still running', not an error."""
    name = "AurumSignalDesk"
    assert _by(th_audit(_reader({name: TaskInfo(name, True, True, NOW, 267009)}),
                        now=NOW), name).ok


def test_a_task_that_stopped_firing_is_caught():
    name = "AurumSignalDesk-SelfHeal"
    old = NOW - timedelta(minutes=15) * (STALE_MULTIPLE + 2)
    f = _by(th_audit(_reader({name: TaskInfo(name, True, True, old, 0)}),
                     now=NOW), name)
    assert not f.ok
    assert "stopped firing" in f.detail


def test_one_late_run_is_not_stale():
    """A machine can be busy; staleness is several intervals, never one."""
    name = "AurumSignalDesk-SelfHeal"
    late = NOW - timedelta(minutes=20)
    assert _by(th_audit(_reader({name: TaskInfo(name, True, True, late, 0)}),
                        now=NOW), name).ok


def test_an_unreadable_task_is_UNMEASURED_not_healthy():
    def boom(name):
        raise OSError("schtasks not found")
    fs = th_audit(boom, now=NOW)
    assert all(f.ok for f in fs)
    assert all("UNMEASURED" in f.detail for f in fs)
    assert "Not the same as healthy" in fs[0].detail


def test_every_scheduled_task_is_actually_checked():
    """A task absent from EXPECTED is a task nothing watches."""
    from golddesk.task_health import EXPECTED
    src = (Path(__file__).parent / "deploy" / "windows" /
           "Install-AurumStartup.ps1").read_text(encoding="utf-8")
    for suffix in ("-Watchdog", "-SelfHeal", "-Update", "-Cycle", "-VantageSpread"):
        assert f"AurumSignalDesk{suffix}" in EXPECTED, suffix
        assert suffix in src, f"{suffix} is watched but never registered"


def test_the_report_names_the_case_it_cannot_cover():
    """Every task runs at LogonType Interactive, so a reboot that never reaches
    a desktop takes the desk AND all of these together — and nothing is left
    running to notice. Written down rather than assumed away."""
    name = "AurumSignalDesk-Cycle"
    text = th_render(th_audit(_reader({name: TaskInfo(name, False, False, None, None)}),
                              now=NOW))
    assert "LogonType Interactive" in text
    assert "nothing is left running to notice" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_all_unreadable_does_not_report_every_watchdog_running():
    """UNMEASURED IS NOT A PASS, and the header is where that lie would live.

    Every finding coming back unreadable — no schtasks, a permissions problem,
    the wrong OS — printed "every watchdog is running": absence read as a clean
    answer, about the one component whose entire job is noticing absence.
    """
    def boom(name):
        raise OSError("schtasks not found")
    text = th_render(th_audit(boom, now=NOW))
    assert "every watchdog is running" not in text
    assert "NOTHING COULD BE READ" in text
    assert "not the same as fine" in text


def test_a_partial_read_says_how_many_were_unreadable():
    name = "AurumSignalDesk-Cycle"
    def read(n):
        if n == name:
            raise OSError("access denied")
        return TaskInfo(n, True, True, NOW, 0)
    text = th_render(th_audit(read, now=NOW))
    assert "1 UNREADABLE" in text
    assert "every watchdog is running" not in text


def test_a_genuine_all_clear_still_says_so():
    """The honest pass must survive the fix, or the check becomes noise."""
    assert "every watchdog is running" in th_render(th_audit(_reader(), now=NOW))
