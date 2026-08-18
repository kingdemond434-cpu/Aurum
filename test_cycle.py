"""Eleven modules were built and tested and eight were imported by nothing.

A tested module on no execution path changes no decision and produces no
evidence — it is a design document that happens to run. These tests are about
the cycle actually invoking them, and about it degrading honestly when there is
nothing to say.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import aurum_cycle as C


@pytest.fixture()
def desk(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(C, "BASE", tmp_path)
    monkeypatch.setattr(C, "STATE_DIR", state)
    monkeypatch.setattr(C, "LEDGER", state / "ledger.jsonl")
    monkeypatch.setattr(C, "CYCLE_STATE", state / "cycle_state.json")
    monkeypatch.setattr(C, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(C, "LOG", state / "cycle.log")
    return tmp_path


def seed_ledger(desk, n=60):
    rows = []
    for i in range(n):
        rows.append({"ts": f"2026-06-{(i % 28) + 1:02d}T10:00:00+00:00",
                     "kind": "SIGNAL", "mechanism": ["a", "b", "c"][i % 3],
                     "realised_r": 1.8 if i % 3 else -1.0})
    rows.append({"ts": "2026-06-02T11:00:00+00:00", "kind": "NO_SETUP",
                 "reason": "no alignment", "forward_r": 1.2})
    (desk / "state" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    return rows


# ------------------------------------------------- it degrades honestly

def test_an_empty_desk_produces_nulls_not_results(desk):
    """The honest state of a desk that has not yet run is not a finding about
    gold, and must not be reported as one."""
    assert C.run(dry=True) == 0
    text = (desk / "reports").glob("cycle-*.md").__next__().read_text()
    assert "LEDGER EMPTY" in text
    assert "not a result" in text


def test_no_evidence_yields_no_size_rather_than_a_default(desk):
    ctx = {"r_multiples": [], "rows": []}
    out = C.step_growth(ctx)
    assert "watched long enough" in out


def test_missing_drivers_read_UNAVAILABLE_not_neutral(desk):
    """Saying 'no drivers moved' asserts something nobody measured."""
    out = C.step_attribution({})
    assert "UNAVAILABLE" in out
    assert "not the same as 'nothing was driving gold'" in out


def test_a_missing_checkpoint_makes_channel_health_UNKNOWN_not_healthy(desk):
    assert "UNKNOWN rather than healthy" in C.step_channel({})


def test_a_thin_sample_gets_no_deflated_sharpe(desk):
    """A Sharpe over fewer than thirty trades is a number, not evidence."""
    out = C.step_census({"r_multiples": [0.5] * 10})
    assert "30 required" in out


# --------------------------------------------------- it actually runs them

def test_the_cycle_invokes_every_wired_module(desk):
    seed_ledger(desk)
    ctx = {}
    names = [n for n, _ in C.STEPS]
    for n, fn in C.STEPS:
        ok, text = C.run_step(n, fn, ctx)
        assert ok, f"{n} failed: {text}"
    assert {"evidence", "growth", "census", "absorb", "channel",
            "attribution"} <= set(names)


def test_growth_reaches_a_real_recommendation_once_evidence_exists(desk):
    seed_ledger(desk)
    ctx = {}
    C.step_evidence(ctx)
    out = C.step_growth(ctx)
    assert "DERIVED, not chosen" in out
    assert ctx["growth"].q > 0


def test_the_census_reads_the_linkage_registry(desk):
    from golddesk.linkage import LinkedRegistry, Run
    reg = LinkedRegistry()
    reg.register_hypotheses(["H1"])
    reg.register_run(Run("R1", ("H1",), "backtest", "2026-08-01T00:00:00+00:00",
                         outcome="ABANDONED"))
    reg.save(desk / "state" / "linkage.json")
    out = C.step_census({"r_multiples": []})
    assert "TRIALS FOR FDR       1" in out


def test_absorb_queues_findings_from_the_inbox(desk):
    inbox = desk / "inbox"
    inbox.mkdir()
    (inbox / "quant_findings.jsonl").write_text(json.dumps({
        "statement": "asia session range widens after a US CPI print",
        "source": "hunt12", "grade": "E4", "measured_on": "XAUUSD H1 2018-2026",
        "transfer_test": "does Aurum's asia range widen on CPI days"}) + "\n")
    out = C.step_absorb({})
    assert "1 new finding" in out


def test_a_malformed_finding_costs_one_row_not_the_step(desk):
    inbox = desk / "inbox"
    inbox.mkdir()
    (inbox / "quant_findings.jsonl").write_text(
        json.dumps({"nonsense": 1}) + "\n"
        + json.dumps({"statement": "s", "source": "x", "grade": "E4",
                      "measured_on": "m", "transfer_test": "t"}) + "\n")
    assert "1 new finding" in C.step_absorb({})


def test_re_running_absorb_does_not_re_queue(desk):
    inbox = desk / "inbox"
    inbox.mkdir()
    (inbox / "quant_findings.jsonl").write_text(json.dumps({
        "statement": "s", "source": "x", "grade": "E4",
        "measured_on": "m", "transfer_test": "t"}) + "\n")
    C.step_absorb({})
    assert "0 new finding" in C.step_absorb({})


# ------------------------------------------------ one failure is not six

def test_a_failing_step_does_not_abort_the_rest(desk, monkeypatch):
    """A cycle that stops at the first problem loses the five things that would
    have worked, which is how a desk goes dark for a week over one bad fetch."""
    def boom(ctx):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(C, "STEPS", (("boom", boom),
                                     ("evidence", C.step_evidence),
                                     ("census", C.step_census)))
    assert C.run(dry=True) == 1
    text = next((desk / "reports").glob("cycle-*.md")).read_text()
    assert "FAILED STEPS: boom" in text
    assert "== CENSUS ==" in text, "a later step was skipped"


def test_the_stamp_records_the_attempt_not_the_success(desk, monkeypatch):
    """Stamping only on a clean run re-runs every step next invocation —
    re-sending notifications and re-queueing findings."""
    monkeypatch.setattr(C, "STEPS", (("boom", lambda c: (_ for _ in ()).throw(
        RuntimeError("x"))),))
    C.run(dry=True)
    state = json.loads((desk / "state" / "cycle_state.json").read_text())
    assert state["last_run"]
    assert state["last_failed_steps"] == ["boom"]


def test_the_cycle_runs_once_a_day_unless_forced(desk):
    C.run(dry=True)
    assert C.run(dry=True) == 0
    C.run(dry=True, force=True)


def test_a_torn_ledger_line_costs_one_row_not_the_cycle(desk):
    seed_ledger(desk, n=30)
    with (desk / "state" / "ledger.jsonl").open("a") as f:
        f.write('{"ts": "2026-06-30T10:00:00+00:00", "realised_r": 1.')
    ctx = {}
    out = C.step_evidence(ctx)
    assert out.startswith("31 ledger rows"), out
    assert len(ctx["r_multiples"]) == 30


def test_the_ledger_path_is_resolved_at_call_time(desk):
    """A module constant captured in a default argument is frozen at import, so
    the parameter only LOOKS configurable — relocating the desk would silently
    keep reading the original path and report an empty ledger for a full file.
    That is precisely what this test caught."""
    seed_ledger(desk, n=5)
    assert len(C._rows()) == 6


def test_a_send_failure_does_not_fail_the_cycle(desk, monkeypatch):
    monkeypatch.setattr("golddesk.notify.build_sink",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    assert C.run() == 0


# ------------------------------------------------------------- the report

def test_the_report_is_written_to_a_file_not_only_sent(desk):
    """Telegram caps a message; the record must survive that."""
    C.run(dry=True)
    files = list((desk / "reports").glob("cycle-*.md"))
    assert len(files) == 1 and files[0].read_text().startswith("AURUM DAILY CYCLE")


def test_nothing_in_the_cycle_can_promote_or_loosen_a_gate():
    """A loop that could widen its own limits would, because looser gates
    produce more signals and more signals feel like progress."""
    import ast
    src = Path(C.__file__).read_text()
    banned = {"promote", "enforce", "set_threshold", "arm", "seal"}
    hits = [f"{n.lineno}:{n.func.attr}" for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in banned]
    assert not hits, f"the cycle can change authority: {hits}"
