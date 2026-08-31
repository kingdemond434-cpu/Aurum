"""The desk must read its own resolved trades, and something must run the loop.

TWO INDEPENDENT BREAKS, both found when the operator asked why it was not
improving on its own.

BREAK 1 — THE LIVE PATH NEVER READ ITS OWN RESULTS.

`build_cohorts()` turns resolved trades into measured hit rates. It existed, was
correct, and was called by adapt.py and acceptance.py — never by build_service,
the thing that constructs the LIVE desk. So `LiveDesk.cohorts` was None forever
and every consumer silently took its no-history path:

    ev_gate        COLD_START_PRIOR on EVERY decision, so a mechanism with
                   eighty wins priced exactly like one never traded
    _size          adaptive sizing saw cohort_n=0
    _edge_r        no measured edge, so execution advice stayed silent
    evidence_tier  could never reach T1 MEASURED, by construction

Every part worked. Nothing joined them, so the desk re-derived ignorance at
every boot — and the same `rows` was already being read one line above for
regime history and then thrown away.

BREAK 2 — NOTHING ON WINDOWS RAN THE LEARNING CYCLE.

aurum_cycle.py holds decay detection, missed-money, the management
counterfactual, the stop autopsy and the growth re-solve. Its only launcher in
the repo was deploy/aurum-cycle.service — a systemd unit for /opt/aurum, on
Linux. On the Windows desk that file is inert, so every self-correction step was
written, tested, correct and executed by nobody.

    python3 -m pytest test_desk_learns.py -q
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

SERVICE = Path(__file__).parent / "golddesk" / "service.py"
INSTALLER = Path(__file__).parent / "deploy" / "windows" / "Install-AurumStartup.ps1"


# --------------------------------------------- break 1: it reads its results

def test_build_service_builds_cohorts_from_the_ledger():
    src = SERVICE.read_text(encoding="utf-8")
    assert "build_cohorts" in src, (
        "build_service does not build cohorts — the live desk cannot price a "
        "mechanism from its own history no matter how many trades resolve")


def test_the_cohorts_actually_reach_the_desk():
    """Building them and not passing them would look identical in a log."""
    src = SERVICE.read_text(encoding="utf-8")
    assert "cohorts=cohorts" in src


def test_cohorts_is_a_real_keyword_on_LiveDesk():
    """Guards a rename: passing cohorts= to a constructor that no longer takes
    it would be a TypeError at boot, not a silent degrade — but a renamed
    parameter with a default would be silent, which is worse."""
    import inspect
    from golddesk.live import LiveDesk
    assert "cohorts" in inspect.signature(LiveDesk.__init__).parameters


def test_a_measured_cohort_changes_the_gate_it_feeds():
    """The point of wiring it: with history, ev_gate must stop pricing off the
    cold-start prior. Without this the wiring could be inert and still pass."""
    from golddesk.opportunity import CohortStat, ev_gate
    thin_rr = 0.8
    cold = ev_gate(thin_rr, 0.05, "m", None, shadow=False)
    assert cold.basis == "COLD_START_PRIOR"
    good = {"m": CohortStat("m", 120, 96, 0.9, 0.80, 0.78, informative=True)}
    warm = ev_gate(thin_rr, 0.05, "m", good, shadow=False)
    assert warm.basis == "COHORT"
    assert warm.take, "a mechanism winning 78% still could not clear the gate"


def test_build_cohorts_reads_resolved_trades():
    """End to end on the ledger shape the desk actually writes."""
    from golddesk.opportunity import build_cohorts
    rows = [{"kind": "TRADE_CLOSED", "mechanism_name": "shelf-retest",
             "realised_r": r, "entry_t0": str(i)}
            for i, r in enumerate([1.8, -1.0, 2.1, -1.0, 0.9])]
    c = build_cohorts(rows)
    assert "shelf-retest" in c
    assert c["shelf-retest"].n == 5


def test_an_empty_ledger_yields_no_cohorts_rather_than_a_fake_one():
    """Absence must stay absence — a fabricated cohort would price every
    mechanism off nothing while claiming to be measured."""
    from golddesk.opportunity import build_cohorts
    assert build_cohorts([]) == {}


# ------------------------------------------ break 2: something runs the loop

def test_the_installer_registers_a_cycle_task():
    src = INSTALLER.read_text(encoding="utf-8")
    assert "aurum_cycle.py" in src, (
        "no Windows task runs the learning cycle — every self-correction step "
        "is correct and executed by nobody (III.16)")
    assert "-Cycle" in src


def test_the_cycle_task_is_removed_by_the_uninstaller():
    """A task the -Remove path forgets is one that keeps firing against a
    deleted install."""
    src = INSTALLER.read_text(encoding="utf-8")
    block = src[src.index("foreach ($t in @($TaskName"):][:400]
    assert "-Cycle" in block


def test_the_cycle_task_survives_a_box_that_was_off():
    """A daily task that fires while the machine is down is SILENTLY skipped,
    and a learning loop that misses days without saying so is the same defect
    one level up."""
    src = INSTALLER.read_text(encoding="utf-8")
    i = src.index("$cySettings")
    assert "StartWhenAvailable" in src[i:i + 400]


def test_every_learning_step_is_actually_in_the_cycle():
    """The task is worthless if the steps are not on the list it runs."""
    import aurum_cycle as C
    names = {n for n, _ in C.STEPS}
    for step in ("evidence", "decay", "missed_money", "stop_autopsy"):
        assert step in names, f"{step} is not in the cycle the scheduler runs"


def test_the_cycle_is_importable_and_runnable_headless():
    """It runs from a scheduled task with no terminal; an import-time failure
    would show up only in a log nobody reads."""
    import aurum_cycle as C
    assert callable(C.run)
    assert C.STEPS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ------------------------------------- the chain only works in order

#: quant's installer registers Aurum-Sync at this time. Duplicated here on
#: purpose: the two repos deploy independently, and a silent drift between them
#: is exactly the failure this test exists to catch.
SYNC_MINUTES = 22 * 60 + 15


def _cycle_minutes() -> int:
    src = INSTALLER.read_text(encoding="utf-8")
    i = src.index("$cyTrigger = New-ScheduledTaskTrigger -Daily -At")
    line = src[i:src.index("\n", i)]
    import re
    h, m = re.search(r"AddHours\((\d+)\)\.AddMinutes\((\d+)\)", line).groups()
    return int(h) * 60 + int(m)


def test_the_cycle_runs_AFTER_the_quant_findings_arrive():
    """THE ORDERING IS A DEPENDENCY, NOT A PREFERENCE.

        21:45  quant writes aurum_findings.jsonl
        22:15  Aurum-Sync carries it into inbox/quant_findings.jsonl
        ~22:40 this cycle runs step_absorb, which READS that inbox

    First shipped at 22:10 — five minutes BEFORE the sync that feeds it. Every
    quant finding would have been absorbed a day late, and silently: step_absorb
    reporting "0 new findings" is indistinguishable from the quant desk having
    found nothing.
    """
    assert _cycle_minutes() > SYNC_MINUTES, (
        f"the cycle runs at {_cycle_minutes() // 60:02d}:{_cycle_minutes() % 60:02d}, "
        f"before Aurum-Sync at 22:15 — it would read yesterday's inbox forever")


def test_it_leaves_room_for_the_syncs_retry_budget():
    """Aurum-Sync carries RestartCount 3 at 5-minute intervals, so a first
    attempt that fails can still land ~15 minutes late. Starting the moment the
    sync nominally fires would race it."""
    assert _cycle_minutes() - SYNC_MINUTES >= 15


def test_absorb_is_on_the_list_the_scheduler_runs():
    """The ordering is pointless if the step that reads the inbox is not run."""
    import aurum_cycle as C
    assert "absorb" in {n for n, _ in C.STEPS}


# ------------------------------------------- the box updates itself

UPDATER = Path(__file__).parent / "deploy" / "windows" / "Update-AurumDesk.ps1"


def test_an_updater_exists_and_is_scheduled():
    """Every change used to need the operator at a terminal, so a fix sat unused
    for as long as they were away from the box."""
    assert UPDATER.exists()
    src = INSTALLER.read_text(encoding="utf-8")
    assert "Update-AurumDesk.ps1" in src and "-Update" in src


def test_it_tests_the_new_code_BEFORE_swapping():
    """A blind pull-and-restart loop is worse than manual updates: it can pull a
    broken commit and restart into a crash loop with nobody watching."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "pytest" in src
    assert "git reset --hard $before" in src, "no rollback on a red suite"


def test_it_will_not_restart_on_an_open_position_by_default():
    src = UPDATER.read_text(encoding="utf-8")
    assert "$st.open_trade" in src
    assert "-not $Force" in src


def test_it_refuses_to_touch_a_dirty_tree():
    """A dirty tree on the desk box is far likelier to be someone mid-
    investigation than junk, and a script that resolves that by discarding it
    eventually discards the one thing that mattered."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "git status --porcelain" in src
    assert "Not touching it" in src


def test_it_refuses_a_non_fast_forward():
    """Diverged history cannot be reconciled automatically without risking
    discarding one side."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "merge --ff-only" in src
    assert "ABORT" in src


def test_it_does_not_re_register_scheduled_tasks():
    """Registering tasks changes machine configuration and can fail leaving the
    desk unregistered — that stays a deliberate operator act."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "Register-ScheduledTask" not in src
    assert "Install-AurumStartup" in src, "it must at least SAY when one is needed"


def test_the_update_task_is_removed_by_the_uninstaller():
    src = INSTALLER.read_text(encoding="utf-8")
    block = src[src.index("foreach ($t in @($TaskName"):][:400]
    assert "-Update" in block


# ---------------- the updater must never fail without saying why

def test_it_resolves_git_explicitly():
    """A scheduled task runs with a minimal environment. git is frequently on
    the interactive user's PATH and not on the task's, and with
    ErrorActionPreference 'Stop' the first git call then threw before the first
    log write — so the task exited 1 and wrote NO LOG AT ALL. Observed live: the
    watchdog said "firing and FAILING" and logs\\update.log did not exist."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "Get-Command git" in src
    assert "does not inherit the" in src, "the abort must explain the cause"


def test_nothing_can_fail_without_a_line_in_the_log():
    """An updater that dies without a line is indistinguishable from one that
    ran and found nothing, and the watchdog can only report 'exited 1'."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "} catch {" in src
    assert "UNHANDLED:" in src
    assert "ScriptLineNumber" in src, "the catch must say WHERE"


def test_a_no_op_run_still_leaves_a_trace():
    """Silence on 'nothing to do' is the same silence as a crash, and telling
    them apart is the whole point of the log."""
    src = UPDATER.read_text(encoding="utf-8")
    assert "update_lastcheck.txt" in src
    i = src.index("if ($before -eq $after)")
    assert "update_lastcheck" in src[i:i + 700]


def test_the_heartbeat_does_not_pollute_the_change_log():
    """48 no-ops a day would bury the runs that actually did something."""
    src = UPDATER.read_text(encoding="utf-8")
    i = src.index("if ($before -eq $after)")
    block = src[i:i + 700]
    assert "update_lastcheck.txt" in block
    assert 'Add-Content -Path $log' not in block
