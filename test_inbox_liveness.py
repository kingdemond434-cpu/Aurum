r"""The inbox file's age is not the transport's age, and the check said it was.

OBSERVED 2026-08-28, on the live desk:

    [LOOK ] quant inbox   last updated 180h ago and the chain runs daily.
                          A link is broken, which is not the same as quant
                          having found nothing.

The second sentence is the right principle and the first sentence violates it.
Sync-QuantFindings.ps1 APPENDS, and when there are no new rows it does not touch
the inbox at all — so the mtime records the last time CONTENT arrived and says
nothing whatever about whether the transport ran this morning. "Ran daily and
found nothing new" and "has not run since Tuesday" are indistinguishable from
that file alone, and the check picked one and asserted it.

So the transport now writes a heartbeat on EVERY path, and liveness and content
age became separate facts with separate evidence.

    python3 -m pytest test_inbox_liveness.py -q
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.capture import INBOX_STALE_H, check_quant_inbox

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _base(tmp_path, inbox_age_h=None, hb=None, hb_age_h=0.5):
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)
    if inbox_age_h is not None:
        f = tmp_path / "inbox" / "quant_findings.jsonl"
        f.write_text('{"statement":"x","measured_on":"y"}\n', encoding="utf-8")
        t = NOW.timestamp() - inbox_age_h * 3600
        os.utime(f, (t, t))
    if hb is not None:
        h = tmp_path / "inbox" / "quant_sync_heartbeat.json"
        h.write_text(json.dumps(hb), encoding="utf-8")
        t = NOW.timestamp() - hb_age_h * 3600
        os.utime(h, (t, t))
    return tmp_path


def _now():
    """check_quant_inbox compares mtimes against now.timestamp()."""
    return NOW


def test_a_quiet_quant_desk_is_no_longer_called_a_broken_link(tmp_path):
    """THE LIVE FALSE ALARM. The transport ran half an hour ago and had nothing
    to carry; the file is a week old because nothing new has been written to it."""
    b = _base(tmp_path, inbox_age_h=180, hb={"status": "no-new", "new": 0})
    f = check_quant_inbox(b, _now())
    assert f.ok, f.detail
    assert "transport alive" in f.detail
    assert "quant desk being quiet" in f.detail


def test_a_genuinely_dead_transport_is_still_caught(tmp_path):
    """The check must not become permissive — that would trade a false alarm for
    a missed one."""
    b = _base(tmp_path, inbox_age_h=180, hb={"status": "no-new"}, hb_age_h=200)
    f = check_quant_inbox(b, _now())
    assert not f.ok
    assert "genuinely broken" in f.detail


def test_a_missing_source_points_upstream_at_quant(tmp_path):
    """Aurum's side working and quant's exporter not running is a THIRD state,
    and it needs a different person to fix it."""
    b = _base(tmp_path, inbox_age_h=180,
              hb={"status": "no-source", "source": "C:/opt/quant/.../x.jsonl"})
    f = check_quant_inbox(b, _now())
    assert not f.ok
    assert "break is UPSTREAM" in f.detail
    assert "Aurum's side of the chain is working" in f.detail


def test_no_heartbeat_at_all_is_UNMEASURED_not_a_verdict(tmp_path):
    """An older box simply predates the heartbeat. Reporting that as a broken
    link would be the same overreach in the opposite direction."""
    b = _base(tmp_path, inbox_age_h=180)
    f = check_quant_inbox(b, _now())
    assert not f.ok
    assert "UNMEASURED" in f.detail
    assert "look identical" in f.detail


def test_a_fresh_inbox_needs_no_heartbeat_reasoning(tmp_path):
    b = _base(tmp_path, inbox_age_h=2)
    f = check_quant_inbox(b, _now())
    assert f.ok
    assert "updated 2h ago" in f.detail


def test_an_inbox_that_never_arrived_is_unchanged(tmp_path):
    (tmp_path / "inbox").mkdir(parents=True)
    f = check_quant_inbox(tmp_path, _now())
    assert not f.ok
    assert "has EVER arrived" in f.detail


def test_a_corrupt_heartbeat_is_not_read_as_alive(tmp_path):
    b = _base(tmp_path, inbox_age_h=180)
    (b / "inbox" / "quant_sync_heartbeat.json").write_text("{broken", encoding="utf-8")
    f = check_quant_inbox(b, _now())
    assert not f.ok
    assert "UNMEASURED" in f.detail


def test_the_script_writes_the_heartbeat_on_every_path():
    """Three exits, three heartbeats. One missing path and the check silently
    goes back to guessing on that branch."""
    ps1 = (Path(__file__).parent / "deploy" / "windows"
           / "Sync-QuantFindings.ps1").read_text(encoding="utf-8")
    for status in ("no-source", "no-new", "delivered"):
        assert f'Write-Heartbeat "{status}"' in ps1, status


def test_the_statuses_the_script_writes_are_the_ones_the_check_reads():
    """Two files encoding one vocabulary is how a transport and its monitor
    start disagreeing about what a word means."""
    from golddesk import capture
    src = Path(capture.__file__).read_text(encoding="utf-8")
    assert '"no-source"' in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
