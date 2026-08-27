r"""Fix what is mechanical. Escalate what is not. Never blur the two.

The operator asked why, if the desk can DETECT its own faults, a human has to
fix them. For a large class the answer is "no reason" and remediate.py fixes
those unattended. For another class the fix is WRITING CODE, and a process that
writes and deploys its own code into a live trading desk can introduce a losing
bug, widen a risk limit, or reach the ruin rail.

The value is not that everything is automatic. It is that the line is EXPLICIT
and allowlisted rather than decided in the moment — and these tests are that
line, written down so a later edit has to delete an assertion rather than merely
add a capability.

    python3 -m pytest test_self_heal.py -q
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.remediate import (COOLDOWN, MAX_ATTEMPTS, Remediator, plan,
                                render)
from golddesk.self_audit import Finding

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

REMEDIATE = Path(__file__).parent / "golddesk" / "remediate.py"


def _broken(check, detail="broken"):
    return Finding(check, False, detail)


class _Spy:
    def __init__(self, ok=True):
        self.calls, self.ok = 0, ok

    def __call__(self):
        self.calls += 1
        return self.ok


# ------------------------------------------------- the mechanical class

def test_missing_cohorts_is_fixed_by_a_restart():
    """Cohorts are rebuilt at boot from the ledger, so a desk holding none while
    resolved trades exist booted before they resolved. A restart IS the remedy."""
    spy = _Spy()
    rem, esc = plan([_broken("cohorts")], restart_desk=spy)
    Remediator().run(rem, now=NOW)
    assert spy.calls == 1
    assert not esc


def test_a_stalled_ledger_is_fixed_by_a_restart():
    spy = _Spy()
    rem, _ = plan([_broken("ledger growth")], restart_desk=spy)
    Remediator().run(rem, now=NOW)
    assert spy.calls == 1


def test_a_passing_finding_triggers_nothing():
    rem, esc = plan([Finding("cohorts", True, "fine")], restart_desk=_Spy())
    assert not rem and not esc


# ------------------------------------------------ the escalation class

def test_a_design_fault_is_escalated_and_never_fixed():
    """'tp1 is computed and compared to nothing' needs new code. No allowlisted
    action can touch it, and pretending otherwise is the dangerous version."""
    spy = _Spy()
    rem, esc = plan([_broken("tp1 banking")], restart_desk=spy)
    Remediator().run(rem, now=NOW)
    assert spy.calls == 0
    assert [f.check for f in esc] == ["tp1 banking"]


def test_an_unknown_fault_escalates_rather_than_being_dropped():
    """A fault nobody is told about is worse than one nobody can fix."""
    _, esc = plan([_broken("something-new")], restart_desk=_Spy())
    assert [f.check for f in esc] == ["something-new"]


def test_the_escalation_text_says_why_it_was_not_fixed():
    """Otherwise the operator asks the same question every time one fires."""
    _, esc = plan([_broken("excursion")], restart_desk=_Spy())
    text = render([], esc)
    assert "NEEDS A HUMAN" in text
    assert "new code" in text
    assert "widen a risk limit" in text


# ------------------------------------------- it cannot become a loop

def test_the_same_fault_is_not_re_fixed_inside_the_cooldown():
    spy = _Spy()
    r = Remediator()
    rem, _ = plan([_broken("cohorts")], restart_desk=spy)
    r.run(rem, now=NOW)
    r.run(rem, now=NOW + timedelta(minutes=5))
    assert spy.calls == 1


def test_it_retries_once_the_cooldown_has_passed():
    spy = _Spy()
    r = Remediator()
    rem, _ = plan([_broken("cohorts")], restart_desk=spy)
    r.run(rem, now=NOW)
    r.run(rem, now=NOW + COOLDOWN + timedelta(minutes=1))
    assert spy.calls == 2


def test_a_fault_that_survives_its_own_remedy_stops_being_mechanical():
    """THE LOAD-BEARING LIMIT. A remedy that has not worked three times is not
    the right remedy, and applying it forever is how a self-healer becomes a
    crash loop."""
    spy = _Spy()
    r = Remediator()
    rem, _ = plan([_broken("cohorts")], restart_desk=spy)
    t = NOW
    for _ in range(MAX_ATTEMPTS + 3):
        r.run(rem, now=t)
        t += COOLDOWN + timedelta(minutes=1)
    assert spy.calls == MAX_ATTEMPTS


def test_the_attempt_cap_says_what_it_means():
    spy = _Spy()
    r = Remediator()
    rem, _ = plan([_broken("cohorts")], restart_desk=spy)
    t = NOW
    for _ in range(MAX_ATTEMPTS):
        r.run(rem, now=t)
        t += COOLDOWN + timedelta(minutes=1)
    out = r.run(rem, now=t)
    assert "no longer a mechanical fault" in out[0].detail


def test_a_remedy_that_raises_does_not_take_the_caller_down():
    def boom():
        raise RuntimeError("schtasks exploded")
    rem, _ = plan([_broken("cohorts")], restart_desk=boom)
    out = Remediator().run(rem, now=NOW)
    assert not out[0].taken
    assert "RuntimeError" in out[0].detail


def test_a_remedy_that_declines_is_recorded_as_not_taken():
    rem, _ = plan([_broken("cohorts")], restart_desk=_Spy(ok=False))
    out = Remediator().run(rem, now=NOW)
    assert not out[0].taken


# --------------------------------------- what it must never be able to do

def test_the_module_cannot_write_code_or_touch_risk():
    """Enumerated so a later edit has to delete a line rather than merely add a
    capability. Walks the AST: the module docstring names these things while
    promising not to do them, so a substring grep fails on its own explanation."""
    tree = ast.parse(REMEDIATE.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update(a.name for a in n.names)
            names.add(getattr(n, "module", "") or "")
    forbidden = ("exec", "eval", "compile", "write_text", "unlink", "rmtree",
                 "Thresholds", "current_stop", "risk_r", "max_open_risk_r",
                 "arm", "deadman", "subprocess", "os")
    for f in forbidden:
        assert f not in names, (
            f"remediate.py references {f!r} — it may fix OPERATIONAL state and "
            f"nothing else")


def test_every_action_is_injected_not_summoned():
    """Actions arrive as callables from the caller, so the allowlist is the
    call site and every one of them is visible to a test."""
    src = REMEDIATE.read_text(encoding="utf-8")
    assert "restart_desk: Callable" in src
    assert "subprocess" not in src


def test_the_runner_only_controls_the_desk_task():
    """self_heal.py is the one file allowed to touch a process, and it may only
    bounce the scheduled task — not kill by PID, not spawn python directly."""
    src = (Path(__file__).parent / "self_heal.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run"]
    args = [a.value for c in calls for a in ast.walk(c) if isinstance(a, ast.Constant)]
    assert "schtasks" in args
    for bad in ("taskkill", "Stop-Process", "shutdown", "pip", "git"):
        assert bad not in src, f"self_heal.py can invoke {bad!r}"


def test_the_dry_run_changes_nothing():
    src = (Path(__file__).parent / "self_heal.py").read_text(encoding="utf-8")
    i = src.index("if args.dry_run:")
    block = src[i:src.index("return 0", i)]
    assert "Remediator(" not in block
    assert "WOULD FIX" in block


def test_attempt_history_outlives_the_process():
    """The task starts fresh every 15 minutes. Without persistence the cooldown
    is a fiction and it would loop forever."""
    src = (Path(__file__).parent / "self_heal.py").read_text(encoding="utf-8")
    assert "_load_attempts" in src and "_save_attempts" in src


def test_the_remedy_calls_a_real_flows_api():
    """An earlier draft called flows.refresh(), which does not exist. A remedy
    that raises on first real use is worse than none, because the allowlist
    claims it works."""
    from golddesk import flows
    import self_heal, inspect
    src = inspect.getsource(self_heal._refresh_flows)
    assert "flows.save(flows.collect" in src
    assert hasattr(flows, "collect") and hasattr(flows, "save")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_the_healer_is_actually_scheduled():
    """An auto-fixer nothing runs is the exact defect class it exists to catch."""
    src = (Path(__file__).parent / "deploy" / "windows" /
           "Install-AurumStartup.ps1").read_text(encoding="utf-8")
    assert "self_heal.py" in src and "-SelfHeal" in src
    block = src[src.index("foreach ($t in @($TaskName"):][:500]
    assert "-SelfHeal" in block, "the uninstaller would leave it firing"


# ------------------------------- the expanded allowlist, split correctly

@pytest.mark.parametrize("check,expect_fix", [
    ("cohorts", True),
    ("ledger growth", True),
    ("checkpoint", True),
    ("spread profile", True),
    ("macro", True),
    ("disk", True),
    # These need NEW CODE or a human decision. No allowlisted action touches
    # them, and pretending otherwise is the dangerous version.
    ("tp1 banking", False),
    ("excursion", False),
    ("ledger integrity", False),
    ("notifications", False),
])
def test_each_fault_routes_to_the_right_side_of_the_line(check, expect_fix):
    rem, esc = plan([_broken(check)], restart_desk=_Spy(),
                    sample_spread=_Spy(), refresh_macro=_Spy(),
                    rotate_logs=_Spy(), refresh_flows=_Spy())
    assert bool(rem) is expect_fix, check
    assert bool(esc) is (not expect_fix), check


def test_a_torn_ledger_is_never_auto_repaired():
    """THE ONE THAT MATTERS MOST. The ledger is the only record of what this
    desk predicted and what happened. A remedy that edits it unattended can
    destroy the evidence the whole desk exists to produce."""
    rem, esc = plan([_broken("ledger integrity")], restart_desk=_Spy(),
                    sample_spread=_Spy(), refresh_macro=_Spy(), rotate_logs=_Spy())
    assert not rem
    assert [f.check for f in esc] == ["ledger integrity"]


def test_a_spread_sampler_that_declines_is_not_a_failure():
    """It refuses unless the attached terminal is the execution venue, and that
    refusal is the sampler working correctly."""
    rem, _ = plan([_broken("spread profile")], restart_desk=_Spy(),
                  sample_spread=_Spy(ok=False))
    out = Remediator().run(rem, now=NOW)
    assert not out[0].taken
    assert "declined by the action" in out[0].detail


def test_the_disk_remedy_can_only_reach_rotated_logs():
    """A disk remedy that can reach evidence eventually destroys it, which is
    worse than the full disk it was fixing."""
    import self_heal
    for pat in self_heal.ROTATABLE:
        assert pat.endswith((".1", ".2", ".old", ".tmp.*")), pat
    src = (Path(__file__).parent / "self_heal.py").read_text(encoding="utf-8")
    i = src.index("def _rotate_logs")
    block = src[i:i + 700]
    assert "ledger" not in block
    assert "service_state" not in block
    assert 'BASE / "logs"' in block


def test_no_remedy_is_offered_without_its_action():
    """plan() must not promise a fix it was given no way to perform."""
    rem, esc = plan([_broken("spread profile"), _broken("macro"), _broken("disk")],
                    restart_desk=_Spy())
    assert not rem
    assert len(esc) == 3
