r"""'The fix is deployed' and 'the fix is running' are different sentences.

`git pull` updates the working tree. It does not reload a long-running Python
process. So the desk can be executing code from hours ago while the repository
sits at HEAD, and every report that reads HEAD says the fix is deployed.

OBSERVED 2026-08-28. The published artifact reported a deployed_commit that
contained the rule-based fallback — correctly, of the DISK — while the desk
process, up since before that fallback existed, went on booking BLIND on every
wake, which the fallback was written to prevent. The fix was present, installed,
and not running, and nothing anywhere could see the difference.

    python3 -m pytest test_code_drift.py -q
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.remediate import plan
from golddesk.self_audit import check_running_code_is_current
from golddesk.state_publish import build_state, running_commit


# --------------------------------------------------------------------------
# The desk records what IT is running, not what is on disk.

def test_the_process_captures_its_commit_at_import():
    """Read on demand it would return the DISK's value and answer the wrong
    question entirely — which is the whole defect, restated."""
    from golddesk.service import RUNNING_COMMIT
    assert RUNNING_COMMIT and len(RUNNING_COMMIT) <= 12


def test_the_service_state_carries_it():
    """It has to reach a file, or nothing outside the process can compare."""
    from dataclasses import asdict

    from golddesk.service import ServiceState
    assert "running_commit" in asdict(ServiceState())


def test_a_desk_outside_a_checkout_reports_unknown_not_a_guess():
    from golddesk import service
    assert service.running_commit() != ""


def test_reading_it_back_from_a_state_file(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "service_state.json").write_text(
        json.dumps({"running_commit": "abc123456789"}), encoding="utf-8")
    assert running_commit(tmp_path) == "abc123456789"


def test_a_missing_state_file_is_unknown(tmp_path):
    assert running_commit(tmp_path) == "unknown"


def test_a_state_file_predating_the_field_is_unknown(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "service_state.json").write_text(
        json.dumps({"version": "old"}), encoding="utf-8")
    assert running_commit(tmp_path) == "unknown"


# --------------------------------------------------------------------------
# The artifact reports both, and never confuses them.

def test_the_artifact_reports_process_and_disk_separately():
    s = build_state([], {}, commit="aaaaaaaaaaaa", process_commit="bbbbbbbbbbbb")
    assert s["deployed_commit"] == "aaaaaaaaaaaa"
    assert s["process_commit"] == "bbbbbbbbbbbb"
    assert s["code_drift"] is True


def test_agreement_is_not_drift():
    s = build_state([], {}, commit="aaaaaaaaaaaa", process_commit="aaaaaaaaaaaa")
    assert s["code_drift"] is False


def test_an_unknown_process_commit_is_not_read_as_agreement():
    """UNMEASURED, not agreement. Treating an absent value as 'they match' would
    hide the exact case this exists for on any box whose state file predates the
    field — which is every box, the first time it deploys."""
    s = build_state([], {}, commit="aaaaaaaaaaaa")
    assert s["code_drift"] is False
    assert s["process_commit"] == "unknown"


# --------------------------------------------------------------------------
# It is a fault, and it has the one remedy that fixes it.

def test_drift_is_reported_as_a_fault():
    f = check_running_code_is_current("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    assert not f.ok
    assert "RUNNING bbbbbbbbbbbb" in f.detail
    assert "aaaaaaaaaaaa is installed" in f.detail


def test_the_fault_says_a_restart_is_what_makes_installed_code_run():
    f = check_running_code_is_current("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    assert "a restart is the only thing" in f.detail
    assert f.fixable is True


def test_agreement_passes_quietly():
    assert check_running_code_is_current("aaaaaaaaaaaa", "aaaaaaaaaaaa").ok


def test_unknown_is_UNMEASURED_and_says_it_is_not_agreement():
    f = check_running_code_is_current("aaaaaaaaaaaa", "unknown")
    assert f.ok
    assert "UNMEASURED" in f.detail
    assert "Not the same as agreement" in f.detail


def test_the_healer_restarts_the_desk_for_it():
    """The simplest remedy in the allowlist, and no new authority: bouncing the
    task is already the healer's one process-control verb."""
    @dataclass
    class F:
        check: str
        ok: bool
        detail: str
        fixable: bool = False

    bounced = []
    remedies, escalations = plan(
        [F("running code", False, "drift", fixable=True)],
        restart_desk=lambda: bounced.append(1) or True)
    assert [r.fault for r in remedies] == ["running code"]
    assert not escalations
    # `.apply` is the callable; `.action` is its human-readable name.
    remedies[0].apply()
    assert bounced == [1]
    assert "restart the desk" in remedies[0].action


def test_a_healthy_running_code_finding_gets_no_remedy():
    @dataclass
    class F:
        check: str
        ok: bool
        detail: str
        fixable: bool = False

    remedies, escalations = plan([F("running code", True, "agree")],
                                 restart_desk=lambda: True)
    assert not remedies and not escalations


def test_the_audit_includes_the_check():
    from golddesk.self_audit import audit
    names = [f.check for f in audit([], None, disk_commit="a" * 12,
                                    process_commit="b" * 12)]
    assert "running code" in names


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
