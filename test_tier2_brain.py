"""Tier-2 brain modules: the multi-timeframe veto, measured seasonality, the missed-move ledger.

The load-bearing tests here are the ones that pin REFUSALS. Each module has a failure mode where
it would quietly produce a confident answer from nothing — an unavailable timeframe read as
consent, a seasonal bias from four observations, a missed move that only existed between two
ticks. Those are the tests that matter; the happy paths are almost incidental.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from golddesk.hierarchical_bias import (Alignment, TimeframeRead, assess)
from golddesk.missed_move import MissedReport, scan
from golddesk.seasonality import MIN_YEARS, T_NEUTRAL, measure, monthly_returns, to_prompt

UTC = timezone.utc


# ------------------------------------------------------------------ fixtures

@dataclass
class FakeState:
    trend_direction: str = "NONE"
    trend_health: str = "MODERATE"
    trend_maturity: str = "MID"
    displacement_state: str = "NONE"


@dataclass
class FakeBar:
    ts: datetime
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0


def tf(label: str, direction="NONE", disp="NONE", maturity="MID") -> TimeframeRead:
    return TimeframeRead(label, FakeState(trend_direction=direction,
                                          displacement_state=disp,
                                          trend_maturity=maturity))


# ------------------------------------------------------------------ the veto

class TestTheVetoOnlyFiresOnConfirmedOpposition:
    def test_buy_against_a_confirmed_down_displacement_is_refused(self):
        a = assess("BUY", [tf("entry", "UP"), tf("H4", "DOWN", disp="CONFIRMED")])
        assert a.alignment is Alignment.COUNTER_HARD and a.vetoed

    def test_buy_against_a_mere_downtrend_is_allowed_and_flagged(self):
        a = assess("BUY", [tf("entry", "UP"), tf("H4", "DOWN")])
        assert a.alignment is Alignment.COUNTER_SOFT
        assert not a.vetoed, (
            "mean reversion into a trend is a legitimate trade; a rule that forbade it would "
            "delete a whole family of setups the desk is supposed to take")

    def test_a_FORMING_displacement_does_not_veto(self):
        # Vetoing on candidates refuses continuation entries when they are cheapest.
        a = assess("BUY", [tf("H4", "DOWN", disp="FORMING")])
        assert a.alignment is Alignment.COUNTER_SOFT

    def test_an_EXHAUSTED_opposing_trend_downgrades_the_veto(self):
        """Opposing a young strong trend and an exhausted one are opposite bets. The exhausted
        case IS the reversal trade — the module must not refuse the thing it exists to permit."""
        a = assess("SELL", [tf("H4", "UP", disp="CONFIRMED", maturity="EXHAUSTED")])
        assert a.alignment is Alignment.COUNTER_SOFT
        assert "EXHAUSTED" in a.why and "reversal" in a.why


class TestAbsenceIsNeverConsent:
    def test_an_unavailable_timeframe_contributes_nothing(self):
        a = assess("BUY", [TimeframeRead("H4", None), TimeframeRead("D1", None)])
        assert a.alignment is Alignment.NEUTRAL
        assert "nothing to fight" in a.why

    def test_an_unavailable_timeframe_is_not_counted_as_agreement(self):
        """The dangerous reading: absence rendered as ALIGNED. A veto that stops vetoing during
        low-data conditions has stopped vetoing exactly when it matters most."""
        a = assess("BUY", [tf("entry", "UP"), TimeframeRead("H4", None)])
        assert a.alignment is Alignment.ALIGNED
        assert "H4" not in a.why, "an absent timeframe must not appear in the list of agreers"

    def test_a_NONE_trend_is_not_opposition(self):
        a = assess("BUY", [tf("H4", "NONE", disp="CONFIRMED")])
        assert a.alignment is Alignment.NEUTRAL

    def test_the_prompt_says_UNAVAILABLE_rather_than_omitting_the_row(self):
        a = assess("BUY", [TimeframeRead("D1", None)])
        assert "UNAVAILABLE" in a.to_prompt()


# ------------------------------------------------------------------ seasonality

def _month_series(per_month_returns: dict[int, list[float]]) -> list[FakeBar]:
    """Synthesise bars whose month-open→month-close return is exactly as specified."""
    bars, base = [], 1000.0
    for m, rets in sorted(per_month_returns.items()):
        for k, r in enumerate(rets):
            y = 2000 + k
            bars.append(FakeBar(datetime(y, m, 1, tzinfo=UTC), close=base))
            bars.append(FakeBar(datetime(y, m, 27, tzinfo=UTC), close=base * (1 + r)))
    return sorted(bars, key=lambda b: b.ts)


class TestSeasonalityRefusesWhatItCannotSupport:
    def test_a_month_with_too_few_years_is_UNMEASURED_not_neutral(self):
        stats = measure(_month_series({3: [0.05] * 4}))
        march = stats[2]
        assert march.verdict == "UNMEASURED", (
            "four observations cannot carry a directional verdict in either direction")
        assert "standard error" in march.why

    def test_UNMEASURED_is_reported_not_silently_skipped(self):
        stats = measure(_month_series({3: [0.05] * 4}))
        p = to_prompt(stats, now=datetime(2026, 3, 15, tzinfo=UTC))
        assert "UNMEASURED" in p

    def test_a_real_but_insignificant_mean_reads_NEUTRAL(self):
        # Noisy positive mean, enough years, no significance -> NEUTRAL is the honest answer.
        rets = [0.05, -0.04, 0.06, -0.05, 0.04, -0.03, 0.05, -0.06, 0.02]
        stats = measure(_month_series({6: rets}))
        assert stats[5].verdict == "NEUTRAL" and abs(stats[5].t_stat) < T_NEUTRAL

    def test_a_consistent_strong_month_is_detected(self):
        stats = measure(_month_series({12: [0.03] * MIN_YEARS}))
        assert stats[11].verdict == "BULLISH" and stats[11].win_rate == 1.0

    def test_every_significant_verdict_carries_its_multiplicity_warning(self):
        """Twelve months were examined. At t=2.0 roughly one in twenty clears by chance, so
        seeing one or two 'significant' months is what noise looks like. The text must say so."""
        stats = measure(_month_series({12: [0.03] * MIN_YEARS}))
        assert "uncorrected" in stats[11].why and "twelve" in stats[11].why

    def test_monthly_returns_use_first_and_last_close_in_the_month(self):
        bars = [FakeBar(datetime(2020, 5, 1, tzinfo=UTC), close=100.0),
                FakeBar(datetime(2020, 5, 9, tzinfo=UTC), close=900.0),
                FakeBar(datetime(2020, 5, 30, tzinfo=UTC), close=110.0)]
        assert monthly_returns(bars)[(2020, 5)] == pytest.approx(0.10), (
            "the mid-month spike is not the month's return; a position held the month earned 10%")


# ------------------------------------------------------------------ missed moves

def _flat(n: int, price: float = 2000.0) -> list[FakeBar]:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    return [FakeBar(t0 + timedelta(hours=i), high=price, low=price, close=price)
            for i in range(n)]


class TestMissedMovesAreFactsNotRegrets:
    def test_a_clean_untraded_run_is_recorded(self):
        bars = _flat(10)
        for i in range(5, 10):                       # a 5-unit run on a 1.0 ATR
            bars[i] = FakeBar(bars[i].ts, high=2005.0, low=2000.0, close=2005.0)
        r = scan(bars, [1.0] * 10, [])
        assert r.state == "MEASURED" and r.missed >= 1 and r.largest_r >= 2.0

    def test_a_signal_anywhere_inside_the_window_disqualifies_it(self):
        bars = _flat(10)
        for i in range(5, 10):
            bars[i] = FakeBar(bars[i].ts, high=2005.0, low=2000.0, close=2005.0)
        r = scan(bars, [1.0] * 10, [bars[6].ts])
        assert r.missed == 0, "the desk was engaged; that is not a miss"

    def test_a_wrong_direction_signal_also_disqualifies(self):
        """A mis-read is attribution.py's business. Counting it here too would double-charge one
        failure and inflate the ledger."""
        bars = _flat(10)
        for i in range(5, 10):
            bars[i] = FakeBar(bars[i].ts, high=2005.0, low=2000.0, close=2005.0)
        assert scan(bars, [1.0] * 10, [bars[5].ts]).missed == 0

    def test_the_threshold_is_in_R_so_volatility_rescales_it(self):
        bars = _flat(10)
        for i in range(5, 10):
            bars[i] = FakeBar(bars[i].ts, high=2005.0, low=2000.0, close=2005.0)
        calm = scan(bars, [1.0] * 10, [])
        wild = scan(bars, [10.0] * 10, [])
        assert calm.missed >= 1 and wild.missed == 0, (
            "five dollars is a rout at ATR 1 and noise at ATR 10 — a price threshold would "
            "call both the same")

    def test_overlapping_runs_collapse_to_one(self):
        bars = _flat(60)
        for i in range(1, 60):                       # one long clean trend
            p = 2000.0 + i
            bars[i] = FakeBar(bars[i].ts, high=p, low=p - 1, close=p)
        r = scan(bars, [1.0] * 60, [])
        assert r.missed <= 3, (
            f"one trend reported as {r.missed} separate misses — without de-overlapping the "
            "ledger becomes noise")

    def test_a_quiet_market_misses_nothing(self):
        r = scan(_flat(50), [1.0] * 50, [])
        assert r.missed == 0 and r.total_missed_r == 0.0

    def test_warmup_bars_with_no_ATR_are_skipped_not_treated_as_zero(self):
        # ATR None must never divide into an enormous R.
        bars = _flat(10)
        bars[5] = FakeBar(bars[5].ts, high=9999.0, low=2000.0, close=2000.0)
        r = scan(bars, [None] * 10, [])
        assert r.missed == 0

    def test_a_mismatched_ATR_series_is_UNMEASURED(self):
        r = scan(_flat(10), [1.0] * 3, [])
        assert r.state == "UNMEASURED" and "matching ATR" in r.why

    def test_signal_timestamps_that_match_no_bar_are_reported_not_ignored(self):
        """If the signal log and the bar series are on different clocks, every signal misses and
        the report silently claims the desk traded nothing. It has to say so."""
        bars = _flat(10)
        for i in range(5, 10):
            bars[i] = FakeBar(bars[i].ts, high=2005.0, low=2000.0, close=2005.0)
        r = scan(bars, [1.0] * 10, [datetime(1999, 1, 1, tzinfo=UTC)])
        assert "matched no bar" in r.why and "overstating" in r.why

    def test_the_prompt_reports_UNMEASURED_state_rather_than_zero_misses(self):
        r = MissedReport(0, 0, 0, 0.0, 0.0, 2.0, 48, "UNMEASURED", "no bars")
        assert "UNMEASURED" in r.to_prompt()


# ------------------------------------------------------------------ supply side

from golddesk.supply_side import (SUPPORT_ATR_MULTIPLE, calendar_flags,  # noqa: E402
                                  floor_context, to_prompt as supply_prompt)


class TestTheCostFloorRefusesToPretendItIsSupport:
    def test_todays_reality_is_NOT_actionable(self):
        """Gold ~$3,300, aggregate AISC ~$1,400, ATR ~$20. The floor is ~95 ATR away. Rendering
        that as support would put a confident falsehood in every prompt for years."""
        f = floor_context(spot=3300.0, atr=20.0, aisc=1400.0)
        assert f.state == "MEASURED" and not f.actionable
        assert f.distance_pct == pytest.approx(0.576, abs=0.01)
        assert "cannot interact" in f.why and "NOT ACTIONABLE" in f.to_prompt()

    def test_it_becomes_actionable_only_when_genuinely_close(self):
        f = floor_context(spot=1450.0, atr=20.0, aisc=1400.0)
        assert f.actionable and f.distance_atr == pytest.approx(2.5)
        assert "supply destruction" in f.to_prompt()

    def test_absent_AISC_is_UNMEASURED_not_no_floor(self):
        f = floor_context(spot=3300.0, atr=20.0, aisc=None)
        assert f.state == "UNMEASURED" and not f.actionable
        assert "not 'no floor'" in f.why

    def test_absent_ATR_refuses_rather_than_guessing_the_unit(self):
        assert floor_context(spot=3300.0, atr=0.0, aisc=1400.0).state == "UNMEASURED"

    def test_the_threshold_is_in_ATR_not_dollars_or_percent(self):
        # Same dollar gap, different volatility -> different verdict.
        near = floor_context(spot=1500.0, atr=50.0, aisc=1400.0)
        far = floor_context(spot=1500.0, atr=1.0, aisc=1400.0)
        assert near.actionable and not far.actionable


class TestTheCalendarNeedsNoFeed:
    def test_a_quarter_end_is_flagged_within_the_window(self):
        flags = calendar_flags(now=datetime(2026, 3, 30, tzinfo=UTC))
        assert any(f.kind == "QUARTER_END" and f.days_away == 1 for f in flags)

    def test_mid_quarter_is_quiet(self):
        assert calendar_flags(now=datetime(2026, 2, 10, tzinfo=UTC)) == []

    def test_it_works_for_dates_years_ahead_with_no_fetch(self):
        """The whole point: these are computable in advance. Nothing to fetch, nothing to go
        stale, no vendor — unlike every feed in the physical-pulse family."""
        assert calendar_flags(now=datetime(2031, 6, 29, tzinfo=UTC))

    def test_it_spans_a_year_boundary(self):
        flags = calendar_flags(now=datetime(2027, 1, 2, tzinfo=UTC))
        assert any(f.day == date(2026, 12, 31) for f in flags)

    def test_the_prompt_says_so_when_nothing_is_near(self):
        p = supply_prompt(floor_context(3300.0, 20.0, None),
                          calendar_flags(now=datetime(2026, 2, 10, tzinfo=UTC)))
        assert "No dated supply/demand effect" in p


# ------------------------------------------------------------------ wiring into the prompt

from pathlib import Path                                          # noqa: E402

from golddesk.analyst import AnalystRead, MarketBrief, Setup, compile_signal  # noqa: E402
from golddesk.brief_blocks import build as build_blocks           # noqa: E402
from golddesk.brief_blocks import seasonality_block, timeframe_block  # noqa: E402


class TestBlocksReachThePrompt:
    def test_blocks_render_verbatim_in_the_brief(self):
        b = MarketBrief(symbol="XAUUSD", as_of_utc=datetime(2026, 8, 20, tzinfo=UTC),
                        session="LONDON", bid=3300.0, ask=3300.3, spread=0.3,
                        tick_age_s=1.0, atr=20.0, context=_ctx(), levels=(),
                        blocks=("[SEASONALITY]\n  x\n[/SEASONALITY]",))
        assert "[SEASONALITY]" in b.render()

    def test_a_brief_with_no_blocks_still_renders(self):
        b = MarketBrief(symbol="XAUUSD", as_of_utc=datetime(2026, 8, 20, tzinfo=UTC),
                        session="LONDON", bid=3300.0, ask=3300.3, spread=0.3,
                        tick_age_s=1.0, atr=20.0, context=_ctx(), levels=())
        assert "SYMBOL XAUUSD" in b.render()

    def test_build_returns_widest_context_first(self):
        blocks = build_blocks(spot=3300.0, atr=20.0,
                              now=datetime(2026, 12, 15, tzinfo=UTC))
        assert "[SEASONALITY" in blocks[0] and "SUPPLY" in blocks[1] and "BIAS" in blocks[2]

    def test_the_measured_december_table_reaches_the_prompt(self):
        """End to end on the shipped artifact: December measured BULLISH at t=+3.71, and the
        hardcoded table it replaced called that month neutral."""
        blk = seasonality_block(now=datetime(2026, 12, 15, tzinfo=UTC))
        assert "December" in blk and "BULLISH" in blk and "n=8" in blk

    def test_a_missing_seasonality_table_says_so_rather_than_going_quiet(self):
        blk = seasonality_block(now=datetime(2026, 12, 15, tzinfo=UTC),
                                path=Path("/nonexistent/seasonality.json"))
        assert "UNAVAILABLE" in blk and "not 'no seasonal effect'" in blk

    def test_absent_timeframe_reads_are_NOT_COMPUTED_not_aligned(self):
        assert "NOT COMPUTED" in timeframe_block(())
        assert "nothing was checked" in timeframe_block(())


def _ctx():
    from golddesk.analyst import Context
    import inspect
    kw = {}
    for name, p in inspect.signature(Context).parameters.items():
        if p.default is inspect.Parameter.empty:
            ann = str(p.annotation)
            kw[name] = 0.0 if "float" in ann else ("NONE" if "Literal" in ann or "str" in ann
                                                   else None)
    return Context(**kw)


class TestTheVetoIsEnforcedNotAdvised:
    def _brief(self):
        return MarketBrief(symbol="XAUUSD", as_of_utc=datetime(2026, 8, 20, tzinfo=UTC),
                           session="LONDON", bid=3300.0, ask=3300.3, spread=0.3,
                           tick_age_s=1.0, atr=20.0, context=_ctx(), levels=())

    def _read(self, direction="LONG"):
        return AnalystRead(setup=Setup.TREND_CONTINUATION, direction=direction,
                           entry_ref="MARKET", stop_ref="L1", tp1_ref="L2", tp2_ref="L3", mechanism_name="test",
                           confidence=4, read="r", why="w", why_not="wn", invalidation="inv")

    def test_a_hard_counter_refuses_before_any_level_is_resolved(self):
        out = compile_signal(self._brief(), self._read("LONG"),
                             tf_reads=[tf("H4", "DOWN", disp="CONFIRMED")])
        assert out.__class__.__name__ == "Refusal"
        assert "hierarchical bias" in out.reason and out.vetoed_by_compiler

    def test_a_soft_counter_does_not_refuse_on_bias_grounds(self):
        out = compile_signal(self._brief(), self._read("LONG"), tf_reads=[tf("H4", "DOWN")])
        assert "hierarchical bias" not in getattr(out, "reason", "")

    def test_no_reads_supplied_means_no_veto_gained_silently(self):
        """An un-wired caller must keep today's behaviour. Empty reads are 'nothing was checked',
        never 'aligned' — so nothing is vetoed and nothing is waved through on false grounds."""
        out = compile_signal(self._brief(), self._read("LONG"))
        assert "hierarchical bias" not in getattr(out, "reason", "")

    def test_the_veto_follows_the_direction_the_analyst_actually_proposed(self):
        reads = [tf("H4", "DOWN", disp="CONFIRMED")]
        assert "hierarchical bias" in compile_signal(
            self._brief(), self._read("LONG"), tf_reads=reads).reason
        assert "hierarchical bias" not in getattr(
            compile_signal(self._brief(), self._read("SHORT"), tf_reads=reads), "reason", "")
