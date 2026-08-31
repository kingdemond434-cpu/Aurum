"""The macro block must never render absence as neutral.

Every test here locks BOTH directions of that rule. A macro reader whose
only tests are happy-path is precisely how a stale vector starts reading as
a measured neutral backdrop -- the failure is silent, the prompt looks
normal, and every conditioned read inherits a world that no longer exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from golddesk.drivers_free import DriverPoint
from golddesk.macro_context import DEFAULT_MAX_AGE_H, MacroContext, from_drivers, load

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _write(tmp_path, updated, states):
    p = tmp_path / "macro_state.json"
    doc = {"states": states}
    if updated is not None:
        doc["updated"] = updated.isoformat()
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_missing_file_is_unusable_and_says_so(tmp_path):
    m = load(tmp_path / "nope.json", now=NOW)
    assert not m.usable
    assert "absent" in m.render()
    assert "UNMEASURED" in m.render()


def test_absence_is_never_rendered_as_neutral(tmp_path):
    """The whole point of the module: absent must not read as a measurement."""
    text = load(tmp_path / "nope.json", now=NOW).render()
    assert "Treat as ABSENT, not as neutral" in text
    # No numeric state may appear in an unmeasured block -- a rendered 0.000
    # is indistinguishable from a measured neutral reading.
    assert "+0.000" not in text


def test_fresh_state_is_usable_and_renders_values(tmp_path):
    p = _write(tmp_path, NOW - timedelta(hours=2),
               {"DOLLAR_STATE": 0.805, "RISK_STATE": -0.725})
    m = load(p, now=NOW)
    assert m.usable and not m.stale
    out = m.render()
    assert "+0.805" in out and "-0.725" in out
    assert "no vote on direction" in out


def test_expired_state_fails_closed(tmp_path):
    p = _write(tmp_path, NOW - timedelta(hours=DEFAULT_MAX_AGE_H + 1),
               {"DOLLAR_STATE": 0.805})
    m = load(p, now=NOW)
    assert m.stale and not m.usable
    assert "may have stopped" in m.detail
    assert "UNMEASURED" in m.render()
    # and the stale VALUE must not leak into the rendered block
    assert "0.805" not in m.render()


def test_get_returns_none_when_unusable_never_zero(tmp_path):
    p = _write(tmp_path, NOW - timedelta(hours=DEFAULT_MAX_AGE_H + 1),
               {"DOLLAR_STATE": 0.805})
    assert load(p, now=NOW).get("DOLLAR_STATE") is None


def test_missing_timestamp_is_unknown_not_fresh(tmp_path):
    """No timestamp means age UNKNOWN, which must not resolve to usable."""
    p = _write(tmp_path, None, {"DOLLAR_STATE": 0.805})
    m = load(p, now=NOW)
    assert not m.usable
    assert "UNKNOWN" in m.detail


def test_unparseable_file_is_unusable(tmp_path):
    p = tmp_path / "macro_state.json"
    p.write_text("{not json", encoding="utf-8")
    m = load(p, now=NOW)
    assert not m.usable and "unreadable" in m.detail


def test_real_rate_index_needs_both_inputs(tmp_path):
    p = _write(tmp_path, NOW, {"POLICY_RATE": 3.75})
    assert load(p, now=NOW).real_rate_index is None
    p2 = _write(tmp_path, NOW, {"POLICY_RATE": 3.75, "INFLATION_STATE": 1.136})
    assert load(p2, now=NOW).real_rate_index == pytest.approx(2.614)


def test_bool_state_is_not_read_as_a_number(tmp_path):
    """POLICY_PATH_HAWKISH is a bool; get() must not coerce it to 0.0/1.0."""
    p = _write(tmp_path, NOW, {"POLICY_PATH_HAWKISH": False})
    m = load(p, now=NOW)
    assert m.get("POLICY_PATH_HAWKISH") is None
    assert "False" in m.render()


def test_none_valued_series_renders_unmeasured(tmp_path):
    p = _write(tmp_path, NOW, {"GOLD_USD": None, "WTI": 86.48})
    out = load(p, now=NOW).render()
    assert "GOLD_USD" in out and "UNMEASURED" in out
    assert "+86.480" in out


def test_brief_without_macro_renders_unmeasured():
    """A MarketBrief built with no macro must say so, not stay silent.

    Silence would be the worst of the three options: the model cannot tell a
    missing section from one that was never going to be there.
    """
    from golddesk.analyst import Context, Level, LevelKind, MarketBrief
    ctx = Context(trend_direction="UP", trend_health="STRONG", trend_maturity="MID",
                  volatility_state="NORMAL", htf_alignment="ALIGNED",
                  displacement_state="NONE", sweep_state="NONE",
                  reclaim_state="NONE", pullback_depth="SHALLOW",
                  distance_from_session_extreme="MID")
    b = MarketBrief(symbol="XAUUSD", as_of_utc=NOW, session="LONDON",
                    bid=4400.0, ask=4400.5, spread=0.5, tick_age_s=1.0, atr=8.0,
                    context=ctx,
                    levels=[Level(id="L1", kind=LevelKind.SWING_LOW, price=4390.0,
                                  timeframe="H1", bars_ago=5, confirmed=True)])
    out = b.render()
    assert "MACRO CONTEXT: UNMEASURED" in out
    assert "Treat as ABSENT, not as neutral" in out


def test_brief_with_macro_renders_it_after_structure():
    """Macro must follow structure -- leading with it invites a top-down
    narrative that then hunts for confirming structure."""
    from golddesk.analyst import Context, Level, LevelKind, MarketBrief
    ctx = Context(trend_direction="UP", trend_health="STRONG", trend_maturity="MID",
                  volatility_state="NORMAL", htf_alignment="ALIGNED",
                  displacement_state="NONE", sweep_state="NONE",
                  reclaim_state="NONE", pullback_depth="SHALLOW",
                  distance_from_session_extreme="MID")
    macro = MacroContext(updated=NOW, age_hours=1.0, detail="1.0h old",
                         states={"DOLLAR_STATE": 0.805})
    b = MarketBrief(symbol="XAUUSD", as_of_utc=NOW, session="LONDON",
                    bid=4400.0, ask=4400.5, spread=0.5, tick_age_s=1.0, atr=8.0,
                    context=ctx,
                    levels=[Level(id="L1", kind=LevelKind.SWING_LOW, price=4390.0,
                                  timeframe="H1", bars_ago=5, confirmed=True)],
                    macro=macro)
    out = b.render()
    assert out.index("MEASURED CONTEXT") < out.index("MACRO CONTEXT")
    assert "+0.805" in out


def test_one_stale_driver_does_not_blank_fresh_cross_market_evidence():
    points = {
        "dxy": DriverPoint("dxy", 0.42, 100.0, NOW - timedelta(minutes=3),
                           "MT5 EURUSD inverse", exact=False),
        "spx": DriverPoint("spx", -0.31, 6500.0, NOW - timedelta(minutes=2),
                           "MT5 US500", exact=False),
        "real_yield_10y": DriverPoint(
            "real_yield_10y", 0.18, 1.8,
            NOW - timedelta(hours=DEFAULT_MAX_AGE_H + 12), "FRED DFII10"),
    }

    macro = from_drivers(points, now=NOW)

    assert macro.usable
    assert macro.get("dxy") == pytest.approx(0.42)
    assert macro.get("spx") == pytest.approx(-0.31)
    assert macro.get("real_yield_10y") is None
    rendered = macro.render()
    assert "real_yield_10y" in rendered and "STALE" in rendered
    assert "MACRO CONTEXT: UNMEASURED" not in rendered


def test_all_stale_drivers_still_fail_closed():
    points = {
        "dxy": DriverPoint("dxy", 0.42, 100.0,
                           NOW - timedelta(hours=DEFAULT_MAX_AGE_H + 1), "Yahoo")
    }
    macro = from_drivers(points, now=NOW)
    assert not macro.usable
    assert "UNMEASURED" in macro.render()
