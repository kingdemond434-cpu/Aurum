from golddesk.lessons import build_lessons


def _row(i, direction, session, win):
    return {
        "kind": "SIGNAL",
        "context": {"session": session},
        "decision": {
            "direction": direction,
            "analyst_read": {
                "mechanism_name": f"fragment-{i}",
                "confidence": 3,
                "novelty": "LOW",
            },
        },
        "outcome": {
            "mfe_r": 2.1 if win else 0.1,
            "mae_r": -0.1 if win else -1.1,
            "time_to_mfe_s": 10 if win else 20,
            "time_to_mae_s": 20 if win else 10,
        },
    }


def test_lessons_expose_direction_session_skew_and_name_fragmentation():
    rows = ([_row(i, "LONG", "ASIA", False) for i in range(6)]
            + [_row(i + 6, "SHORT", "LONDON", True) for i in range(6)])
    text = build_lessons(rows)
    assert "LONG -1.00R/6" in text
    assert "SHORT +2.00R/6" in text
    assert "ASIA -1.00R/6" in text
    assert "fragmented: 12 names across 12 resolved signals" in text


def test_live_signal_learning_prefers_actual_close_over_embedded_bar_outcome():
    signals = [_row(i, "LONG", "ASIA", True) for i in range(5)]
    for i, row in enumerate(signals):
        row["t0"] = f"2026-09-01T00:0{i}:00+00:00"
    closes = [{"kind": "TRADE_CLOSED", "entry_t0": r["t0"],
               "realised_r": -1.0} for r in signals]
    text = build_lessons(signals + closes)
    assert "LONG -1.00R/5" in text
    assert "LONG +2.00R/5" not in text
