"""Chart character as numbers -- and proof it reaches the prompt.

The desk runs --numeric-only because the Claude Code CLI takes no image input, while
ANALYST_SYSTEM asks the model to read wick character, close position, compression and
impulsiveness off a chart. That is a reading it had no way to take. These tests pin the
arithmetic AND the wiring, because a feature that computes correctly and reaches no prompt is
the failure mode this repo has hit before.
"""
from __future__ import annotations

from dataclasses import dataclass

from golddesk.candle_character import LOOKBACK, MIN_BARS, block, measure


@dataclass
class B:
    open: float
    high: float
    low: float
    close: float


def flat(n: int, hi: float = 101.0, lo: float = 99.0) -> list[B]:
    """n identical ordinary bars: range 2.0, body 1.0, so mean body share is 0.5."""
    return [B(open=99.5, high=hi, low=lo, close=100.5) for _ in range(n)]


def test_too_few_bars_is_unmeasured_not_a_number():
    """Absence must stay distinguishable from a neutral reading."""
    assert measure(flat(MIN_BARS - 1)) == {}
    assert "UNMEASURED" in block(flat(MIN_BARS - 1))


def test_wick_shares_are_the_shares_of_one_range_and_sum_to_one():
    """Three ratios of the same denominator, so a reader can see where the bar spent itself."""
    bars = flat(LOOKBACK) + [B(open=100.0, high=110.0, low=90.0, close=102.0)]
    v = measure(bars)
    # range 20, body 2 -> 0.10; upper 110-102 = 8 -> 0.40; lower 100-90 = 10 -> 0.50
    assert v["body_pct"] == 0.10
    assert v["upper_wick_pct"] == 0.40
    assert v["lower_wick_pct"] == 0.50
    assert abs(v["body_pct"] + v["upper_wick_pct"] + v["lower_wick_pct"] - 1.0) < 1e-9


def test_close_position_reads_the_extremes_the_way_the_prompt_describes():
    """1.00 = closed on the high, 0.00 = on the low -- the exact wording in the block."""
    on_high = flat(LOOKBACK) + [B(open=95.0, high=105.0, low=95.0, close=105.0)]
    on_low = flat(LOOKBACK) + [B(open=105.0, high=105.0, low=95.0, close=95.0)]
    middle = flat(LOOKBACK) + [B(open=99.0, high=105.0, low=95.0, close=100.0)]
    assert measure(on_high)["close_position"] == 1.0
    assert measure(on_low)["close_position"] == 0.0
    assert measure(middle)["close_position"] == 0.5


def test_expansion_and_compression_are_measured_against_recent_range():
    """>1 expanding, <1 compressing. Prior bars all have range 2.0."""
    wide = flat(LOOKBACK) + [B(open=100.0, high=103.0, low=99.0, close=101.0)]   # range 4
    tight = flat(LOOKBACK) + [B(open=100.0, high=100.5, low=99.5, close=100.0)]  # range 1
    assert measure(wide)["range_vs_mean"] == 2.0
    assert measure(tight)["range_vs_mean"] == 0.5


def test_impulsiveness_is_normalised_to_how_this_market_has_been_behaving():
    """Body share against RECENT mean body share, not against a constant chosen here -- a
    market that has been grinding sets a different bar than one that has been trending."""
    # prior mean body share is 0.5; this bar is all body -> 1.0 / 0.5 = 2.0
    impulsive = flat(LOOKBACK) + [B(open=99.0, high=101.0, low=99.0, close=101.0)]
    assert measure(impulsive)["body_pct_vs_mean"] == 2.0


def test_a_zero_range_bar_is_unmeasured_rather_than_zero_percent():
    """A doji has an UNDEFINED body share, not a measured 0%. Reporting 0.0 would be a
    fabricated measurement -- the exact absence-as-clean-number defect this desk names."""
    doji = flat(LOOKBACK) + [B(open=100.0, high=100.0, low=100.0, close=100.0)]
    v = measure(doji)
    assert v["body_pct"] is None
    assert v["close_position"] is None
    assert "UNMEASURED" in block(doji)


def test_it_reads_only_the_bars_it_is_given():
    """Causality is the caller's half, but this must not reach past the end of its own input."""
    bars = flat(LOOKBACK) + [B(open=100.0, high=110.0, low=90.0, close=102.0)]
    before = measure(bars)
    after = measure(bars + [B(open=200.0, high=300.0, low=100.0, close=250.0)])
    assert before["body_pct"] == 0.10          # unchanged by what came later
    assert after["body_pct"] != before["body_pct"]


def test_the_block_actually_reaches_the_rendered_prompt():
    """THE WIRING, NOT JUST THE ARITHMETIC. brief_blocks.build has no production caller at all
    -- correct code reaching nothing -- so this asserts the path end to end: runner builds the
    block, MarketBrief carries it, render() emits it."""
    from datetime import datetime, timezone

    from golddesk.analyst import Context, Level, LevelKind, MarketBrief

    ctx = Context(trend_direction="UP", trend_health="MODERATE", trend_maturity="MID",
                  volatility_state="NORMAL", htf_alignment="ALIGNED",
                  displacement_state="CONFIRMED", sweep_state="CONFIRMED",
                  reclaim_state="CONFIRMED", pullback_depth="MEDIUM",
                  distance_from_session_extreme="MID")
    bars = flat(LOOKBACK) + [B(open=100.0, high=110.0, low=90.0, close=102.0)]
    brief = MarketBrief(
        symbol="XAUUSD", as_of_utc=datetime(2026, 8, 27, tzinfo=timezone.utc),
        session="LONDON", bid=100.0, ask=100.2, spread=0.2, tick_age_s=1.0, atr=4.0,
        context=ctx, levels=[Level("L1", LevelKind.SWING_LOW, 90.0, "M15", 3, True)],
        blocks=(block(bars),))
    rendered = brief.render()
    assert "CANDLE_CHARACTER" in rendered
    assert "CLOSE_POSITION" in rendered
    assert "0.10" in rendered          # the body_pct actually computed above


def test_runner_wires_the_block_into_every_brief_it_builds():
    """Source check: the live brief must carry it, or none of the above matters in production."""
    import inspect

    from golddesk import runner
    src = inspect.getsource(runner)
    assert "candle_character_block(bars[:i + 1])" in src, "causal slice, not the whole series"
    assert "blocks=blocks" in src, "computed but never passed to MarketBrief"
