r"""The desk's state has to travel, or somebody has to be asked for it.

WHAT THIS IS FOR. Every audit in this repo runs on the box and prints to a log on
the box. On 2026-08-28 the CLI's login expired at ~21:00 and the desk booked
BLIND on every bar until ~01:30, and the reason was sitting in a file the whole
time -- on a machine nobody was logged into. Four hours of blind bars, ended by a
person typing a command.

So self_heal now publishes a bounded summary to git every fifteen minutes, the
same way the MT5 desk's sync_shadow_to_git.ps1 does. These tests hold it to the
two properties that make that safe to run unattended: it says something TRUE, and
it never commits over somebody else's work.

    python3 -m pytest test_state_publish.py -q
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.state_publish import ARTIFACT, build_state, publish

NOW = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)


@dataclass
class F:
    check: str
    ok: bool
    detail: str


def _blind_row(ts="2026-08-28T01:30:00+00:00"):
    return {"t0": ts, "kind": "BLIND", "decision": {
        "stage": "read", "error": "claude cannot authenticate: ...",
        "needs_login": True,
        "cli": {"subtype": "success", "stop_reason": "stop_sequence",
                "duration_api_ms": 0, "input_tokens": 0,
                "result": "Failed to authenticate: OAuth session expired and "
                          "could not be refreshed"}}}


@dataclass
class R:
    """A CompletedProcess stand-in. Module scope so a test can build results
    for the script it is about to hand to _fake_git."""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _fake_git(script):
    """A git whose per-subcommand results are scripted. Records what it was asked."""
    calls = []

    def git(*args):
        calls.append(list(args))
        for prefix, result in script:
            if list(args)[:len(prefix)] == list(prefix):
                return result
        return R()

    return git, calls


# --------------------------------------------------------------------------
# It says something true.

def test_the_artifact_carries_the_reason_not_just_the_status():
    """'BROKEN analyst login' is a status. 'THE LOGIN HAS EXPIRED -- run
    `claude` once interactively' is an instruction, and only one of them saves
    somebody the trip to the box."""
    s = build_state([_blind_row()], {"analyst": [
        F("analyst login", False, "THE LOGIN HAS EXPIRED — run `claude` once "
                                  "interactively on the box")]}, NOW)
    assert s["audits"]["analyst"]["faults"] == 1
    assert "LOGIN HAS EXPIRED" in s["audits"]["analyst"]["checks"][0]["detail"]
    assert s["decisions"]["BLIND"]["why"]["result"].startswith("Failed to authenticate")
    assert s["decisions"]["BLIND"]["why"]["needs_login"] is True


def test_it_is_dateable_without_trusting_that_it_is_fresh():
    """The MT5 artifact went 35 hours stale while every number in it kept being
    read as current. A reader must be able to date what they are looking at."""
    s = build_state([_blind_row()], {}, NOW)
    assert s["generated_utc"] == NOW.isoformat()
    assert s["last_row_utc"] == "2026-08-28T01:30:00+00:00"


def test_an_empty_ledger_is_reported_as_empty_not_as_healthy():
    """UNMEASURED is a real answer (L1.28a). Zero blind rows because nothing ran
    must not render as a desk that is reading fine."""
    s = build_state([], {}, NOW)
    assert s["ledger_rows"] == 0
    assert s["last_row_utc"] is None
    assert all(v["count"] == 0 and v["last"] is None
               for v in s["decisions"].values())


def test_faults_are_totalled_across_every_axis():
    s = build_state([], {"wiring": [F("a", True, ""), F("b", False, "x")],
                         "tasks": [F("c", False, "y")]}, NOW)
    assert s["total_faults"] == 2


def test_only_allowlisted_cli_fields_are_published():
    """A POSITIVE allowlist, because a denylist is one forgotten key away from
    publishing something it should not. Fields are copied in by name."""
    row = _blind_row()
    row["decision"]["cli"]["api_key"] = "sk-ant-SHOULD-NEVER-APPEAR"
    row["decision"]["cli"]["session_id"] = "109b4c99"
    s = build_state([row], {}, NOW)
    blob = json.dumps(s)
    assert "sk-ant-SHOULD-NEVER-APPEAR" not in blob
    assert "109b4c99" not in blob
    assert "OAuth session expired" in blob


# --------------------------------------------------------------------------
# It never commits over somebody else's work.

def test_a_dirty_tree_is_refused_not_committed_over(tmp_path):
    """R0423: three recorded instances of a broad commit sweeping a sibling
    session's staged files into an unrelated commit. An unattended committer is
    exactly the thing that must refuse."""
    git, calls = _fake_git([
        (["status"], R(0, " M golddesk/live.py\n M self_heal.py\n")),
    ])
    changed, msg = publish(tmp_path, build_state([], {}, NOW), runner=git)
    assert changed is False
    assert "NOT committing over them" in msg
    assert not any(c[0] == "commit" for c in calls)
    assert not (tmp_path / ARTIFACT).exists(), "it wrote the file anyway"


def test_its_own_artifact_does_not_count_as_a_dirty_tree(tmp_path):
    git, calls = _fake_git([
        (["status"], R(0, f" M {ARTIFACT.as_posix()}\n")),
    ])
    changed, msg = publish(tmp_path, build_state([], {}, NOW), runner=git)
    assert changed is True and msg == "pushed"


def test_it_stages_an_explicit_path_and_never_commit_dash_a(tmp_path):
    git, calls = _fake_git([(["status"], R(0, ""))])
    publish(tmp_path, build_state([], {}, NOW), runner=git)
    add = next(c for c in calls if c[0] == "add")
    assert add == ["add", "--", ARTIFACT.as_posix()]
    for c in calls:
        assert "-a" not in c and "--all" not in c, c


def test_it_never_stashes_and_never_forces(tmp_path):
    """Both are standing prohibitions: a stash restores to the index and a
    sibling can check the tree out from under you; a force push discards work
    nobody agreed to discard."""
    git, calls = _fake_git([
        (["status"], R(0, "")),
        (["push"], R(1, "", "rejected")),
        (["pull"], R(1, "", "conflict")),
    ])
    publish(tmp_path, build_state([], {}, NOW), runner=git)
    flat = " ".join(" ".join(c) for c in calls)
    assert "stash" not in flat
    assert "--force" not in flat and "-f" not in flat.split()


def test_a_rejected_push_rebases_once_and_then_waits(tmp_path):
    """It races the code branch. Losing that race is ordinary, and the next
    cycle is fifteen minutes away -- so this never loops."""
    git, calls = _fake_git([
        (["status"], R(0, "")),
        (["pull"], R(0, "")),
    ])
    pushes = {"n": 0}

    def counting(*args):
        if args[0] == "push":
            pushes["n"] += 1
            return R(1, "", "rejected") if pushes["n"] == 1 else R(0)
        return git(*args)

    changed, msg = publish(tmp_path, build_state([], {}, NOW), runner=counting)
    assert changed is True and msg == "pushed after rebase"
    assert pushes["n"] == 2, "it should try exactly twice, not loop"


def test_an_unchanged_state_does_no_git_work_at_all(tmp_path):
    """Fifteen minutes at a time, almost every cycle finds nothing. An empty
    commit each time would bury the ones that mean something."""
    state = build_state([], {}, NOW)
    git, calls = _fake_git([(["status"], R(0, ""))])
    publish(tmp_path, state, runner=git)
    calls.clear()
    changed, msg = publish(tmp_path, state, runner=git)
    assert changed is False and msg == "unchanged"
    assert calls == [], "it touched git for a state that had not moved"


def test_the_artifact_is_written_where_it_can_travel(tmp_path):
    git, _ = _fake_git([(["status"], R(0, ""))])
    publish(tmp_path, build_state([_blind_row()], {}, NOW), runner=git)
    on_disk = json.loads((tmp_path / ARTIFACT).read_text())
    assert on_disk["decisions"]["BLIND"]["count"] == 1


def test_publishing_failure_is_reported_not_raised(tmp_path):
    """Visibility failing must never take down the thing doing the watching."""
    git, _ = _fake_git([(["status"], R(128, "", "not a git repository"))])
    changed, msg = publish(tmp_path, build_state([], {}, NOW), runner=git)
    assert changed is False
    assert "git status failed" in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
