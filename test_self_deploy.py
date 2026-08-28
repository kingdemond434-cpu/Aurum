r"""The updater's task was failing, so nothing could deploy — including the fix.

THE LOOP. AurumSignalDesk-Update exits 1 every thirty minutes. Nothing deploys.
The fixes that would report WHY it fails are among the things that do not deploy.
The only exit has been a human running `git pull` by hand — three times in one
day, on a desk whose stated requirement is that nothing be manual.

Meanwhile self_heal's own scheduled task, on the same box under the same account
with the same interpreter, runs cleanly every fifteen minutes. Whatever is wrong
lives in that task's environment, not in the script — the operator's manual pull
succeeded every time.

SAME SCRIPT, SAME GUARDS, A TRIGGER THAT WORKS. This is not a second deployer
and it is not new authority: Update-AurumDesk.ps1 keeps every safety it has.

    python3 -m pytest test_self_deploy.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import self_heal
from golddesk.remediate import plan


@dataclass
class F:
    check: str
    ok: bool
    detail: str
    fixable: bool = False


UPDATE_FAILING = F("AurumSignalDesk-Update", False,
                   "last run exited 1 — pulls and deploys fixes is firing and "
                   "FAILING, which no restart fixes.")


def _plan(findings, **kw):
    return plan(findings, restart_desk=lambda: True, **kw)


def test_a_failing_update_task_now_has_a_remedy():
    """It escalated before, which was honest and left the desk stuck: nobody
    was going to be woken by it, and the fix needed a human at a terminal."""
    remedies, escalations = _plan([UPDATE_FAILING], run_update=lambda: True)
    assert [r.fault for r in remedies] == ["AurumSignalDesk-Update"]
    assert not escalations


def test_it_escalates_when_no_updater_is_injected():
    """A finding that maps to no remedy must never be silently dropped."""
    remedies, escalations = _plan([UPDATE_FAILING])
    assert not remedies
    assert [f.check for f in escalations] == ["AurumSignalDesk-Update"]


def test_the_remedy_says_why_this_is_not_new_authority():
    """The reason has to survive into the log, or the next reader sees only
    'self_heal deploys code' and reasonably panics."""
    remedies, _ = _plan([UPDATE_FAILING], run_update=lambda: True)
    why = remedies[0].why
    assert "suite-before-swap" in why and "rollback on red" in why
    assert "trigger, not the risk" in why


def test_it_invokes_the_real_updater_and_nothing_else(monkeypatch):
    """A SECOND deployer would be a genuinely dangerous thing to add. There is
    exactly one, and this must call it rather than reimplement any part of it."""
    seen = {}

    class R:
        returncode = 0

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env") or {}
        return R()

    monkeypatch.setattr(self_heal.subprocess, "run", fake_run)
    assert self_heal._run_update() is True
    assert "Update-AurumDesk.ps1" in " ".join(seen["argv"])
    assert "-ExecutionPolicy" in seen["argv"] and "Bypass" in seen["argv"]
    # No -Force and no -SkipTests: the two flags that would remove the guards.
    assert "-Force" not in seen["argv"]
    assert "-SkipTests" not in seen["argv"]


def test_it_forces_utf8_for_the_child(monkeypatch):
    """cp1252 plus a codebase of em-dashes is how the suite goes red for no
    reason — which is the failure that started this whole loop."""
    seen = {}

    class R:
        returncode = 0

    monkeypatch.setattr(self_heal.subprocess, "run",
                        lambda a, **kw: (seen.update(env=kw.get("env") or {}), R())[1])
    self_heal._run_update()
    assert seen["env"].get("PYTHONUTF8") == "1"
    assert seen["env"].get("PYTHONIOENCODING") == "utf-8"


def test_a_nonzero_exit_is_reported_as_failure_not_swallowed(monkeypatch):
    class R:
        returncode = 1

    monkeypatch.setattr(self_heal.subprocess, "run", lambda a, **kw: R())
    assert self_heal._run_update() is False


def test_a_crash_is_reported_as_failure(monkeypatch):
    def boom(*a, **kw):
        raise OSError("powershell missing")

    monkeypatch.setattr(self_heal.subprocess, "run", boom)
    assert self_heal._run_update() is False


def test_a_missing_updater_script_is_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(self_heal, "BASE", tmp_path)
    assert self_heal._run_update() is False


def test_a_healthy_update_task_gets_no_remedy():
    ok = F("AurumSignalDesk-Update", True, "enabled, last result 0")
    remedies, escalations = _plan([ok], run_update=lambda: True)
    assert not remedies and not escalations


def test_other_failing_tasks_do_not_trigger_a_deploy():
    """The remedy is bound to ONE task name. A deploy fired by an unrelated
    failure is a deploy nobody asked for."""
    other = F("MT5-ShadowSync", False, "last run exited 1 — is firing and FAILING")
    remedies, _ = _plan([other], run_update=lambda: True)
    assert all(r.fault != "AurumSignalDesk-Update" for r in remedies)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
