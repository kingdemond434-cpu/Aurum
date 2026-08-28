r"""The desk's state has to travel, or somebody has to be asked for it.

WHAT THIS IS FOR. Every audit in this repo runs on the box and prints to a log on
the box. On 2026-08-28 the CLI's login expired at ~21:00 and the desk booked
BLIND on every bar until ~01:30, and the reason was sitting in a file the whole
time -- on a machine nobody was logged into. Four hours of blind bars, ended by a
person typing a command.

So self_heal now publishes a bounded summary to git every fifteen minutes, the
same way the MT5 desk's sync_shadow_to_git.ps1 does.

IT PUBLISHES TO ITS OWN REF, and the first version did not -- it committed onto
the desk's CODE branch, which would have broken the auto-updater outright.
Update-AurumDesk.ps1 advances with `git merge --ff-only` and skips whenever
`git status --porcelain` is non-empty, so a state commit that lost a push race
would leave the box permanently diverged, and a tracked file rewritten every
fifteen minutes would stop the update path on its own.

These tests hold it to the two properties that make it safe to run unattended:
it says something TRUE, and it cannot touch the code branch, the index, HEAD or
the working tree -- verified against a REAL repository, not only a fake git.

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

from golddesk.state_publish import (ARTIFACT, FILENAME, STATE_BRANCH,
                                    build_state, publish)

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


#: What an unscripted plumbing command returns. Real-looking object ids matter:
#: publish() refuses to continue on an empty sha, so a fake that answered "" to
#: hash-object would silently short-circuit every test after it -- which is
#: exactly what the first version of this harness did.
_PLUMBING_OUT = {
    "hash-object": "a" * 40,
    "mktree": "b" * 40,
    "commit-tree": "c" * 40,
}


def _fake_git(script):
    """A git whose per-subcommand results are scripted. Records what it was asked."""
    calls = []

    def git(*args, **kw):
        calls.append(list(args))
        for prefix, result in script:
            if list(args)[:len(prefix)] == list(prefix):
                return result
        # `-c user.name=... commit-tree` puts the verb past the -c pairs.
        verb = next((a for a in args if a in _PLUMBING_OUT), None)
        return R(0, _PLUMBING_OUT.get(verb, ""))

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
# It cannot touch the code branch. Verified against a real git repository,
# because this is the property a scripted fake is least able to prove.

def _repo(tmp_path):
    """A throwaway repository, or a SKIP.

    WHY THIS SKIPS RATHER THAN FAILS. Update-AurumDesk.ps1 runs this suite
    against new code while the old desk is still live and ROLLS BACK ON RED. So
    a test that fails for an environmental reason -- git missing from the task's
    PATH, a git too old for `init -b`, a sandbox that forbids subprocesses --
    would not report a broken publisher: it would silently stop the box from
    ever deploying anything again. The property under test is worth proving
    where git exists and worth nothing where it does not.
    """
    import subprocess
    def g(*a, **kw):
        return subprocess.run(["git", "-C", str(tmp_path), *a],
                              capture_output=True, text=True, **kw)
    try:
        init = g("init", "-q", "-b", "work")
    except (OSError, subprocess.SubprocessError) as e:   # no git at all
        pytest.skip(f"git unavailable: {e}")
    if init.returncode != 0:                             # e.g. git < 2.28
        pytest.skip(f"git init -b unsupported: {(init.stderr or '').strip()[:120]}")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (tmp_path / "code.py").write_text("x = 1\n")
    g("add", "code.py")
    if g("commit", "-qm", "base").returncode != 0:
        pytest.skip("could not create a base commit in a scratch repo")
    return g


def test_it_leaves_the_working_tree_clean(tmp_path):
    """THE PROPERTY THE AUTO-UPDATER DEPENDS ON. Update-AurumDesk.ps1 skips
    entirely when `git status --porcelain` is non-empty, so a publisher that
    dirties the tree every fifteen minutes silently stops the box from ever
    updating itself again."""
    g = _repo(tmp_path)
    (tmp_path / ".gitignore").write_text(f"{ARTIFACT.as_posix()}\n")
    g("add", ".gitignore"); g("commit", "-qm", "ignore")
    before = g("status", "--porcelain").stdout

    changed, msg = publish(tmp_path, build_state([_blind_row()], {}, NOW),
                           push=False)
    assert changed is True, msg
    assert g("status", "--porcelain").stdout == before, "the tree got dirty"


def test_it_does_not_move_head_or_the_code_branch(tmp_path):
    g = _repo(tmp_path)
    head_before = g("rev-parse", "HEAD").stdout.strip()
    branch_before = g("rev-parse", "work").stdout.strip()

    publish(tmp_path, build_state([], {}, NOW), push=False)

    assert g("rev-parse", "HEAD").stdout.strip() == head_before
    assert g("rev-parse", "work").stdout.strip() == branch_before
    assert g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "work"


def test_it_does_not_stage_anything(tmp_path):
    """An empty diff --cached is what proves the index was never involved --
    which is what makes R0423's failure unreachable rather than merely avoided."""
    g = _repo(tmp_path)
    publish(tmp_path, build_state([], {}, NOW), push=False)
    assert g("diff", "--cached", "--name-only").stdout.strip() == ""


def test_the_state_branch_really_carries_the_artifact(tmp_path):
    """Built by plumbing, so it is worth proving the commit is real, readable,
    and contains what a reader will actually ask for."""
    g = _repo(tmp_path)
    publish(tmp_path, build_state([_blind_row()], {"analyst": [
        F("analyst login", False, "THE LOGIN HAS EXPIRED")]}, NOW), push=False)
    shown = g("show", f"{STATE_BRANCH}:{FILENAME}").stdout
    doc = json.loads(shown)
    assert doc["decisions"]["BLIND"]["why"]["needs_login"] is True
    assert doc["audits"]["analyst"]["faults"] == 1
    assert g("ls-tree", "--name-only", STATE_BRANCH).stdout.split() == [FILENAME]


def test_the_state_branch_accumulates_history(tmp_path):
    """One commit per change, chained -- so 'when did it start' is answerable
    from the branch alone rather than only from the newest snapshot."""
    g = _repo(tmp_path)
    publish(tmp_path, build_state([], {}, NOW), push=False)
    later = build_state([_blind_row()], {}, NOW)
    later["generated_utc"] = "2026-08-28T02:15:00+00:00"
    publish(tmp_path, later, push=False)
    log = g("log", "--oneline", STATE_BRANCH).stdout.strip().splitlines()
    assert len(log) == 2, log


def test_it_works_in_a_repo_with_no_git_identity_configured(tmp_path):
    """A deployment box may have none. commit-tree needs one, and writing it
    into the repo config would be a side effect nobody asked for."""
    import subprocess
    try:
        init = subprocess.run(["git", "-C", str(tmp_path), "init", "-q", "-b", "work"],
                              capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"git unavailable: {e}")
    if init.returncode != 0:
        pytest.skip("git init -b unsupported")
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-qm", "base", "--allow-empty"],
                   capture_output=True)
    changed, msg = publish(tmp_path, build_state([], {}, NOW), push=False)
    assert changed is True, msg


def test_an_unchanged_state_does_no_git_work_at_all(tmp_path):
    """Fifteen minutes at a time, almost every cycle finds nothing."""
    state = build_state([], {}, NOW)
    git, calls = _fake_git([])
    publish(tmp_path, state, runner=git, push=False)
    calls.clear()
    changed, msg = publish(tmp_path, state, runner=git, push=False)
    assert changed is False and msg == "unchanged"
    assert calls == [], "it touched git for a state that had not moved"


def test_it_never_stashes_checks_out_pulls_resets_or_forces(tmp_path):
    """Each of these is either a standing prohibition or a way to lose work.
    None of them is reachable: the whole publish path is object-database
    plumbing plus one push."""
    git, calls = _fake_git([])
    publish(tmp_path, build_state([_blind_row()], {}, NOW), runner=git)
    verbs = {c[0] for c in calls} | {c[2] for c in calls if len(c) > 2 and c[0] == "-c"}
    for forbidden in ("stash", "checkout", "pull", "reset", "add", "commit",
                      "merge", "rebase"):
        assert forbidden not in verbs, f"{forbidden} in {verbs}"
    flat = " ".join(" ".join(c) for c in calls)
    assert "--force" not in flat and "-f" not in flat.split()


def test_a_rejected_push_waits_for_the_next_cycle_instead_of_forcing(tmp_path):
    """The ref is rebuilt from scratch every cycle, so losing a push race costs
    fifteen minutes. Forcing would trade that for the chance of discarding
    somebody's work."""
    git, calls = _fake_git([(["push"], R(1, "", "rejected: fetch first"))])
    changed, msg = publish(tmp_path, build_state([], {}, NOW), runner=git)
    assert changed is True
    assert "will retry next cycle" in msg
    assert sum(1 for c in calls if c[0] == "push") == 1, "it retried in-cycle"


def test_it_pushes_the_state_ref_explicitly_never_the_current_branch(tmp_path):
    """A bare `git push` would publish whatever branch the box happens to be on.
    The refspec is spelled out so this can only ever move one ref."""
    git, calls = _fake_git([])
    publish(tmp_path, build_state([], {}, NOW), runner=git)
    push = next(c for c in calls if c[0] == "push")
    assert push == ["push", "origin",
                    f"refs/heads/{STATE_BRANCH}:refs/heads/{STATE_BRANCH}"]


def test_publishing_failure_is_reported_not_raised(tmp_path):
    """Visibility failing must never take down the thing doing the watching."""
    git, _ = _fake_git([(["hash-object"], R(128, "", "not a git repository"))])
    changed, msg = publish(tmp_path, build_state([], {}, NOW), runner=git)
    assert changed is False
    assert "hash-object failed" in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
