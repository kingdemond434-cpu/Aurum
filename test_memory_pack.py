from datetime import datetime, timedelta, timezone

from golddesk.analyst import Context, MarketBrief
from golddesk.memory_pack import build_memory_pack

UTC = timezone.utc
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _brief():
    return MarketBrief(
        "XAUUSD", NOW, "LONDON", 2500, 2500.5, 0.5, 0, 5,
        Context("UP", "MODERATE", "MID", "NORMAL", "ALIGNED", "NONE",
                "NONE", "NONE", "SHALLOW", "MID"), ())


def _closed(hours_ago=24, realised=1.2, trend="UP", closed_after=False):
    entered = NOW - timedelta(hours=hours_ago)
    closed = NOW + timedelta(minutes=1) if closed_after else entered + timedelta(hours=2)
    return {
        "kind": "TRADE_CLOSED", "entry_t0": entered.isoformat(),
        "ts": closed.isoformat(), "direction": "LONG",
        "setup": "TREND_CONTINUATION", "mechanism_name": "pullback-resume",
        "realised_r": realised, "mfe_r": 1.4, "mae_r": -0.3, "reason": "STOP",
        "context": {"trend_direction": trend, "trend_health": "MODERATE",
                    "trend_maturity": "MID", "volatility_state": "NORMAL",
                    "htf_alignment": "ALIGNED", "session": "LONDON"}}


def test_pack_ranks_similar_prior_closed_decisions_and_summarises_outcomes():
    pack = build_memory_pack([_closed(realised=1.5),
                              _closed(hours_ago=48, realised=-1.0)], _brief())
    assert len(pack.analogues) == 2
    text = pack.render()
    assert "mean +0.25R" in text
    assert "wins 1/2" in text
    assert "compiler" in text.lower()


def test_pack_tells_entry_failure_from_management_giveback():
    failed = _closed(realised=-1.0)
    failed.update(mfe_r=0.12, mae_r=-1.0)
    giveback = _closed(hours_ago=48, realised=-0.1)
    giveback.update(mfe_r=2.2, mae_r=-0.2)
    text = build_memory_pack([failed, giveback], _brief()).render()
    assert "THESIS/TIMING FAILURE" in text
    assert "MANAGEMENT GIVEBACK" in text
    assert "Do not learn 'trade less'" in text


def test_open_or_future_resolved_cases_cannot_leak_into_the_pack():
    rows = [_closed(closed_after=True),
            {"kind": "SIGNAL", "outcome": {"returns_r": {"h4": 5.0}}}]
    assert build_memory_pack(rows, _brief()).analogues == ()


def test_one_shared_field_is_not_presented_as_a_market_analogue():
    row = _closed(trend="DOWN")
    row["context"] = {"trend_direction": "DOWN", "session": "LONDON"}
    assert build_memory_pack([row], _brief()).analogues == ()
