"""The preflight checked the world. The world was fine and the desk was broken.

On 2026-08-27 run_desk.py's preflight passed every check all day — MT5 up,
broker matched, Telegram delivering — while the desk was broken in five places:

  cohorts never reached the live desk, so every mechanism priced off the
  cold-start prior no matter how many trades resolved
  tp1 was computed, journalled and rendered, and compared to price by nothing
  the observer's tick count and path reset on every restart
  the learning cycle had no Windows launcher at all
  a bar the analyst never answered on left no ledger row

NONE was a world problem. Each was a JOIN between two components that both
worked and both passed their own tests — and a join is invisible to any check
that looks at one side of it. These tests are about the checks that look at
both sides.

    python3 -m pytest test_self_audit.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.self_audit import audit, render

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _closed(t0="a", mfe=1.5, mae=-0.4, obs=120, mgmt=None, ts=None):
    return {"kind": "TRADE_CLOSED", "entry_t0": t0, "mfe_r": mfe, "mae_r": mae,
            "observations": obs, "management": mgmt or [],
            "ts": (ts or NOW).isoformat(), "realised_r": 0.5}


def _signal(t0="a", rr1=1.2, ts=None):
    return {"kind": "SIGNAL", "t0": t0, "ts": (ts or NOW).isoformat(),
            "decision": {"rr_tp1": rr1}}


def _by(findings, name):
    return next(f for f in findings if f.check == name)


# ----------------------------------------------------- cohorts are loaded

def test_resolved_trades_with_no_cohorts_is_a_wiring_fault():
    f = _by(audit([_closed()], cohorts=None, now=NOW), "cohorts")
    assert not f.ok
    assert "cannot learn from its own results" in f.detail


def test_resolved_trades_with_cohorts_passes():
    f = _by(audit([_closed()], cohorts={"m": object()}, now=NOW), "cohorts")
    assert f.ok


def test_an_empty_ledger_does_not_demand_cohorts():
    """Before anything resolves, no cohorts is the correct state — flagging it
    would train the operator to ignore the audit from day one."""
    assert _by(audit([], cohorts=None, now=NOW), "cohorts").ok


# --------------------------------------------------------- tp1 is acted on

def test_trades_reaching_tp1_without_banking_is_a_wiring_fault():
    rows = []
    for k in range(3):
        rows += [_signal(str(k), rr1=1.0), _closed(str(k), mfe=1.8)]
    f = _by(audit(rows, cohorts={"m": 1}, now=NOW), "tp1 banking")
    assert not f.ok
    assert "acted on by nothing" in f.detail


def test_banking_at_tp1_passes():
    rows = []
    for k in range(3):
        rows += [_signal(str(k), rr1=1.0),
                 _closed(str(k), mfe=1.8, mgmt=[{"source": "tp1"}])]
    assert _by(audit(rows, cohorts={"m": 1}, now=NOW), "tp1 banking").ok


def test_a_trade_that_never_reached_tp1_is_not_counted_against_it():
    """MFE below TP1 means the objective was never touched — no bank was owed."""
    rows = []
    for k in range(4):
        rows += [_signal(str(k), rr1=2.0), _closed(str(k), mfe=0.5)]
    assert _by(audit(rows, cohorts={"m": 1}, now=NOW), "tp1 banking").ok


def test_one_touch_is_too_few_to_judge():
    """A single unbanked touch could legitimately be the runner-fraction
    invariant refusing. Two is a pattern."""
    rows = [_signal("a", rr1=1.0), _closed("a", mfe=1.8)]
    assert _by(audit(rows, cohorts={"m": 1}, now=NOW), "tp1 banking").ok


# ------------------------------------------------------ excursion survives

def test_zero_observations_and_zero_excursion_is_a_wiring_fault():
    """The exact exit message seen in production: MFE +0.00R, MAE +0.00R,
    0 observations, on a trade held for hours."""
    f = _by(audit([_closed(mfe=0.0, mae=0.0, obs=0)], cohorts={"m": 1}, now=NOW),
            "excursion")
    assert not f.ok
    assert "unanswerable" in f.detail


def test_a_real_excursion_record_passes():
    assert _by(audit([_closed()], cohorts={"m": 1}, now=NOW), "excursion").ok


# ---------------------------------------------------------- blind bars

def test_blind_bars_are_reported_without_being_called_a_fault():
    """Zero is the healthy case, and a blind bar is a real event rather than a
    broken one — it must be visible without crying wolf."""
    rows = [_closed(), {"kind": "BLIND", "ts": NOW.isoformat()}]
    f = _by(audit(rows, cohorts={"m": 1}, now=NOW), "blind bars")
    assert f.ok
    assert "NOT refusals" in f.detail


# -------------------------------------------------------- ledger growth

def test_a_stalled_ledger_is_a_fault():
    old = NOW - timedelta(hours=30)
    f = _by(audit([_closed(ts=old)], cohorts={"m": 1}, now=NOW), "ledger growth")
    assert not f.ok
    assert "not reaching decisions" in f.detail


def test_a_fresh_ledger_passes():
    assert _by(audit([_closed(ts=NOW - timedelta(hours=1))],
                     cohorts={"m": 1}, now=NOW), "ledger growth").ok


def test_an_empty_ledger_is_not_a_stall():
    assert _by(audit([], cohorts=None, now=NOW), "ledger growth").ok


# ------------------------------------------------------------- reporting

def test_the_report_names_the_number_of_faults():
    text = render(audit([_closed(mfe=0.0, mae=0.0, obs=0)], cohorts=None, now=NOW))
    assert "WIRING FAULT(S)" in text
    assert "[BROKEN]" in text


def test_a_clean_report_says_so_plainly():
    text = render(audit([_closed()], cohorts={"m": 1}, now=NOW))
    assert "all wiring checks pass" in text
    assert "[BROKEN]" not in text


def test_the_report_explains_why_these_faults_hide():
    """The operator asked why watchdogs could not just fix this. The report has
    to answer it, or it will be asked again every time one fires."""
    text = render(audit([_closed(mfe=0.0, mae=0.0, obs=0)], cohorts=None, now=NOW))
    assert "JOINS, not components" in text
    assert "announce itself" in text


def test_it_reports_and_cannot_block_a_boot():
    """A desk that refuses to start because an audit is unhappy is worse than
    one that starts and says so loudly."""
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "self_audit.py")
                     .read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "SystemExit" not in names


def test_the_audit_runs_at_boot():
    """An audit nothing calls is the defect it exists to catch."""
    src = (Path(__file__).parent / "golddesk" / "service.py").read_text(encoding="utf-8")
    assert "from .self_audit import audit" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ==================================================================
# System checks — each corresponds to a fault observed on the live box
# ==================================================================

def _base(tmp_path):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    return tmp_path


def _sys(tmp_path, rows=(), cohorts=None, now=NOW):
    return audit(list(rows), cohorts, now=now, base=_base(tmp_path))


# ------------------------------------------------------- spread profile

def test_a_missing_spread_profile_is_a_fault(tmp_path):
    """OBSERVED LIVE all day: every expectancy figure priced against Fusion's
    feed while the operator fills on Vantage."""
    f = _by(_sys(tmp_path), "spread profile")
    assert not f.ok
    assert "not the venue that actually fills you" in f.detail


def test_a_profile_with_no_measured_session_is_still_a_fault(tmp_path):
    """calibrate() refuses to characterise a session under 100 quotes, so a
    profile can exist and measure nothing — which prices nothing."""
    import json
    b = _base(tmp_path)
    (b / "config" / "spread_profile.json").write_text(
        json.dumps({"by_session": {}, "calibrated_from": "Vantage"}))
    f = _by(_sys(tmp_path), "spread profile")
    assert not f.ok
    assert "NO session was calibrated" in f.detail


def test_a_calibrated_profile_passes_and_names_the_venue(tmp_path):
    import json
    b = _base(tmp_path)
    (b / "config" / "spread_profile.json").write_text(
        json.dumps({"by_session": {"LONDON": 0.28}, "calibrated_from": "Vantage-Live"}))
    f = _by(_sys(tmp_path), "spread profile")
    assert f.ok and "Vantage-Live" in f.detail


# --------------------------------------------------------- notifications

def test_mostly_failing_sends_is_a_fault(tmp_path):
    """The message IS this desk's entire product. Silence from a broken channel
    and silence from a quiet market look identical to the operator."""
    import json
    b = _base(tmp_path)
    (b / "state" / "service_state.json").write_text(
        json.dumps({"notification_health": {"sent": 2, "failed": 30}}))
    f = _by(_sys(tmp_path), "notifications")
    assert not f.ok
    assert "deciding into a void" in f.detail


def test_a_sink_that_tracks_nothing_reads_UNKNOWN_not_healthy(tmp_path):
    import json
    b = _base(tmp_path)
    (b / "state" / "service_state.json").write_text(json.dumps({"notification_health": {}}))
    f = _by(_sys(tmp_path), "notifications")
    assert f.ok
    assert "UNKNOWN" in f.detail and "not the same as healthy" in f.detail


# ------------------------------------------------------------ checkpoint

def test_a_stale_checkpoint_is_a_fault(tmp_path):
    import json, os, time
    b = _base(tmp_path)
    p = b / "state" / "service_state.json"
    p.write_text(json.dumps({}))
    old = time.time() - 60 * 60 * 24
    os.utime(p, (old, old))
    f = _by(audit([], None, now=datetime.now(UTC), base=b), "checkpoint")
    assert not f.ok
    assert "not persisting state" in f.detail


# ------------------------------------------------------ ledger integrity

def test_torn_lines_and_duplicate_ids_are_counted(tmp_path):
    b = _base(tmp_path)
    (b / "state" / "ledger.jsonl").write_text(
        '{"decision_id": "a"}\n{"decision_id": "a"}\n{not json\n')
    f = _by(_sys(tmp_path), "ledger integrity")
    assert not f.ok
    assert "1 torn line(s), 1 duplicate" in f.detail


def test_the_ledger_is_never_auto_repaired(tmp_path):
    """It is the only record of what this desk predicted. A process that edits
    evidence unattended is how the evidence gets destroyed."""
    b = _base(tmp_path)
    (b / "state" / "ledger.jsonl").write_text('{"decision_id": "a"}\n{bad\n')
    before = (b / "state" / "ledger.jsonl").read_text()
    f = _by(_sys(tmp_path), "ledger integrity")
    assert not f.ok
    assert "NOT auto-repaired" in f.detail
    assert (b / "state" / "ledger.jsonl").read_text() == before


def test_a_clean_ledger_passes(tmp_path):
    b = _base(tmp_path)
    (b / "state" / "ledger.jsonl").write_text(
        '{"decision_id": "a"}\n{"decision_id": "b"}\n')
    assert _by(_sys(tmp_path), "ledger integrity").ok


# ----------------------------------------------------------------- disk

def test_low_disk_is_a_fault(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(1, 1, 10 * 1024 * 1024))
    f = _by(_sys(tmp_path), "disk")
    assert not f.ok
    assert "fail silently" in f.detail


# ---------------------------------------------------------------- macro

def test_briefs_all_reading_UNMEASURED_is_a_fault():
    """OBSERVED LIVE: yfinance returned 'possibly delisted' for DX-Y.NYB, ^GSPC
    and ^VIX at once — the Yahoo API, not three delistings."""
    rows = [{"brief_render": "MACRO CONTEXT: UNMEASURED", "ts": NOW.isoformat()}
            for _ in range(5)]
    f = _by(audit(rows, {"m": 1}, now=NOW), "macro")
    assert not f.ok
    assert "no DXY" in f.detail


def test_briefs_carrying_macro_pass():
    rows = [{"brief_render": "DXY -0.4%  REAL YIELD 1.9%", "ts": NOW.isoformat()}
            for _ in range(5)]
    assert _by(audit(rows, {"m": 1}, now=NOW), "macro").ok


# ------------------------------------------------- the base is optional

def test_ledger_checks_run_without_a_filesystem():
    """So the ledger half stays trivially testable, and a caller with no
    checkout gets the checks it can answer rather than a spray of UNMEASURED."""
    names = {f.check for f in audit([], None, now=NOW)}
    assert "cohorts" in names
    assert "spread profile" not in names
