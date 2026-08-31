"""Contract tests for the mechanism-first analyst rewrite (P0).

Covers exactly the surfaces P0 promised and nothing else:
  - the NO_TRADE boundary (what counts as a refusal, and what no longer does)
  - post-hoc family classification
  - the deterministic baseline reading & compiling under the strict gate
  - the adversarial/path gate, including its ordering AFTER the bias veto
  - visual-region resolution (prices dropped, names resolved)
  - novelty as an uncertainty surcharge on the cold-start prior only
"""

from datetime import datetime, timezone

from golddesk.analyst import (AdversarialReview, AnalystRead, Context, Level,
                              LevelKind, MarketBrief, PathForecast, Refusal,
                              Setup, Thresholds, VisualRegion, classify_family,
                              compile_signal, resolve_region)
from golddesk.opportunity import CohortStat, ev_gate

UTC = timezone.utc


def _ctx(**over):
    base = dict(trend_direction="UP", trend_health="STRONG", trend_maturity="MID",
                volatility_state="NORMAL", htf_alignment="ALIGNED",
                displacement_state="NONE", sweep_state="NONE", reclaim_state="NONE",
                pullback_depth="NONE", distance_from_session_extreme="MID")
    base.update(over)
    return Context(**base)


_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _brief(levels=(), trigger_price=None, **ctx):
    over = dict(displacement_state="NONE", trend_direction="UP",
                pullback_depth="NONE")
    over.update(ctx)
    return MarketBrief(symbol="XAUUSD", as_of_utc=_NOW, session="LONDON",
                       bid=3300.0, ask=3300.3, spread=0.3, tick_age_s=1.0,
                       atr=20.0, context=_ctx(**over), levels=levels,
                       trigger_price=trigger_price,
                       trigger_utc=_NOW if trigger_price is not None else None)


def _simple_brief():
    """Layout used by the visual-region tests: two confirmed swing levels."""
    return _brief((Level("L1", LevelKind.SWING_HIGH, 3312.0, "M15", 5, True),
                   Level("L2", LevelKind.SWING_LOW, 3288.0, "M15", 8, True)),
                  displacement_state="CONFIRMED", pullback_depth="SHALLOW")


def _strong_brief():
    """A LONG displacement geometry that clears every economic gate: R:R well
    above the cold-start prior, drift from the trigger inside the limit, spread
    a small fraction of a wide-ish stop. The ONLY thing left to refuse this
    read is the adversarial/path gate the tests are about."""
    return _brief((Level("L1", LevelKind.SWING_HIGH, 3340.0, "M15", 5, True),
                   Level("L2", LevelKind.SWING_LOW, 3296.0, "M15", 8, True)),
                  displacement_state="CONFIRMED", pullback_depth="SHALLOW",
                  trend_direction="UP", trigger_price=3299.0)


def tf(label, direction="NONE", disp="NONE", maturity="MID"):
    from golddesk.hierarchical_bias import TimeframeRead
    from dataclasses import dataclass

    @dataclass
    class _State:
        trend_direction: str = "NONE"
        trend_health: str = "MODERATE"
        trend_maturity: str = "MID"
        displacement_state: str = "NONE"

    return TimeframeRead(label, _State(trend_direction=direction,
                                       displacement_state=disp,
                                       trend_maturity=maturity))


def _path():
    return PathForecast(p_plus_half_r=0.65, p_plus_1r=0.5, p_plus_2r=0.3,
                        p_minus_1r_first=0.4, expected_mfe_r=1.8, expected_mae_r=0.7,
                        expected_r=1.0, expected_holding_hours=6.0,
                        path_narrative="shape relies on the displacement origin")


def _adv():
    return AdversarialReview(thesis="t", counter_cases="c", missing="m",
                             forced="f", timing="ti", monetization="mo")


def _read(**over):
    base = dict(direction="LONG", entry_ref="MARKET", stop_ref="L2",
                tp1_ref="NONE", tp2_ref="L1", mechanism_name="test-mech",
                confidence=3, read="r", why="w", why_not="wn", invalidation="inv")
    base.update(over)
    return AnalystRead(**base)


# ---------------------------------------------------------------------------
# 1. The NO_TRADE boundary
# ---------------------------------------------------------------------------

class TestNoTradeBoundary:
    def test_an_actionable_read_is_not_a_refusal(self):
        assert not _read(setup=Setup.OTHER, direction="LONG").is_no_trade()

    def test_action_no_trade_refuses_even_with_a_family_and_direction(self):
        # setup must be IRRELEVANT to the refusal test: a model that says
        # NO_TRADE while filing it under a family is still a refusal.
        assert _read(action="NO_TRADE", setup=Setup.SWING_REVERSAL,
                     direction="LONG").is_no_trade()

    def test_legacy_direction_none_refuses(self):
        assert _read(direction="NONE", setup=Setup.NO_SETUP,
                     action="NO_TRADE").is_no_trade()

    def test_a_new_read_omitting_setup_is_not_a_refusal(self):
        # The compatibility-critical case: the old contract made NO_SETUP a
        # refusal flag; the new contract must not, or every ACTIONABLE read
        # that omits the analytics tag would be silently killed.
        assert not _read(setup=Setup.OTHER, direction="LONG",
                         action="ACTIONABLE").is_no_trade()


# ---------------------------------------------------------------------------
# 2. Post-hoc family classification
# ---------------------------------------------------------------------------

class TestFamilyClassification:
    def test_reversal_keywords(self):
        for tag in ("sweep", "reclaim", "reversal", "trap", "engulf"):
            assert classify_family(_read(mechanism_name=f"asia-{tag}-long")) \
                is Setup.SWING_REVERSAL

    def test_continuation_keywords(self):
        for tag in ("continuation", "displacement", "breakout", "retest",
                    "pullback", "resume", "follow-through"):
            assert classify_family(_read(mechanism_name=tag)) \
                is Setup.TREND_CONTINUATION

    def test_the_mechanism_and_why_text_both_count(self):
        assert classify_family(_read(mechanism_name="odd-notion",
                                     why="sweep of the low")) \
            is Setup.SWING_REVERSAL

    def test_an_explicit_family_is_honoured(self):
        assert classify_family(_read(setup=Setup.SWING_REVERSAL,
                                     mechanism_name="displacement-continue")) \
            is Setup.SWING_REVERSAL

    def test_novel_mechanism_lands_in_other_with_a_real_cohort_key(self):
        assert classify_family(_read(mechanism_name="liquidity-pong",
                                     setup=Setup.OTHER)) is Setup.OTHER


# ---------------------------------------------------------------------------
# 3. The deterministic baseline survives the strict gate
# ---------------------------------------------------------------------------

class TestDeterministicBaseline:
    def test_displacement_read_compiles_without_refusal(self):
        from golddesk.runner import DeterministicAnalyst
        brief = _strong_brief()
        read = DeterministicAnalyst().read(brief)
        assert read.action == "ACTIONABLE"
        assert read.setup_tag and read.mechanism_name == "displacement-continuation"
        assert read.adversarial is not None and read.adversarial.is_complete()
        assert read.path is not None and read.path.is_complete()
        sig = compile_signal(brief, read)
        assert not isinstance(sig, Refusal)
        assert sig.path is read.path and sig.adversarial is read.adversarial

    def test_a_brief_without_levels_is_a_no_trade(self):
        from golddesk.runner import DeterministicAnalyst
        read = DeterministicAnalyst().read(_brief(levels=()))
        assert read.is_no_trade() and read.action == "NO_TRADE"


# ---------------------------------------------------------------------------
# 4. The adversarial/path gate — and its ordering
# ---------------------------------------------------------------------------

class TestStrictGate:
    def test_actionable_without_adversarial_is_refused(self):
        out = compile_signal(_strong_brief(), _read(tp2_ref="L1", path=_path()))
        assert isinstance(out, Refusal) and "adversarial case" in out.reason

    def test_actionable_without_path_is_refused(self):
        out = compile_signal(_strong_brief(), _read(tp2_ref="L1", adversarial=_adv()))
        assert isinstance(out, Refusal) and "path forecast" in out.reason

    def test_complete_read_passes_under_a_favourable_geometry(self):
        out = compile_signal(_strong_brief(),
                             _read(tp2_ref="L1", path=_path(), adversarial=_adv()))
        assert not isinstance(out, Refusal)
        assert out.path is not None and out.adversarial is not None

    def test_the_bias_veto_still_fires_before_the_gate(self):
        # Ordering is contractual: a hierarchical-bias contention must veto even
        # a read that omitted its adversarial case — the veto is the earlier,
        # cheaper evidence of a wrong trade.
        out = compile_signal(_strong_brief(), _read(tp2_ref="L1"),
                             tf_reads=[tf("H4", "DOWN", disp="CONFIRMED")])
        assert isinstance(out, Refusal)
        assert "hierarchical bias" in out.reason

    def test_market_intent_cannot_enter_a_conditional_level_immediately(self):
        out = compile_signal(
            _strong_brief(),
            _read(entry_ref="L2", entry_intent="MARKET", tp2_ref="L1",
                  path=_path(), adversarial=_adv()))
        assert isinstance(out, Refusal)
        assert "MARKET intent requires entry_ref MARKET" in out.reason

    def test_break_intent_waits_for_a_closed_break(self):
        from dataclasses import replace
        brief = replace(_strong_brief(), bar_close=3300.0)
        out = compile_signal(
            brief,
            _read(entry_ref="L1", entry_intent="BREAK", tp2_ref="L1",
                  path=_path(), adversarial=_adv()))
        assert isinstance(out, Refusal)
        assert "BREAK not confirmed" in out.reason


# ---------------------------------------------------------------------------
# 5. Visual-region resolution
# ---------------------------------------------------------------------------

class TestVisualRegions:
    def test_current_price_resolves_to_the_live_quote(self):
        brief = _simple_brief()
        assert resolve_region(brief, VisualRegion(
            concept="demand band", band="AT",
            reference_ref="CURRENT_PRICE")) == f"demand band AT price@{brief.mid:.2f}"

    def test_nearest_resolves_to_the_nearest_confirmed_level(self):
        # L1 at 3312 is 11.85 away from 3300.15; L2 at 3288 is 12.15.
        zone = resolve_region(_simple_brief(), VisualRegion(
            concept="previous demand", band="AT", reference_ref="NEAREST"))
        assert zone == "previous demand AT L1@3312.00"

    def test_an_explicit_level_ref_resolves_by_id(self):
        assert resolve_region(_simple_brief(), VisualRegion(
            concept="the supply shelf", band="ABOVE",
            reference_ref="L1")) == "the supply shelf ABOVE L1@3312.00"

    def test_a_region_carrying_a_price_is_dropped_not_resolved(self):
        assert resolve_region(_simple_brief(), VisualRegion(
            concept="the 3305.25 shelf", band="ABOVE",
            reference_ref="NEAREST")) is None

    def test_unknown_reference_is_dropped(self):
        assert resolve_region(_simple_brief(), VisualRegion(
            concept="ghost", band="AT", reference_ref="NOPE")) is None


# ---------------------------------------------------------------------------
# 6. Novelty = uncertainty, on the cold-start prior only
# ---------------------------------------------------------------------------

class TestNoveltyUncertainty:
    def _verb(self, novelty, rr):
        return ev_gate(rr, 0.0, "unseen-mech", None,
                       fallback_min_rr=2.0, novelty_level=novelty)

    def test_low_novelty_is_the_plain_prior(self):
        assert not self._verb("LOW", 1.5).take
        assert self._verb("LOW", 2.0).take
        assert "novelty" not in self._verb("LOW", 2.0).reason

    def test_high_novelty_widens_the_prior_until_mechanism_has_history(self):
        mid = self._verb("HIGH", 2.2)
        assert not mid.take and "HIGH novelty widened prior to 2.50" in mid.reason
        assert self._verb("HIGH", 2.5).take

    def test_medium_novelty_is_a_ten_percent_wider_prior(self):
        assert not self._verb("MEDIUM", 2.19).take
        assert self._verb("MEDIUM", 2.20).take

    def test_novelty_never_charges_once_a_cohort_exists(self):
        cohort = {"unseen-mech": CohortStat(
            key="unseen-mech", n=20, wins=12, mean_r=1.0,
            hit_rate_raw=0.6, hit_rate_shrunk=0.6, informative=True)}
        high = ev_gate(1.2, 0.2, "unseen-mech", cohort, novelty_level="HIGH")
        low = ev_gate(1.2, 0.2, "unseen-mech", cohort, novelty_level="LOW")
        assert high.take and high.basis == "COHORT" and low.basis == "COHORT"
