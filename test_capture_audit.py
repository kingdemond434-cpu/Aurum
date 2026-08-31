r"""Is the desk still exploiting, or has it quietly gone timid?

self_audit asks "is the desk WIRED". This asks "is it still TAKING WHAT IS
THERE" — a different axis, and one where a desk can pass every integrity check
and be worth nothing: refusing everything, banking 15% of the moves it calls
right, or no longer receiving the quant desk's survivors. None of those raises
an error. All of them look like a quiet week.

THE HARD PART IS NOT MEASURING, IT IS NOT LYING. A low signal rate is not
evidence of timidity — the market may be quiet — and a check that treats every
slow week as a fault trains the operator to ignore it. These tests exist mostly
to pin the refusals: what each check declines to conclude, and where it says
UNMEASURED instead of producing a ratio it cannot support.

    python3 -m pytest test_capture_audit.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.capture import (CAPTURE_FLOOR, MIN_DECISIONS, MIN_WINNERS,
                              SURVIVOR_MARK, audit, render)

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _by(fs, name):
    return next(f for f in fs if f.check == name)


def _dec(kind, when, reason="expectancy gate: no resolved history"):
    return {"kind": kind, "ts": when.isoformat(), "t0": when.isoformat(),
            "reason": reason}


def _win(mfe, realised):
    return {"kind": "TRADE_CLOSED", "mfe_r": mfe, "realised_r": realised,
            "ts": NOW.isoformat()}


# ------------------------------------------------------------- capture

def test_the_2708_leak_is_named():
    """A short reached +1.88R and kept +0.29R. The call was right and 85% was
    given back — invisible to every win-rate statistic, because it is a win."""
    f = _by(audit([_win(1.88, 0.29)] * MIN_WINNERS, now=NOW), "capture")
    assert not f.ok
    assert "15%" in f.detail
    assert "count as wins" in f.detail


def test_good_capture_passes():
    assert _by(audit([_win(2.0, 1.6)] * MIN_WINNERS, now=NOW), "capture").ok


def test_capture_is_UNMEASURED_below_the_sample_it_needs():
    """A ratio over four trades is noise wearing a percentage sign."""
    f = _by(audit([_win(1.88, 0.29)] * (MIN_WINNERS - 1), now=NOW), "capture")
    assert f.ok
    assert "UNMEASURED" in f.detail


def test_a_losing_trade_does_not_drag_the_capture_ratio():
    """Capture asks what happened to the calls that were RIGHT. Folding losers
    in would measure win rate again under a different name."""
    rows = [_win(2.0, 1.8)] * MIN_WINNERS + [{"kind": "TRADE_CLOSED",
                                              "mfe_r": 0.0, "realised_r": -1.0,
                                              "ts": NOW.isoformat()}]
    assert _by(audit(rows, now=NOW), "capture").ok


# --------------------------------------------------------- signal rate

def test_a_halved_signal_rate_is_flagged_without_being_blamed_on_timidity():
    """It cannot tell a quiet market from a timid desk, and must not pretend to."""
    old = [_dec("SIGNAL", NOW - timedelta(days=10)) for _ in range(20)]
    old += [_dec("REFUSAL_MODEL", NOW - timedelta(days=10)) for _ in range(20)]
    new = [_dec("REFUSAL_MODEL", NOW - timedelta(hours=1)) for _ in range(MIN_DECISIONS)]
    f = _by(audit(old + new, now=NOW), "signal rate")
    assert not f.ok
    assert "cannot tell them apart" in f.detail


def test_a_steady_rate_passes():
    old = [_dec("SIGNAL", NOW - timedelta(days=10)) for _ in range(20)]
    old += [_dec("REFUSAL_MODEL", NOW - timedelta(days=10)) for _ in range(20)]
    new = [_dec("SIGNAL", NOW - timedelta(hours=1)) for _ in range(20)]
    new += [_dec("REFUSAL_MODEL", NOW - timedelta(hours=1)) for _ in range(20)]
    assert _by(audit(old + new, now=NOW), "signal rate").ok


def test_the_rate_is_UNMEASURED_on_a_thin_window():
    f = _by(audit([_dec("SIGNAL", NOW)] * 5, now=NOW), "signal rate")
    assert f.ok and "UNMEASURED" in f.detail


def test_blind_bars_are_not_counted_as_decisions():
    """A bar the analyst never answered on is not a decision to stand aside —
    counting it would make an outage look like selectivity."""
    rows = [_dec("SIGNAL", NOW - timedelta(days=10)) for _ in range(MIN_DECISIONS)]
    rows += [{"kind": "BLIND", "ts": NOW.isoformat(), "t0": NOW.isoformat()}
             for _ in range(500)]
    f = _by(audit(rows, now=NOW), "signal rate")
    assert "UNMEASURED" in f.detail, "500 blind bars were counted as decisions"


# -------------------------------------------------------- dominant gate

def test_the_dominant_gate_is_always_reported():
    """The actionable half whatever the rate is doing: if one gate refuses most
    of everything, that is the lever."""
    rows = [_dec("REFUSAL_COMPILER", NOW, "expectancy gate: thin") for _ in range(9)]
    rows += [_dec("REFUSAL_MODEL", NOW, "analyst: NO_SETUP")]
    f = _by(audit(rows, now=NOW), "dominant gate")
    assert f.ok
    assert "90%" in f.detail and "expectancy gate" in f.detail


def test_naming_a_gate_is_not_an_argument_against_it():
    """Reported as PASS: the refusals it produced are already priced against
    their own forward paths by missed_money."""
    rows = [_dec("REFUSAL_COMPILER", NOW) for _ in range(50)]
    assert _by(audit(rows, now=NOW), "dominant gate").ok


# ------------------------------------------------------------ absorption

def _inbox(tmp_path, rows):
    (tmp_path / "inbox").mkdir(exist_ok=True)
    (tmp_path / "inbox" / "quant_findings.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return tmp_path


def test_a_never_delivered_inbox_names_the_likely_cause(tmp_path):
    (tmp_path / "inbox").mkdir(exist_ok=True)
    f = _by(audit([], now=NOW, base=tmp_path), "quant inbox")
    assert not f.ok
    assert "-AurumRoot" in f.detail


def test_a_stale_inbox_with_no_heartbeat_is_UNMEASURED_not_a_verdict(tmp_path):
    """THIS TEST USED TO ASSERT THE OPPOSITE, and it was wrong.

    It was named "a stale inbox is a broken link, not a quiet quant desk" and
    pinned that claim. But Sync-QuantFindings.ps1 APPENDS, and with no new rows
    it does not touch the file — so the mtime records when CONTENT last arrived
    and says nothing about whether the transport ran. The check was making
    exactly the inference its own message warned against, and this test held it
    there.

    With a heartbeat the two are separable (test_inbox_liveness.py). Without
    one, the honest answer is that it cannot be told — which is what an older
    box, predating the heartbeat, will report."""
    import os
    b = _inbox(tmp_path, [{"statement": "x"}])
    p = b / "inbox" / "quant_findings.jsonl"
    old = NOW.timestamp() - 60 * 60 * 96
    os.utime(p, (old, old))
    f = _by(audit([], now=NOW, base=b), "quant inbox")
    assert not f.ok
    assert "UNMEASURED" in f.detail
    assert "look identical" in f.detail


def test_the_survivor_check_matches_the_real_exporter_marker(tmp_path):
    """THE FALSE POSITIVE THIS FIXES. The first version guessed at
    'survivor'/'certified'/'passed', matched NONE of 69 real findings, and
    reported the channel as carrying only refutations — while it was carrying
    E2/E3 measurement results. The check was wrong, not the channel."""
    b = _inbox(tmp_path, [{"statement": f"3 cell(s) {SURVIVOR_MARK}, 1 on XAUUSD"}])
    assert _by(audit([], now=NOW, base=b), "survivors").ok


def test_no_survivor_states_BOTH_readings_rather_than_picking_one(tmp_path):
    """Absent survivors can mean nothing passed yet — the honest state of a desk
    with no forward evidence — or that the gates never ran. Different fixes."""
    b = _inbox(tmp_path, [{"statement": "rank_ic: horizon 24, ic 0.04"}])
    f = _by(audit([], now=NOW, base=b), "survivors")
    assert not f.ok
    assert "nothing has passed yet" in f.detail
    assert "no fault here" in f.detail
    assert "QQUANT_GATES.json" in f.detail


def test_measurement_findings_are_not_mistaken_for_refutations(tmp_path):
    """Real shape from the live inbox."""
    b = _inbox(tmp_path, [
        {"statement": 'push_ceiling: 9 arms measured; first = {"cagr": 0.63}',
         "grade": "E2"},
        {"statement": 'rank_ic: {"horizon": 24, "ic": 0.041}', "grade": "E3"}])
    f = _by(audit([], now=NOW, base=b), "survivors")
    assert "2 finding(s) arrived" in f.detail


# ------------------------------------------------------------ reporting

def test_the_report_calls_them_LOOK_not_BROKEN():
    """These are descriptions of what happened, not verdicts about what to
    change — acting on four trades is as much a defect as timidity."""
    text = render(audit([_win(1.88, 0.29)] * MIN_WINNERS, now=NOW))
    assert "LOOK" in text
    assert "not so a threshold moves" in text


def test_nothing_here_can_change_a_threshold():
    """A desk that adjusts its own thresholds toward a rate it likes is not
    self-healing; it is optimising its own scorecard."""
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "capture.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for f in ("Thresholds", "ev_gate", "compile_signal", "is_enforcing",
              "fallback_min_rr", "write_text"):
        assert f not in names, f"capture.py references {f!r}"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ------------------- quant's evidence pipeline, watched 24/7

def _shadow(tmp_path, updated_at, n_fwd=4):
    d = tmp_path / "quant" / "desks" / "mt5" / "reports" / "shadow"
    d.mkdir(parents=True, exist_ok=True)
    (d / "shadow_health.json").write_text(json.dumps({
        "updated_at": updated_at.isoformat(), "sleeves_with_forward_trades": n_fwd,
        "certified_sleeves_total": 16, "missing_sleeves": [], "status": "OPERATING"}),
        encoding="utf-8")
    (tmp_path / "aurum").mkdir(exist_ok=True)
    return tmp_path / "aurum"


def test_a_stale_shadow_artifact_is_caught(tmp_path):
    """OBSERVED: 33 hours old against a 15-minute sync, while MT5-ShadowSync
    fired every 15 minutes and returned 0. Publishing nothing and publishing
    successfully were byte-identical to every watchdog."""
    base = _shadow(tmp_path, NOW - timedelta(hours=33))
    f = _by(audit([], now=NOW, base=base), "shadow evidence")
    assert not f.ok
    assert "NOT" in f.detail and "now" in f.detail


def test_a_fresh_artifact_passes_and_reports_what_is_accruing(tmp_path):
    base = _shadow(tmp_path, NOW - timedelta(minutes=10))
    f = _by(audit([], now=NOW, base=base), "shadow evidence")
    assert f.ok
    assert "4 sleeve(s) accruing" in f.detail


def test_the_check_reads_the_AGE_not_the_contents(tmp_path):
    """A stale artifact's numbers stay perfectly plausible — 16 certified, 4
    accruing, status OPERATING — which is exactly why nothing else notices."""
    base = _shadow(tmp_path, NOW - timedelta(hours=33), n_fwd=4)
    assert not _by(audit([], now=NOW, base=base), "shadow evidence").ok


def test_no_quant_checkout_reads_UNMEASURED_not_healthy(tmp_path):
    (tmp_path / "aurum").mkdir()
    f = _by(audit([], now=NOW, base=tmp_path / "aurum"), "shadow evidence")
    assert f.ok
    assert "UNMEASURED" in f.detail and "not the same as healthy" in f.detail


def test_the_quant_tasks_aurum_depends_on_are_watched():
    """Aurum's absorption cannot exceed what quant certifies, so a quant task
    that stops firing degrades THIS desk — silently, because the only symptom is
    findings that stop arriving."""
    from golddesk.task_health import EXPECTED
    for t in ("MT5-ShadowSync", "MT5-Shadow", "MT5-QQuantGatesCertify", "Aurum-Sync"):
        assert t in EXPECTED, t


def test_only_the_quant_tasks_aurum_depends_on_are_watched():
    """Watching all seventeen would put this desk in the business of policing
    another one, and a watchdog reporting faults its owner cannot act on is
    noise."""
    from golddesk.task_health import EXPECTED
    mt5 = [t for t in EXPECTED if t.startswith("MT5-")]
    assert len(mt5) <= 4, mt5
