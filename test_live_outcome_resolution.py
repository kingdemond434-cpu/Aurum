from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from golddesk.features import Bar
from golddesk.ledger import Ledger
from golddesk.service import DeskService


def test_append_only_outcome_event_materialises_on_original_decision(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append_raw({"kind": "REFUSAL_MODEL", "decision_id": "d1",
                       "t0": "2026-09-01T00:00:00+00:00",
                       "decision": {"outcome_direction": "NONE"},
                       "outcome": None})
    ledger.append_raw({"kind": "DECISION_OUTCOME", "decision_id": "d1",
                       "ts": "2026-09-01T08:01:00+00:00",
                       "outcome_direction": "SHORT",
                       "path_ref": {"timeframe": "M1"},
                       "outcome": {"best_achievable_r": 2.4},
                       "opposite_outcome": {"best_achievable_r": 0.7}})
    rows = ledger.read_all()
    original = rows[0]
    assert original["outcome"]["best_achievable_r"] == 2.4
    assert original["decision"]["outcome_direction"] == "SHORT"
    assert original["decision"]["opposite_outcome"]["best_achievable_r"] == 0.7
    assert ledger.unresolved() == []


def test_service_resolves_only_post_decision_m1_path(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=9)
    ledger.append_raw({"kind": "REFUSAL_MODEL", "decision_id": "live-1",
                       "t0": t0.isoformat(), "symbol": "XAUUSD",
                       "decision": {"outcome_direction": "NONE",
                                    "outcome_reference_price": 2500.0,
                                    "outcome_risk_price": 10.0},
                       "outcome": None})
    bars = [Bar(t0 + timedelta(minutes=i + 1), 2500.0, 2500.0 + i / 20,
                2499.0 - i / 40, 2500.0 + i / 40, 1.0, 0.2)
            for i in range(540)]

    class Feed:
        def bars(self, timeframe, count):
            assert timeframe == "M1"
            return bars[-count:]

    service = DeskService.__new__(DeskService)
    service.desk = SimpleNamespace(ledger=ledger)
    service.feed = Feed()
    service.cfg = SimpleNamespace(symbol="XAUUSD")
    service._resolve_pending_decisions()
    original = ledger.read_all()[0]
    assert original["outcome"] is not None
    assert original["path_ref"]["timeframe"] == "M1"
    assert datetime.fromisoformat(original["path_ref"]["t0"]) > t0
    assert original["resolved_by_event"] is not None
