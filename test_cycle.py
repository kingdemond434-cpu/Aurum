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
    # REFUSAL_MODEL, not "NO_SETUP". This fixture asserted a kind string that
    # DecisionKind has never emitted, which is exactly how step_evidence shipped
    # an allowlist matching zero real rows: the test and the code were wrong
    # together, in the same invented vocabulary, so they agreed. The fixture now
    # speaks only kinds the ledger actually writes — pinned by
    # test_the_refusal_filter_matches_the_kinds_the_ledger_writes.
    rows.append({"ts": "2026-06-02T11:00:00+00:00", "kind": "REFUSAL_MODEL",
                 "reason": "no alignment", "forward_r": 1.2})
    (desk / "state" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return rows


# ------------------------------------------------- it degrades honestly

def test_an_empty_desk_produces_nulls_not_results(desk):
    """The honest state of a desk that has not yet run is not a finding about
    gold, and must not be reported as one."""
    assert C.run(dry=True) == 0
    text = (desk / "reports").glob("cycle-*.md").__next__().read_text(encoding='utf-8')
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

def test_mining_refuses_to_guess_the_server_offset(desk):
    """Parsing broker time as UTC shifts every trade two or three hours and
    misaligns every session inference, while every timestamp still looks
    ordinary. There is no safe default."""
    (desk / "inbox" / "copytrade").mkdir(parents=True)
    (desk / "inbox" / "copytrade" / "s.csv").write_text("x\n", encoding="utf-8")
    out = C.step_mining({})
    assert "no safe default" in out


def test_mining_says_where_to_put_the_files_when_there_are_none(desk):
    out = C.step_mining({})
    assert "nothing to mine" in out


def test_mining_ingests_and_reverse_engineers(desk):
    inbox = desk / "inbox" / "copytrade"
    inbox.mkdir(parents=True)
    (desk / "state" / "ingest_offset.txt").write_text("3", encoding="utf-8")
    rows = ["Ticket,Position,Symbol,Type,Entry,Volume,Price,Time,S/L,T/P,Profit"]
    for i in range(30):
        rows.append(f"{i*2+1},P{i},XAUUSD,BUY,in,0.10,2000.00,"
                    f"2026-06-{(i % 28)+1:02d} 10:00:00,1990.00,2030.00,0")
        rows.append(f"{i*2+2},P{i},XAUUSD,SELL,out,0.10,2010.00,"
                    f"2026-06-{(i % 28)+1:02d} 14:00:00,,,100")
    (inbox / "s.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    ctx = {}
    out = C.step_mining(ctx)
    assert "30 paired trades" in out
    assert "REVERSE-ENGINEERING REPORT" in out
    assert len(ctx["copytrades"]) == 30


def test_mining_is_idempotent_across_runs(desk):
    inbox = desk / "inbox" / "copytrade"
    inbox.mkdir(parents=True)
    (desk / "state" / "ingest_offset.txt").write_text("3", encoding="utf-8")
    (inbox / "s.csv").write_text(
        "Ticket,Position,Symbol,Type,Entry,Volume,Price,Time\n"
        "1,P1,XAUUSD,BUY,in,0.10,2000.00,2026-06-01 10:00:00\n"
        "2,P1,XAUUSD,SELL,out,0.10,2010.00,2026-06-01 14:00:00\n", encoding="utf-8")
    C.step_mining({})
    assert "0 new deal(s)" in C.step_mining({})


def test_the_regime_contest_not_running_is_not_the_incumbent_winning(desk):
    out = C.step_regime({})
    assert "not the same as" in out and "incumbent won" in out


def test_a_thin_series_gets_no_regime_verdict(desk):
    import numpy as np
    rng = np.random.default_rng(0)
    ctx = {"regime_series": {"returns": rng.normal(size=100),
                             "incumbent": rng.integers(0, 3, 100),
                             "forward": rng.normal(size=100)}}
    assert "needed before a contest means anything" in C.step_regime(ctx)


def test_the_regime_contest_runs_on_a_real_series(desk):
    import numpy as np
    rng = np.random.default_rng(3)
    n = 2000
    r = rng.normal(size=n)
    ctx = {"regime_series": {"returns": r, "incumbent": rng.integers(0, 3, n),
                             "forward": np.roll(r, -1)}}
    out = C.step_regime(ctx)
    assert "REGIME CONTEST" in out
    assert ctx["regime_contest"]["n_test"] > 0


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
        "transfer_test": "does Aurum's asia range widen on CPI days"}) + "\n", encoding="utf-8")
    out = C.step_absorb({})
    assert "1 new finding" in out


def test_a_malformed_finding_costs_one_row_not_the_step(desk):
    inbox = desk / "inbox"
    inbox.mkdir()
    (inbox / "quant_findings.jsonl").write_text(
        json.dumps({"nonsense": 1}) + "\n"
        + json.dumps({"statement": "s", "source": "x", "grade": "E4",
                      "measured_on": "m", "transfer_test": "t"}) + "\n", encoding="utf-8")
    assert "1 new finding" in C.step_absorb({})


def test_re_running_absorb_does_not_re_queue(desk):
    inbox = desk / "inbox"
    inbox.mkdir()
    (inbox / "quant_findings.jsonl").write_text(json.dumps({
        "statement": "s", "source": "x", "grade": "E4",
        "measured_on": "m", "transfer_test": "t"}) + "\n", encoding="utf-8")
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
    text = next((desk / "reports").glob("cycle-*.md")).read_text(encoding='utf-8')
    assert "FAILED STEPS: boom" in text
    assert "== CENSUS ==" in text, "a later step was skipped"


def test_the_stamp_records_the_attempt_not_the_success(desk, monkeypatch):
    """Stamping only on a clean run re-runs every step next invocation —
    re-sending notifications and re-queueing findings."""
    monkeypatch.setattr(C, "STEPS", (("boom", lambda c: (_ for _ in ()).throw(
        RuntimeError("x"))),))
    # dry=False deliberately: this is about FAILURE not preventing the stamp,
    # and a dry run now correctly declines to stamp at all, so running it dry
    # would test the wrong mechanism and pass for the wrong reason.
    C.run(dry=False)
    state = json.loads((desk / "state" / "cycle_state.json").read_text(encoding='utf-8'))
    assert state["last_run"]
    assert state["last_failed_steps"] == ["boom"]


def test_the_cycle_runs_once_a_day_unless_forced(desk):
    # Also dry=False. With dry runs no longer stamping, the original version of
    # this test asserted that two un-stamping runs both return 0 — true, and
    # nothing to do with once-a-day.
    C.run(dry=False)
    assert C.run(dry=False) == 0
    C.run(dry=False, force=True)


def test_a_torn_ledger_line_costs_one_row_not_the_cycle(desk):
    seed_ledger(desk, n=30)
    with (desk / "state" / "ledger.jsonl").open("a", encoding="utf-8") as f:
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
    assert len(files) == 1 and files[0].read_text(encoding='utf-8').startswith("AURUM DAILY CYCLE")


def test_nothing_in_the_cycle_can_promote_or_loosen_a_gate():
    """A loop that could widen its own limits would, because looser gates
    produce more signals and more signals feel like progress."""
    import ast
    src = Path(C.__file__).read_text(encoding='utf-8')
    banned = {"promote", "enforce", "set_threshold", "arm", "seal"}
    hits = [f"{n.lineno}:{n.func.attr}" for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in banned]
    assert not hits, f"the cycle can change authority: {hits}"


# ------------------------------------------------- free driver feed is wired

def test_attribution_falls_back_to_the_free_live_reading(desk, monkeypatch):
    """A fitted decomposition needs history; today's reading is free and is
    worth reporting on its own."""
    from golddesk.drivers_free import DriverPoint
    monkeypatch.setattr(
        "golddesk.drivers_free.build_drivers",
        lambda *a, **k: {"dxy": DriverPoint("dxy", 0.4, 104.0, None, "yahoo/X"),
                         "vix": DriverPoint("vix", -2.0, 15.0, None, "yahoo/^VIX")})
    out = C.step_attribution({})
    assert "DRIVER COVERAGE" in out and "EXACT" in out


def test_a_driver_fetch_failure_is_UNAVAILABLE_not_neutral(desk, monkeypatch):
    monkeypatch.setattr("golddesk.drivers_free.build_drivers",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    out = C.step_attribution({})
    assert "UNAVAILABLE" in out
    assert "not the same as 'nothing was driving gold'" in out


def test_nothing_observed_is_still_not_a_neutral_reading(desk, monkeypatch):
    from golddesk.drivers_free import DriverPoint
    monkeypatch.setattr(
        "golddesk.drivers_free.build_drivers",
        lambda *a, **k: {"dxy": DriverPoint("dxy", None, None, None, "UNAVAILABLE")})
    assert "UNAVAILABLE" in C.step_attribution({})


# --------------------------------------------- decay, levers, entries wired

def test_decay_runs_over_every_mechanism_including_the_armed_book(desk):
    """promoter.py explicitly does not manage the armed book. A sleeve carrying
    real capital that nobody monitors is the gap this closes."""
    seed_ledger(desk, n=180)
    ctx = {}
    C.step_evidence(ctx)
    out = C.step_decay(ctx)
    assert "BOOK HEALTH" in out
    assert {s.sleeve for s in ctx["decay_states"]} == {"a", "b", "c"}


def test_decay_reports_the_detection_latency_alongside_the_verdict(desk):
    seed_ledger(desk, n=180)
    ctx = {}
    C.step_evidence(ctx)
    out = C.step_decay(ctx)
    assert "DETECTION LATENCY" in out
    assert "by the time decay is provable it has been paid for" in out


def test_no_resolved_trades_means_unmonitored_not_healthy(desk):
    assert "UNMONITORED is not the same as healthy" in C.step_decay({"rows": []})


def test_the_lever_ranking_needs_a_book_before_it_describes_one(desk):
    out = C.step_levers({"decay_states": [], "r_multiples": []})
    assert "before it describes this book" in out


def test_the_levers_run_once_there_is_a_book(desk):
    seed_ledger(desk, n=200)
    ctx = {}
    C.step_evidence(ctx)
    C.step_decay(ctx)
    out = C.step_levers(ctx)
    assert "GROWTH LEVERS" in out and "BINDING CONSTRAINT" in out


def test_entries_without_bars_says_so_rather_than_claiming_nothing_clusters(desk):
    """Not run is not the same as 'his entries cluster on nothing'."""
    out = C.step_entries({"copytrades": [object()]})
    assert "cannot run without them" in out
    assert "Not run is not the same as" in out


def test_entries_with_no_mined_trades_is_silent(desk):
    assert "nothing to classify" in C.step_entries({})


# ---------------------------------------------------- a dry run is a rehearsal
#
# Found in operation, not in test. The documented sequence is `--dry` to read
# the output, then the real run — and the dry run stamped the day, so the second
# command answered "cycle already ran" and the operator had to reach for
# --force. A rehearsal that consumes the thing it rehearses is not a rehearsal.

def test_a_dry_run_does_not_consume_the_day(tmp_path, monkeypatch):
    import aurum_cycle as C
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(C, "CYCLE_STATE", tmp_path / "cycle_state.json")
    monkeypatch.setattr(C, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(C, "LOG", tmp_path / "cycle.log")
    monkeypatch.setattr(C, "LEDGER", tmp_path / "ledger.jsonl")

    C.run(dry=True)
    assert not (tmp_path / "cycle_state.json").exists(), (
        "the dry run stamped the day and blocked the real one")

    C.run(dry=False)
    state = json.loads((tmp_path / "cycle_state.json").read_text(encoding='utf-8'))
    assert state.get("last_run"), "the real run must stamp"


def test_the_real_run_still_refuses_to_repeat_itself(tmp_path, monkeypatch):
    import aurum_cycle as C
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(C, "CYCLE_STATE", tmp_path / "cycle_state.json")
    monkeypatch.setattr(C, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(C, "LOG", tmp_path / "cycle.log")
    monkeypatch.setattr(C, "LEDGER", tmp_path / "ledger.jsonl")

    C.run(dry=False)
    before = (tmp_path / "cycle_state.json").read_text(encoding='utf-8')
    C.run(dry=False)
    assert (tmp_path / "cycle_state.json").read_text(encoding='utf-8') == before


def test_the_measurement_scripts_the_desk_owns_are_actually_run():
    """BUILT, TESTED, CORRECT, AND SCHEDULED BY NOTHING -- the failure class this repo keeps
    hitting. missed_money.py and mgmt_counterfactual.py both shipped with a `__main__` block
    pointed at backtest fixtures, so the LIVE ledger was never their subject and neither ever
    ran on real evidence.

    They matter more than most: refusals are the majority of what this analyst produces and
    missed_money is the only thing that grades them, while management is roughly half of
    realised R and mgmt_counterfactual is the only thing that prices the alternative arms.

    Order is asserted, not incidental: `levers` ranks where the next unit of effort should go,
    and it cannot do that honestly while the cost of refusals and the value of the management
    arms are both still invisible."""
    import aurum_cycle

    names = [n for n, _ in aurum_cycle.STEPS]
    assert "missed_money" in names, "refusals graded by nothing"
    assert "mgmt_counterfactual" in names, "management arms priced by nothing"
    assert names.index("decay") < names.index("missed_money") < names.index("levers")
    assert names.index("mgmt_counterfactual") < names.index("levers")


def test_the_new_measurement_steps_refuse_honestly_with_no_ledger():
    """An absent ledger must read UNMEASURED, never as a clean zero. 'no missed money' and
    'nobody has looked' are opposite findings and the desk must not confuse them -- which is
    exactly the state it is in today, with two ledger rows."""
    import aurum_cycle

    steps = dict(aurum_cycle.STEPS)
    for name in ("missed_money", "mgmt_counterfactual"):
        _, text = aurum_cycle.run_step(name, steps[name], {})
        assert "UNMEASURED" in text, f"{name} reported absence as a result"


# ------------------------------------------- the refusal/blind vocabulary

def test_the_refusal_filter_matches_the_kinds_the_ledger_writes(desk):
    """The filter and the writer must share ONE vocabulary.

    step_evidence tested `kind in ("NO_SETUP","REFUSED","REFUSAL","VETO","BLOCKED")`.
    DecisionKind emits none of those, so a ledger full of refusals reported
    "0 refusals recorded" — absence read as a clean answer on the exact
    quantity the desk exists to measure (0 of 2 real rows matched, checked
    against the live ledger). This walks the enum itself rather than a copied
    list, so adding a REFUSAL_* kind cannot silently fall outside the filter.
    """
    from golddesk.ledger import DecisionKind
    refusal_kinds = [k.value for k in DecisionKind if k.value.startswith("REFUSAL")]
    assert refusal_kinds, "no REFUSAL_* kinds — the enum was renamed under this test"
    rows = [{"ts": "2026-06-01T10:00:00+00:00", "kind": k, "reason": "x"}
            for k in refusal_kinds]
    (desk / "state" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    ctx = {}
    out = C.step_evidence(ctx)
    assert len(ctx["refusals"]) == len(refusal_kinds), ctx["refusals"]
    assert f"{len(refusal_kinds)} refusals recorded" in out, out


def test_a_blind_bar_is_reported_and_is_never_counted_as_a_refusal(desk):
    """An outage must not be able to read as discipline.

    A bar the analyst never answered on is journalled BLIND. If it were folded
    into the refusal count, a desk whose provider was timing out would look
    like a desk patiently standing aside — and `missed_money` would bill a gate
    that never ran for the forward move.
    """
    rows = [{"ts": "2026-06-01T10:00:00+00:00", "kind": "REFUSAL_MODEL", "reason": "x"},
            {"ts": "2026-06-01T10:15:00+00:00", "kind": "BLIND",
             "reason": "BLIND: analyst unavailable at read — TimeoutExpired"}]
    (desk / "state" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    ctx = {}
    out = C.step_evidence(ctx)
    assert len(ctx["refusals"]) == 1, "a blind bar was counted as a refusal"
    assert len(ctx["blind"]) == 1
    assert "1 refusals recorded" in out
    assert "1 BLIND bars" in out and "NOT refusals" in out


def test_missed_money_does_not_attribute_forgone_value_to_a_blind_bar(desk):
    """The reason BLIND is not named REFUSAL_*.

    price_restrictions selects with `.startswith("REFUSAL")` and charges the
    forward move to whatever declined. Nothing declined on a blind bar, so
    there is no restriction to charge.
    """
    import missed_money as M
    rows = [{"ts": "2026-06-01T10:15:00+00:00", "kind": "BLIND",
             "reason": "BLIND: analyst unavailable at read — TimeoutExpired",
             "context": {"spread": 0.2}, "outcome": {"mfe_r": 3.0}}]
    assert M.price_restrictions(rows) == []
    assert M.coverage(rows) == ["no refusals to assess"]
