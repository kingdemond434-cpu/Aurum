r"""When the channel that reports on the desk is itself down, say so.

THE CIRCULARITY THIS CLOSES, and it was shipped. state_publish exists so the
desk's condition reaches somebody WITHOUT them logging into the box. But it
delivers by `git push`, so when the push fails -- no credentials on the clone, a
rejected ref, no network -- the failure is written to a log ON THE BOX. The
channel built to end "go and look" was, in precisely the case that matters, only
visible by going and looking.

Observed 2026-08-28: the operator asked whether the desk was working, the
artifact had never appeared on the remote, and nothing anywhere could say which
link had broken.

    python3 -m pytest test_publish_health.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import self_heal


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """Redirect the health file into tmp and capture Telegram sends."""
    monkeypatch.setattr(self_heal, "PUBLISH_STATE",
                        tmp_path / "publish_health.json")
    out = []

    class Sink:
        def send(self, m):
            out.append(m)

    monkeypatch.setattr("golddesk.notify.build_sink", lambda _=None: Sink())
    return out


def _fail(n, sent):
    for _ in range(n):
        self_heal._report_publish_health(
            "aurum-state updated locally; push failed, will retry next cycle: "
            "fatal: could not read Username for 'https://github.com'", False)


def test_one_failed_push_stays_quiet(sent):
    """It races the code branch, and the ref is rebuilt from scratch every
    cycle. Alerting on a lost race trains the operator to ignore the channel."""
    _fail(1, sent)
    assert sent == []


def test_a_second_consecutive_failure_escalates(sent):
    """Twice in a row is not a race, it is a broken channel."""
    _fail(2, sent)
    assert len(sent) == 1
    assert "NOT PUBLISHING" in sent[0]


def test_the_alarm_names_the_likeliest_cause_and_what_it_does_not_mean(sent):
    """A failing publisher does NOT mean a failing desk, and conflating them
    would send somebody to fix the wrong thing at 3am."""
    _fail(2, sent)
    assert "may be fine" in sent[0]
    assert "push credentials" in sent[0]
    assert "could not read Username" in sent[0], "the git error was dropped"


def test_it_alarms_once_per_outage_not_once_per_cycle(sent):
    """At 15 minutes, per-cycle would be 96 messages a day. A channel that
    cries every quarter hour is one nobody reads."""
    _fail(8, sent)
    assert len(sent) == 1


def test_recovery_is_announced(sent):
    """The last thing the operator heard must never be that something was
    broken. Without this the channel goes quiet on success, which is
    indistinguishable from staying broken."""
    _fail(2, sent)
    self_heal._report_publish_health("pushed", False)
    assert len(sent) == 2
    assert "PUBLISHING AGAIN" in sent[1]


def test_a_healthy_run_says_nothing(sent):
    for how in ("pushed", "unchanged"):
        self_heal._report_publish_health(how, False)
    assert sent == []


def test_unchanged_counts_as_reaching_the_remote(sent):
    """The common case, fifteen minutes at a time: nothing moved, so no push was
    needed. Treating that as a failure would alarm on a perfectly healthy desk
    within half an hour."""
    self_heal._report_publish_health("unchanged", False)
    assert json.loads(self_heal.PUBLISH_STATE.read_text())["consecutive_failures"] == 0
    assert sent == []


def test_a_crash_counts_as_a_failure(sent):
    self_heal._report_publish_health("crashed: boom", False)
    self_heal._report_publish_health("crashed: boom", False)
    assert len(sent) == 1


def test_dry_run_never_notifies(sent):
    _fail(0, sent)
    for _ in range(5):
        self_heal._report_publish_health("push failed", True)
    assert sent == []
    assert not self_heal.PUBLISH_STATE.exists(), "dry-run wrote state"


def test_a_notify_failure_does_not_raise(tmp_path, monkeypatch):
    """Visibility failing must not take down the thing doing the watching --
    including the part of it that reports on visibility."""
    monkeypatch.setattr(self_heal, "PUBLISH_STATE", tmp_path / "h.json")

    def boom(_=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("golddesk.notify.build_sink", boom)
    self_heal._report_publish_health("push failed", False)
    self_heal._report_publish_health("push failed", False)   # must not raise
    assert json.loads((tmp_path / "h.json").read_text())["consecutive_failures"] == 2


def test_an_unreadable_health_file_is_not_read_as_healthy(tmp_path, monkeypatch):
    """UNMEASURED is a real answer. A corrupt file resetting the counter to zero
    would make an outage invisible for as long as it kept corrupting."""
    p = tmp_path / "h.json"
    p.write_text("{not json")
    monkeypatch.setattr(self_heal, "PUBLISH_STATE", p)
    self_heal._report_publish_health("push failed", False)
    assert json.loads(p.read_text())["consecutive_failures"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
