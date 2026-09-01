"""Contract tests for the six post-P0 surfaces.

Each feature is deterministic, data-absent-honest, and must never break the
decision path the way the compiler gate rules must never break it:

  - active acquisition (#1): fulfill what the desk owns, UNAVAILABLE otherwise
  - decision expiry / half-life (#3): pure curve, monotone, honest defaults
  - adaptive compute (#5): triage tiers, idle skips measured, errors degrade up
  - counterfactual replay (#4): same bars, every other execution, first-touch
  - state-change booking/resolution (#2): scored on its own timetable
  - failure library (#6): clusters only what the desk actually paid for
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from golddesk.acquire import (AcquireState, Tick, TickRing, fulfill_requests,
                              render_follow_up)
from golddesk.analyst import (AdversarialReview, AnalystRead, Context, Level,
                              LevelKind, MarketBrief, PathForecast,
                              StateChangePrediction, Setup, Thresholds,
                              compile_signal)
from golddesk.attention import AttentionConfig, triage
from golddesk.counterfactual import best_variant, replay, to_sheet
from golddesk.costs import CostModel
from golddesk.failure_memory import cluster_rows, failure_memory_block
from golddesk.signal_decay import decay_multiplier, is_fillable, repriced_ev
from golddesk.state_change import (PRED_KIND, brier, resolve)
from golddesk.ledger import Bar as LedgerBar

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# acquire (#1)
# ---------------------------------------------------------------------------

def test_unknown_request_is_unavailable():
    st = AcquireState(tick_ring=TickRing())
    blocks = fulfill_requests(["show me the moon"], st, now=NOW)
    assert len(blocks) == 1
    assert "UNAVAILABLE" in blocks[0][1]
    assert "Nothing invented" in blocks[0][1]


def test_tick_request_renders_real_numbers():
    st = AcquireState(tick_ring=TickRing())
    for i in range(40):
        st.tick_ring.push(Tick(NOW + timedelta(seconds=i), 3300 + i * 0.01,
                               3300.1 + i * 0.01))
    blocks = fulfill_requests(["last 30s ticks"], st, now=NOW)
    assert blocks, "tick request must fulfill with a live buffer"
    assert "UNAVAILABLE" not in blocks[0][1]
    assert "ticks" in blocks[0][1]
    assert "3300" in blocks[0][1]


def test_crossmarket_refresh_unavailable_without_fetcher():
    st = AcquireState(tick_ring=TickRing())
    blocks = fulfill_requests(["refresh dxy"], st, now=NOW)
    assert "UNAVAILABLE" in blocks[0][1]


def test_fetch_change_uses_wired_fetcher():
    def fake(key, hours):
        return (0.012, NOW, "test")
    st = AcquireState(fetcher=fake)
    got = st.fetch_change("DXY")
    assert got is not None
    assert got[0] == 0.012


def test_render_follow_up_block():
    block = render_follow_up([("refresh dxy", "UNAVAILABLE — nothing invented.")])
    assert "[REQUESTED FOLLOW-UP]" in block
    assert "refresh dxy" in block


# ---------------------------------------------------------------------------
# signal_decay (#3)
# ---------------------------------------------------------------------------

def test_decay_curve_is_pure_half_life():
    assert decay_multiplier(0, 30) == 1.0
    assert decay_multiplier(30, 30) == 0.5
    assert decay_multiplier(60, 30) == 0.25
    assert decay_multiplier(5, 30) > decay_multiplier(6, 30)


def test_fillability_boundary():
    ok, frac = is_fillable(90, None)          # default 90-min half-life
    assert ok and abs(frac - 0.5) < 1e-9
    ok2, _ = is_fillable(370, 90)             # four+ half-lives: stale
    assert not ok2


def test_repriced_ev_scales_down_only():
    r = repriced_ev(2.0, 120, 60)
    assert 0 < r < 2.0
    assert repriced_ev(2.0, 0, 60) == 2.0


# ---------------------------------------------------------------------------
# attention (#5)
# ---------------------------------------------------------------------------

def _brief(**ctx):
    base = dict(trend_direction="UP", trend_health="STRONG",
                trend_maturity="MID", volatility_state="NORMAL",
                htf_alignment="ALIGNED", displacement_state="NONE",
                sweep_state="NONE", reclaim_state="NONE",
                pullback_depth="NONE", distance_from_session_extreme="MID")
    base.update(ctx)
    return MarketBrief(
        symbol="XAUUSD", as_of_utc=NOW, session="LONDON",
        bid=3300.0, ask=3300.2, spread=0.2, tick_age_s=1.0, atr=20.0,
        context=Context(**base), levels=(), trigger_price=None)


def test_idle_state_watches():
    v = triage(_brief())
    assert v.mode == "WATCH"
    assert v.score == 0


def test_confirmed_displacement_analyzes():
    v = triage(_brief(displacement_state="CONFIRMED"),
               AttentionConfig(volatility_z=-1.0, spread_pct=None))
    assert v.mode in ("ANALYZE", "DEEP")
    assert v.score >= 2


def test_near_level_adds_points():
    b = _brief(displacement_state="CONFIRMED")
    # mid = 3300.1, level 3308 is ~7.9 away = ~0.4 ATR < 0.6 ATR -> near
    near = MarketBrief(
        symbol="XAUUSD", as_of_utc=NOW, session="LONDON",
        bid=3300.0, ask=3300.2, spread=0.2, tick_age_s=1.0, atr=20.0,
        context=b.context, trigger_price=None,
        levels=(Level("L1", LevelKind.SWING_HIGH, 3308.0, "M15", 5, True),))
    v = triage(near, AttentionConfig())
    assert v.near_level
    assert v.score >= 4
    assert v.mode == "DEEP"


def test_disabled_attention_degrades_to_analyze():
    v = triage(_brief(), AttentionConfig(enabled=False))
    assert v.mode == "ANALYZE"


# ---------------------------------------------------------------------------
# counterfactual (#4)
# ---------------------------------------------------------------------------

def _bars():
    """8 bars: the stop (3295) is hit on bar 3, then price grinds to 3309."""
    out = []
    t0 = NOW
    o = 3300.0
    for i, (close, low, high) in enumerate([
            (3302.0, 3299.0, 3304.0), (3300.0, 3298.0, 3303.0),
            (3296.0, 3294.0, 3300.0),   # low 3294 <= stop 3295 -> STOP
            (3297.5, 3293.0, 3298.0), (3300.0, 3296.0, 3301.0),
            (3304.0, 3299.0, 3305.0), (3307.0, 3303.0, 3308.0),
            (3309.0, 3306.0, 3310.0)]):
        out.append(LedgerBar(t0 + timedelta(minutes=15 * (i + 1)), o, high,
                             low, close))
        o = close
    return out


def _long_row():
    return {"decision": {"entry": 3300.0, "stop": 3295.0, "tp2": 3308.0,
                         "direction": "LONG", "t0": NOW.isoformat()}}


def test_replay_enumerates_all_variants():
    sheet = to_sheet(replay(_long_row(), _bars()))
    names = {v["name"] for v in sheet["variants"]}
    assert {"hold_full", "opposite", "tight_stop", "wide_tp2"} <= names
    assert any(v["r"] is not None for v in sheet["variants"])


def test_replay_has_best_variant():
    variants = replay(_long_row(), _bars())
    assert best_variant(variants) is not None
    assert best_variant(variants).r >= max(v.r for v in variants if v.r is not None)


def test_replay_empty_bars_is_safe():
    sheet = to_sheet(replay(_long_row(), []))
    assert all(v["r"] is None for v in sheet["variants"])
    assert sheet["best_variant"] is None


def test_replay_reenter_appears_after_stop():
    variants = replay(_long_row(), _bars())
    assert any(v.name == "reenter_after_stop" for v in variants)


# ---------------------------------------------------------------------------
# state_change (#2)
# ---------------------------------------------------------------------------

def _prediction_rows():
    return [{"kind": PRED_KIND, "t0": NOW.isoformat(), "symbol": "XAUUSD",
             "decision": {"meter_key": "volatility_state",
                          "transition": "volatility expansion",
                          "probability": 0.8, "onset_minutes": 15}}]


def test_resolve_matches():
    rows = _prediction_rows()
    rows.append({"kind": "TRADE_CLOSED",
                 "t0": (NOW + timedelta(minutes=10)).isoformat(),
                 "context": {"volatility_state": {"state": "expansion"}}})
    outs = resolve(rows, lambda row, meter: row.get("context", {}).get(meter))
    assert outs[0].outcome == "MATCH"
    assert outs[0].score() == 0.8


def test_resolve_misses():
    rows = _prediction_rows()
    rows.append({"kind": "TRADE_CLOSED",
                 "t0": (NOW + timedelta(minutes=10)).isoformat(),
                 "context": {"volatility_state": {"state": "contracting"}}})
    outs = resolve(rows, lambda row, meter: row.get("context", {}).get(meter))
    assert outs[0].outcome == "MISS"
    assert abs(outs[0].score() - 0.2) < 1e-9


def test_resolve_elapsed_when_horizon_passes():
    rows = _prediction_rows()
    rows.append({"kind": "TRADE_CLOSED",
                 "t0": (NOW + timedelta(hours=2)).isoformat(),
                 "context": {"volatility_state": {"state": "expansion"}}})
    outs = resolve(rows, lambda row, meter: row.get("context", {}).get(meter))
    assert outs[0].outcome == "ELAPSED"


def test_brier_only_over_scored_rows():
    assert brier([]) is None
    rows = _prediction_rows()
    rows.append({"context": {"volatility_state": {"state": "expansion"}},
                 "t0": (NOW + timedelta(minutes=10)).isoformat()})
    outs = resolve(rows, lambda row, meter: row.get("context", {}).get(meter))
    assert brier(outs) is not None
    assert 0 <= brier(outs) <= 1


# ---------------------------------------------------------------------------
# failure library (#6)
# ---------------------------------------------------------------------------

def _ledger_rows():
    return [
        {"kind": "SIGNAL", "t0": NOW.isoformat(), "decided_by": "MODEL",
         "reason": "breakout acceleration toward the level close",
         "decision": {"direction": "LONG", "setup_tag": "reclaim-sweep"},
         "outcome": {"r": -1.0}},
        {"kind": "SIGNAL", "t0": NOW.isoformat(), "decided_by": "MODEL",
         "reason": "breakout then flat round number on the picture",
         "decision": {"direction": "SHORT", "setup_tag": "reclaim-sweep"},
         "outcome": {"r": -0.6}},
        {"kind": "REFUSAL_MODEL", "t0": NOW.isoformat(), "decided_by": "MODEL",
         "reason": "refused — thin, no mechanism",
         "decision": {"direction": "LONG", "setup_tag": "sweep"},
         "outcome": {"r": 2.4}},
    ]


def test_clusters_breakout_and_no_trade():
    by_name = {c.archetype: c for c in cluster_rows(_ledger_rows())}
    assert "missed_breakout_acceleration" in by_name
    assert by_name["missed_breakout_acceleration"].count == 2
    assert abs(by_name["missed_breakout_acceleration"].total_r - (-1.6)) < 1e-9
    assert "no_trade_before_plus_2r" in by_name
    assert by_name["no_trade_before_plus_2r"].count == 1


def test_block_present_only_with_evidence():
    assert failure_memory_block([]) == ""
    block = failure_memory_block(_ledger_rows())
    assert "FAILURE MEMORY" in block
    assert "missed_breakout_acceleration" in block


# ---------------------------------------------------------------------------
# the compiler carries the new surfaces end-to-end
# ---------------------------------------------------------------------------

def _ctx(**over):
    base = dict(trend_direction="UP", trend_health="STRONG",
                trend_maturity="MID", volatility_state="NORMAL",
                htf_alignment="ALIGNED", displacement_state="NONE",
                sweep_state="NONE", reclaim_state="NONE",
                pullback_depth="NONE", distance_from_session_extreme="MID")
    base.update(over)
    return Context(**base)


def _full_brief():
    return MarketBrief(
        symbol="XAUUSD", as_of_utc=NOW, session="LONDON",
        bid=3300.0, ask=3300.2, spread=0.2, tick_age_s=1.0, atr=20.0,
        context=_ctx(displacement_state="CONFIRMED"),
        levels=(Level("L1", LevelKind.SWING_LOW, 3288.0, "M15", 5, True),
                Level("L2", LevelKind.SWING_HIGH, 3308.0, "M15", 8, True),
                Level("L3", LevelKind.SWING_HIGH, 3332.0, "M15", 12, True)),
        trigger_price=3300.0, trigger_utc=NOW)


def _actionable_read():
    return AnalystRead(
        action="ACTIONABLE", direction="LONG", setup=Setup.OTHER,
        setup_tag="breakout assert", mechanism_name="breakout-to-level",
        confidence=4, entry_ref="MARKET", stop_ref="L1", tp1_ref="L2",
        tp2_ref="L3", expected_holding_hours=2.0,
        novelty="MEDIUM",
        read="displacement pressing the confirmed level from below",
        why="buyers accepted the low and the displacement moved through the mid",
        why_not="standalone round number is not structure",
        invalidation="a close back under the low",
        adversarial=AdversarialReview(
            thesis="buyers accepted at the level and the displacement is real",
            counter_cases="the level was tested three times already; a third test "
                          "usually fails; standing volume is fading",
            missing="last 30s ticks", forced="breakout sellers",
            timing="now", monetization="over the level"),
        path=PathForecast(p_plus_half_r=0.75, p_plus_1r=0.60, p_plus_2r=0.35,
                          p_minus_1r_first=0.25, expected_mfe_r=1.8,
                          expected_mae_r=0.6, expected_r=0.9,
                          expected_holding_hours=2.0,
                          path_narrative="grind to TP1, stall, retest, TP2"),
        requests=["last 30s ticks"],
        expected_half_life_minutes=45.0,
        state_change=StateChangePrediction(
            current_state="normal range", transition="volatility expansion",
            meter_key="volatility_state", probability=0.8,
            expected_onset_minutes=15,
            evidence="atr compression over the last two hours",
            is_complete=False))


def test_compile_carries_new_surfaces():
    res = compile_signal(_full_brief(), _actionable_read(), Thresholds(),
                         CostModel(), None)
    assert res.direction == "LONG"
    assert res.entry and res.stop and res.tp2
    assert res.requests_answered == ("last 30s ticks",)
    assert res.half_life_minutes == 45.0
    assert res.expires_at_utc is not None
    assert res.expires_at_utc > NOW
    assert res.state_change is not None
    assert res.state_change.meter_key == "volatility_state"