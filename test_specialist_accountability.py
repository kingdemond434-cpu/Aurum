from datetime import datetime, timedelta, timezone

from golddesk.constitution import measure
from golddesk.ledger import Ledger
from golddesk.snapshot import SnapshotBuilder
from golddesk.specialist_accountability import (
    ACCOUNTABILITY_VERSION, VERDICT_KIND, decision_stamp, earned_brief_block,
    record_verdicts, render_dashboard, scorecards)
from golddesk.specialists import Council, SpecialistRead

UTC = timezone.utc
T0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class Fixed:
    name = "Atlas"
    role = "macro context"
    def read(self, snapshot):
        return SpecialistRead(self.name, "LONG", 0.8, horizon_bars=4,
                              why="USD impulse weakening", role=self.role)


def _snapshot(i=0):
    t = T0 + timedelta(minutes=15 * i)
    return SnapshotBuilder("XAUUSD", "M15", t).add("x", i, t).build()


def _resolved_rows(n=30, changed=True, move=0.6):
    rows = []
    for i in range(n):
        s = _snapshot(i)
        rows.append({
            "kind": VERDICT_KIND, "version": ACCOUNTABILITY_VERSION,
            "state_id": s.state_id, "content_hash": s.content_hash,
            "specialist": "Atlas", "role": "macro context",
            "available": True, "direction": "LONG", "strength": 0.8,
            "probability_up": 0.9, "horizon_bars": 4, "why": "macro"})
        action = "FLAT" if changed else "LONG"
        kind = "REFUSAL_MODEL" if changed else "SIGNAL"
        decision = {"state_id": s.state_id, "content_hash": s.content_hash,
                    "outcome_direction": "LONG"}
        if action == "LONG":
            decision["direction"] = "LONG"
        rows.append({
            "kind": kind, "decision": decision,
            "context": {"trend_direction": "UP", "volatility_state": "NORMAL",
                        "session": "LONDON"},
            "outcome": {"returns_r": {"h1": move}}})
    return rows


def test_every_verdict_is_appended_as_a_permanent_row(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    s = _snapshot()
    rep = Council([Fixed()]).report(s)
    written = record_verdicts(ledger, s, rep)
    assert len(written) == 1
    row = ledger.read_all()[0]
    assert row["kind"] == VERDICT_KIND
    assert row["state_id"] == s.state_id
    assert row["content_hash"] == s.content_hash
    assert row["specialist"] == "Atlas"


def test_replaying_a_state_after_restart_does_not_duplicate_verdicts(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    s = _snapshot()
    rep = Council([Fixed()]).report(s)
    assert len(record_verdicts(ledger, s, rep)) == 1
    assert record_verdicts(ledger, s, rep) == []
    assert len(ledger.read_all()) == 1


def test_decision_stamp_preserves_reads_without_granting_authority():
    s = _snapshot()
    stamp = decision_stamp(Council([Fixed()]).report(s))
    assert stamp["state_id"] == s.state_id
    assert stamp["specialist_verdicts"][0]["direction"] == "LONG"
    assert stamp["specialist_authority"] == "ADVISORY_ONLY_COMPILER_FINAL"


def test_changed_decisions_net_r_brier_regime_and_standing_are_quantitative():
    card = scorecards(_resolved_rows())[0]
    assert card.changed_n == 30
    assert card.incremental_net_r > 0
    assert card.brier_improvement > 0
    assert card.regime_value["UP|NORMAL|LONDON"].changed_n == 30
    assert card.standing == "EARNED"


def test_unchanged_agreement_does_not_manufacture_standing():
    card = scorecards(_resolved_rows(changed=False))[0]
    assert card.changed_n == 0
    assert card.standing == "SHADOW"


def test_only_earned_specialists_reach_the_analyst_brief():
    s = _snapshot()
    rep = Council([Fixed()]).report(s)
    cards = scorecards(_resolved_rows())
    block = earned_brief_block(rep, cards)
    assert "Atlas" in block and "COMPILER" in block
    assert "never votes" in block


def test_explicit_gate_id_survives_reason_wording_changes():
    rows = [{"kind": "REFUSAL_COMPILER", "reason": "completely rewritten",
             "decision": {"gate_id": "entry.expectancy_gate"},
             "outcome": {"mfe_r": 2.5, "mae_r": -0.2,
                         "time_to_mfe_s": 60, "time_to_mae_s": 120}}]
    costs = measure(rows)
    assert costs[0].restriction_id == "entry.expectancy_gate"
    assert costs[0].forgone_r == 2.0


def test_dashboard_contains_evidence_not_a_decorative_edge_number():
    text = render_dashboard(_resolved_rows())
    assert "NO VOTING" in text
    assert "changed" in text
    assert "Brier" in text
    assert "edge score" not in text.lower()
