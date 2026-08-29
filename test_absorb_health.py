"""Absorption: is the pipe from quant carrying anything, or merely running?

    python3 -m pytest test_absorb_health.py -q

THE DISGUISE THESE TESTS STRIP OFF. The nightly pull ran only when
AURUM_QUANT_ROOT was set. When it was not, one line went to a log nobody reads
and the daily report said "0 new finding(s) this cycle" — the identical
sentence a genuinely quiet week produces. A desk that stopped reading the other
desk months ago and a desk with nothing new to read were byte-identical in the
only artifact anyone opens.

Two properties are asserted below and they matter in opposite directions:

  DARK IS A DEFECT      no checkout reachable, two cycles running, and the
                        audit must go red with the reason in the sentence.

  QUIET IS NOT          a checkout that was scanned and produced nothing is
                        quant having a quiet week. A monitor that calls that a
                        fault gets ignored inside a month, and then it is
                        furniture — which is how three earlier checks in this
                        repo died.

And the check must be able to CLEAR. It grades the last recorded cycle, not a
stored timestamp, so restoring the checkout and running once turns it green.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk import absorb_health as AH
from golddesk.absorb_auto import (QUANT_MARKERS, QUANT_ROOT_CANDIDATES,
                                  discover_quant_root)
from golddesk.self_audit import check_absorption_is_reaching_quant


# ------------------------------------------------------------------ discovery

def test_a_checkout_is_found_without_any_environment_variable():
    """The whole defect: absorption used to do nothing at all unless a human
    remembered to set AURUM_QUANT_ROOT on the box."""
    root, basis = discover_quant_root(env_value="")
    if root is None:
        pytest.skip(f"no quant checkout on this machine ({basis})")
    assert basis == "discovered"
    assert any((root / m).exists() for m in QUANT_MARKERS)


def test_the_environment_variable_still_wins(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GAP_REGISTER.md").write_text("x", encoding="utf-8")
    root, basis = discover_quant_root(env_value=str(tmp_path))
    assert root == tmp_path and basis == "env"


def test_a_directory_that_is_not_quant_is_reported_not_accepted(tmp_path):
    """Scanning the wrong repo absorbs nothing and looks exactly like success."""
    root, basis = discover_quant_root(env_value=str(tmp_path))
    assert root is None and basis.startswith("env-wrong-repo")


def test_a_missing_path_is_reported_as_missing(tmp_path):
    root, basis = discover_quant_root(env_value=str(tmp_path / "nope"))
    assert root is None and basis.startswith("env-missing")


def test_the_vps_location_is_in_the_search():
    assert "C:/opt/quant" in QUANT_ROOT_CANDIDATES


def test_self_heal_and_the_cycle_cannot_disagree_about_where_quant_is():
    """Two discovery implementations with different markers is a split brain."""
    import self_heal
    import inspect
    src = inspect.getsource(self_heal._quant_root)
    assert "discover_quant_root" in src


# --------------------------------------------------------------------- health

def test_no_record_is_unmeasured_and_not_fine(tmp_path):
    f = check_absorption_is_reaching_quant(tmp_path)
    assert f.ok is False and "UNMEASURED" in f.detail


def test_a_scan_that_finds_nothing_is_not_a_fault(tmp_path):
    p = tmp_path / "state" / "absorb_health.json"
    AH.record(p, day="2026-08-29", root="/q", basis="discovered",
              scanned=True, n_new=0)
    h = AH.check(p)
    assert h.ok and h.state == "OK"
    assert check_absorption_is_reaching_quant(tmp_path).ok is True


def test_one_dark_cycle_is_a_note_and_two_is_a_defect(tmp_path):
    p = tmp_path / "state" / "absorb_health.json"
    AH.record(p, day="2026-08-28", root=None, basis="absent",
              scanned=False, n_new=0)
    assert AH.check(p).state == "DARK-ONCE"
    assert check_absorption_is_reaching_quant(tmp_path).ok is True
    AH.record(p, day="2026-08-29", root=None, basis="absent",
              scanned=False, n_new=0)
    h = AH.check(p)
    assert h.state == "DARK" and h.dark_streak == 2
    f = check_absorption_is_reaching_quant(tmp_path)
    assert f.ok is False and "DARK" in f.detail


def test_the_check_can_actually_clear(tmp_path):
    """Three checks in this repo stayed BROKEN after their defect was fixed."""
    p = tmp_path / "state" / "absorb_health.json"
    for day in ("2026-08-26", "2026-08-27", "2026-08-28"):
        AH.record(p, day=day, root=None, basis="absent", scanned=False, n_new=0)
    assert check_absorption_is_reaching_quant(tmp_path).ok is False
    AH.record(p, day="2026-08-29", root="/q", basis="discovered",
              scanned=True, n_new=0)
    assert check_absorption_is_reaching_quant(tmp_path).ok is True


def test_rerunning_a_cycle_does_not_manufacture_a_dark_streak(tmp_path):
    p = tmp_path / "state" / "absorb_health.json"
    for _ in range(4):
        AH.record(p, day="2026-08-29", root=None, basis="absent",
                  scanned=False, n_new=0)
    assert AH.check(p).dark_streak == 1


def test_the_last_productive_day_is_remembered_across_quiet_ones(tmp_path):
    p = tmp_path / "state" / "absorb_health.json"
    AH.record(p, day="2026-08-25", root="/q", basis="env", scanned=True, n_new=3)
    for day in ("2026-08-26", "2026-08-27"):
        AH.record(p, day=day, root="/q", basis="env", scanned=True, n_new=0)
    h = AH.check(p)
    assert h.last_finding_day == "2026-08-25" and h.ok


def test_a_corrupt_artifact_does_not_raise(tmp_path):
    p = tmp_path / "state" / "absorb_health.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert AH.check(p).cycles == 0
    assert check_absorption_is_reaching_quant(tmp_path).ok is False


def test_the_report_never_calls_a_quiet_week_a_defect(tmp_path):
    p = tmp_path / "state" / "absorb_health.json"
    AH.record(p, day="2026-08-29", root="/q", basis="env", scanned=True, n_new=0)
    text = AH.check(p).render()
    assert "not a fault" in text and "DARK" not in text


# ----------------------------------------------------------------- it is WIRED

def test_the_cycle_records_health_every_run():
    import inspect

    import aurum_cycle
    src = inspect.getsource(aurum_cycle.step_absorb)
    assert "absorb_health" in src and "discover_quant_root" in src
    # And the darkness has to reach the REPORT, not only the log file.
    assert "ABSORPTION DARK" in src


def test_a_dark_pipe_has_a_fixer():
    from golddesk.remediate import plan

    class F:
        check, ok, detail = "quant absorption", False, "DARK 2 cycle(s)"
    calls = []
    remedies, escalations = plan([F()], restart_desk=lambda: True,
                                 absorb_now=lambda: calls.append(1) or True)
    assert len(remedies) == 1 and not escalations
    assert remedies[0].apply() is True and calls == [1]


def test_without_a_fixer_a_dark_pipe_escalates_rather_than_vanishing():
    from golddesk.remediate import plan

    class F:
        check, ok, detail = "quant absorption", False, "DARK 2 cycle(s)"
    remedies, escalations = plan([F()], restart_desk=lambda: True)
    assert not remedies and len(escalations) == 1
