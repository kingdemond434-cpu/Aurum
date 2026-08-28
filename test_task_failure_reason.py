r"""'exited 1' names the task. It does not name the cause.

WHAT THIS COST, 2026-08-28. AurumSignalDesk-Update had been failing long enough
that the box sat on a commit from before a full day of fixes — expired-login
detection, the CLI flag ladder, state publishing, the desk's own CLAUDE.md — all
pushed to the remote and none of them deployed. Every report said only:

    [BROKEN] AurumSignalDesk-Update  last run exited 1 — pulls and deploys fixes
                                     is firing and FAILING

True, and useless. Meanwhile Update-AurumDesk.ps1 has exactly three ways to exit
1 — git not on the task's PATH, a non-fast-forward, a red suite — and they are
trivially told apart by reading the log it writes on that same box.

So the desk was blind on 59 of 59 wakes, the change that would have said why had
never been installed, and the reason it was never installed was that the thing
which installs it was the broken thing. The report had every fact needed to
break that loop and printed none of them.

    python3 -m pytest test_task_failure_reason.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import self_heal

TASK = "AurumSignalDesk-Update"


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    p = tmp_path / "update.log"
    monkeypatch.setitem(self_heal.TASK_LOGS, TASK, p)
    return p


def test_it_quotes_the_line_that_names_the_cause(logfile):
    logfile.write_text(
        "2026-08-28 07:00:02  new commits on branch: 096f81d -> 1a2e56f\n"
        "2026-08-28 07:00:03  running the suite against the new code...\n"
        "2026-08-28 07:04:41  TESTS FAILED — rolling back to 096f81d. Desk untouched.\n")
    out = self_heal._why_the_task_failed(TASK)
    assert "TESTS FAILED" in out
    assert "rolling back to 096f81d" in out


def test_it_distinguishes_the_three_ways_the_updater_can_die(logfile):
    """Each needs a different fix. Collapsing them into 'exited 1' is what made
    this take a day."""
    for line, token in (
            ("ABORT: git is not on this task's PATH", "not on this task's PATH"),
            ("ABORT: not a fast-forward. The box has diverged", "not a fast-forward"),
            ("TESTS FAILED — rolling back to 096f81d", "TESTS FAILED")):
        logfile.write_text(f"routine line\n{line}\n")
        assert token in self_heal._why_the_task_failed(TASK)


def test_a_missing_log_is_itself_the_finding(logfile):
    """A task that exits 1 without writing a single line died BEFORE its script
    ran — the interpreter, the working directory or the PATH. That is a
    different fix from anything the script could report, and 'no log' is the
    only evidence that says so."""
    assert not logfile.exists()
    out = self_heal._why_the_task_failed(TASK)
    assert "failed BEFORE writing" in out
    assert "interpreter" in out


def test_a_log_with_no_failure_line_is_UNMEASURED_not_healthy(logfile):
    """L1.28a. Absence of a matched line means this could not find the cause —
    never that there wasn't one."""
    logfile.write_text("2026-08-28 07:00:02  up to date at 096f81d\n" * 5)
    out = self_heal._why_the_task_failed(TASK)
    assert "UNMEASURED" in out
    assert "not healthy" in out


def test_it_reads_the_TAIL_not_the_whole_log(logfile):
    """These logs run for months. A failure from March is not this failure, and
    an unbounded read would put it in a Telegram message."""
    logfile.write_text("ABORT: ancient failure nobody cares about\n"
                       + "routine line\n" * 500
                       + "TESTS FAILED — today's actual problem\n")
    out = self_heal._why_the_task_failed(TASK)
    assert "today's actual problem" in out
    assert "ancient failure" not in out


def test_the_quoted_output_is_bounded(logfile):
    """It travels into a report and possibly a notification."""
    logfile.write_text("".join(f"ERROR line {i} {'x' * 900}\n" for i in range(50)))
    out = self_heal._why_the_task_failed(TASK)
    assert len(out.splitlines()) <= 4
    assert all(len(ln) <= 310 for ln in out.splitlines())


def test_an_unmapped_task_says_nothing_rather_than_guessing(tmp_path):
    """Guessing a log path would produce a confident 'no log — it died before
    writing' for a task that simply has no log configured."""
    assert self_heal._why_the_task_failed("SomeTaskNobodyMapped") == ""


def test_an_unreadable_log_is_reported_not_swallowed(logfile, monkeypatch):
    logfile.write_text("TESTS FAILED\n")

    def boom(*a, **kw):
        raise PermissionError("in use by another process")

    monkeypatch.setattr(Path, "read_text", boom)
    out = self_heal._why_the_task_failed(TASK)
    assert "could not read" in out


def test_every_mapped_log_lives_under_the_desk_root():
    """Read-only is not enough — a path escaping the desk root would let a task
    name decide which file gets quoted into a report."""
    for task, path in self_heal.TASK_LOGS.items():
        assert self_heal.BASE in path.parents, task


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
