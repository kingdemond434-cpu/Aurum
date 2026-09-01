"""Tests for the event bus, causal brancher and wake router (Batch I)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from golddesk.attention import AttentionConfig, triage
from golddesk.brancher import active_branches, render_branches
from golddesk.eventbus import BusEventKind, EventBus
from golddesk.planner import plan, select_specialists

from test_contract_edges import _brief

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 17, 0, tzinfo=UTC)


def test_event_bus_records_and_forces_wake():
    bus = EventBus()
    assert not bus.wake_worthy(NOW)
    bus.emit(BusEventKind.LEVEL_TOUCH, NOW, "touch")
    assert bus.wake_worthy(NOW - timedelta(seconds=1))
    assert [e.kind for e in bus.kinds_since(NOW - timedelta(seconds=1))
            ] == [BusEventKind.LEVEL_TOUCH]


def test_event_bus_detects_impulse_on_ticks():
    bus = EventBus(impulse_at=0.5)
    t = NOW
    for i in range(30):
        bus.emit_tick(3300.0 + i * 0.02, t + timedelta(seconds=i))
    assert any(e.kind == BusEventKind.PRICE_IMPULSE for e in bus.events)


def test_event_bus_isolates_old_events():
    bus = EventBus()
    bus.emit(BusEventKind.MACRO_RELEASE, NOW - timedelta(hours=1))
    assert not bus.wake_worthy(NOW - timedelta(minutes=30))


def test_brancher_scores_continuation_on_displacement():
    b = _brief(displacement_state="CONFIRMED")
    top = active_branches(b, top=3)
    assert top
    assert top[0].score >= 2


def test_brancher_repricing_on_macro_proximity():
    b = _brief()
    top = active_branches(b, minutes_to_event=20, top=3)
    assert any(br.name == "event repricing" and br.score == 2 for br in top)


def test_render_branches_text():
    b = _brief(displacement_state="CONFIRMED")
    out = render_branches(active_branches(b, top=2))
    assert "CANDIDATE READINGS" in out


def test_router_upgrades_idle_on_forced_event():
    v = triage(_brief())
    assert v.mode == "WATCH"
    p = plan(_brief(), v, forced=True)
    assert p.tier == "ANALYZE"
    assert p.forced
    assert p.specialists


def test_router_watch_gathers_nothing():
    v = triage(_brief())
    p = plan(_brief(), v, forced=False)
    assert p.tier == "WATCH"
    assert p.specialists == ()
    assert p.branches == ()


def test_router_select_specialists_for_state():
    b = _brief(displacement_state="CONFIRMED", sweep_state="CONFIRMED")
    seats = select_specialists(b)
    assert "atlas" in seats
    assert "lumen" in seats
    assert "mnemosyne" in seats
    assert seats == select_specialists(b)       # deterministic, no ordering drift


def test_router_deep_gathers_all_seats():
    seats = select_specialists(_brief(), deep=True)
    assert "orion" in seats
    assert "hephaestus" in seats

def test_ledger_tail_is_bounded_and_complete():
    from golddesk.ledger import Ledger
    lg = Ledger(Path(tempfile.mkdtemp()) / "l.jsonl")
    for i in range(3000):
        lg.append_raw({"kind": "X", "i": i, "big": "y" * 4000})
    rows = lg.tail(500)
    assert len(rows) == 500
    assert rows[0]["i"] == 2500 and rows[-1]["i"] == 2999   # correct tail window
    full = lg.read_all()
    assert len(full) == 3000
    assert [r["i"] for r in full[-500:]] == [r["i"] for r in rows]
