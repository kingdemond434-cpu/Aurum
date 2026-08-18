"""A copy provider's track record cannot contain the drawdown that ends the
account. These tests are mostly about making that inescapable in the output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from golddesk.reverse import (
    MIN_BASKETS, Basket, Trade, ablate, build_baskets, fixed_risk_normalisation,
    lot_tiers, skew_profile, spacing,
    infer_structure,
    replicate, report, ruin_forensics)

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def t(i, *, side="BUY", lots=0.01, opened=None, closed=None, op=2000.0,
      cp=2001.0, mae=None):
    o = opened or (T0 + timedelta(hours=i))
    return Trade(ticket=str(i), symbol="XAUUSD", direction=side, lots=lots,
                 open_utc=o, close_utc=closed or (o + timedelta(hours=1)),
                 open_price=op, close_price=cp, mae_price=mae)


def martingale(n_baskets=40, depth=4, esc=2.0, spacing=10.0, tp=3.0):
    """A grid, modelled the way a real one actually exits.

    THE EXIT RULE IS THE WHOLE FIXTURE. A martingale does not close at a fixed
    price — it closes when the LOT-WEIGHTED basket reaches a small profit, and
    because the last leg is the biggest, that weighted average sits far below
    the arithmetic average of the entries. Getting this wrong (a fixed, generous
    exit) makes the later entries look individually profitable and hides the
    finding: as traded the basket wins, and with equal risk per fill it loses.
    """
    out, k = [], 0
    for b in range(n_baskets):
        base = T0 + timedelta(days=b)
        entries = [2000.0 - spacing * d for d in range(depth)]
        lots = [round(0.01 * (esc ** d), 6) for d in range(depth)]
        weighted_entry = sum(e * l for e, l in zip(entries, lots)) / sum(lots)
        exit_px = weighted_entry + tp          # basket TP, lot-weighted
        for d in range(depth):
            k += 1
            out.append(Trade(
                ticket=str(k), symbol="XAUUSD", direction="BUY", lots=lots[d],
                open_utc=base + timedelta(hours=d),
                close_utc=base + timedelta(hours=depth + 2),
                open_price=entries[d],          # each add is deeper underwater
                close_price=exit_px,
                mae_price=-spacing * (depth - 1) - 5.0))
    return out


def pyramid(n_baskets=40, depth=3):
    """Adds while WINNING, flat size, staggered exits."""
    out, k = [], 0
    for b in range(n_baskets):
        base = T0 + timedelta(days=b)
        for d in range(depth):
            k += 1
            out.append(Trade(
                ticket=str(k), symbol="XAUUSD", direction="BUY", lots=0.01,
                open_utc=base + timedelta(hours=d),
                close_utc=base + timedelta(hours=d + 5),
                open_price=2000.0 + 8.0 * d,        # each add is further in profit
                close_price=2000.0 + 8.0 * d + 4.0,
                mae_price=-2.0))
    return out


def single_entries(n=40):
    return [t(i, opened=T0 + timedelta(days=i), op=2000.0, cp=2003.0) for i in range(n)]


# ------------------------------------------------------------- basketing

def test_fills_close_together_become_one_basket():
    b = build_baskets([t(0), t(1), t(2)])
    assert len(b) == 1 and b[0].depth == 3


def test_a_long_gap_starts_a_new_basket():
    b = build_baskets([t(0), t(1, opened=T0 + timedelta(days=3))])
    assert len(b) == 2


def test_opposite_sides_are_never_folded_into_one_basket():
    """A hedge is not an add. Folding them would report a martingale as flat
    sizing."""
    b = build_baskets([t(0, side="BUY"), t(1, side="SELL")])
    assert len(b) == 2


def test_different_symbols_are_separate_baskets():
    a = t(0)
    other = Trade("x", "EURUSD", "BUY", 0.01, T0, T0 + timedelta(hours=1), 1.0, 1.1)
    assert len(build_baskets([a, other])) == 2


# ------------------------------------------------------------- structure

def test_a_grid_is_recognised_by_adding_while_underwater():
    """THE DISCRIMINATOR. Scaling and recovery have opposite risk profiles and
    identical equity curves right up until they do not."""
    s = infer_structure(build_baskets(martingale()))
    assert s.kind == "RECOVERY_GRID"
    assert s.add_when_losing_rate >= 0.8
    assert "premium collected, not an edge earned" in s.why


def test_a_pyramid_is_not_called_a_grid():
    s = infer_structure(build_baskets(pyramid()))
    assert s.kind == "SIGNAL_PYRAMID"
    assert s.add_when_losing_rate <= 0.4


def test_single_entry_systems_are_recognised_as_having_no_recovery_layer():
    s = infer_structure(build_baskets(single_entries()))
    assert s.kind == "SINGLE_ENTRY" and "no recovery layer" in s.why


def test_lot_escalation_is_measured():
    s = infer_structure(build_baskets(martingale(esc=2.0)))
    assert s.escalation == pytest.approx(2.0, abs=0.05)


def test_flat_sizing_reads_as_flat():
    s = infer_structure(build_baskets(pyramid()))
    assert s.escalation == pytest.approx(1.0, abs=0.01)


def test_a_common_basket_exit_is_detected():
    s = infer_structure(build_baskets(martingale()))
    assert s.common_exit_rate == 1.0


def test_a_thin_sample_makes_no_structural_claim():
    """A grid's character is a property of its distribution; a handful of
    baskets shows none of it."""
    s = infer_structure(build_baskets(martingale(n_baskets=3)))
    assert s.confidence == "LOW" and "not a property of the system" in s.why


def test_no_baskets_is_reported_rather_than_crashing():
    assert infer_structure([]).kind == "UNKNOWN"


# ------------------------------------------------------- tail-risk forensics

def test_a_grid_is_told_not_to_be_inherited_unmodified():
    bs = build_baskets(martingale())
    rf = ruin_forensics(bs, infer_structure(bs), equity=10_000)
    assert "DO NOT INHERIT THIS UNMODIFIED" in rf.verdict
    assert "where the ruin lives" in rf.verdict


def test_the_depth_headroom_says_how_fast_the_end_arrives():
    """For any escalation worth calling a martingale, this number is small."""
    bs = build_baskets(martingale(esc=2.0))
    rf = ruin_forensics(bs, infer_structure(bs), equity=10_000)
    assert rf.depth_headroom == 1


def test_missing_MAE_makes_the_worst_excursion_a_lower_bound():
    """Absence of a reported excursion is not absence of the excursion."""
    trades = [Trade(str(i), "XAUUSD", "BUY", 0.01, T0 + timedelta(days=i),
                    T0 + timedelta(days=i, hours=1), 2000.0, 2001.0)
              for i in range(30)]
    bs = build_baskets(trades)
    rf = ruin_forensics(bs, infer_structure(bs))
    assert rf.mae_coverage == 0.0 and "LOWER BOUND" in rf.verdict


def test_a_single_entry_system_gets_no_hidden_tail_warning():
    bs = build_baskets(single_entries())
    rf = ruin_forensics(bs, infer_structure(bs))
    assert "no hidden recovery risk" in rf.verdict


def test_absence_of_catastrophe_is_never_read_as_safety():
    bs = build_baskets(pyramid())
    rf = ruin_forensics(bs, infer_structure(bs))
    assert "not evidence of its impossibility" in rf.verdict or "no hidden" in rf.verdict


# ---------------------------------------------------------------- ablation

def test_a_grid_with_no_entry_edge_is_exposed_as_selling_insurance():
    """THE DECISIVE FINDING. Entries alone lose; the record is positive only
    because losers are held and added to until they come back."""
    out = ablate(build_baskets(martingale()))
    assert out["recovery_free_mean"] <= 0 < out["lot_weighted_total"]
    assert "THE RETURN IS THE RECOVERY LAYER" in out["verdict"]
    assert "rare, total loss" in out["verdict"]


def test_the_verdict_compares_as_traded_against_the_ablations_not_two_ablations():
    """The 'original' arm is scored equal-weight like every other arm, so it
    never contained the sizing ladder. Comparing it to an ablation asks whether
    removing sizing changes a number that had no sizing in it — which is how a
    grid gets declared edge-free in both directions at once."""
    out = ablate(build_baskets(martingale()))
    assert out["equal_weight_mean"] < 0 < out["lot_weighted_total"], (
        "the fixture must be a grid that only wins lot-weighted")
    assert not any(a.name.startswith("original") for a in out["arms"])


def test_a_genuine_entry_edge_is_found_and_named_as_the_part_to_rebuild():
    out = ablate(build_baskets(single_entries()))
    assert "THERE IS AN ENTRY EDGE" in out["verdict"]
    assert "without the basket layer" in out["verdict"]


def test_the_third_entry_is_scored_on_its_own():
    """If entry 3 has the expectancy, trade it directly rather than inheriting
    the two losses that produce it."""
    out = ablate(build_baskets(martingale(depth=4)))
    third = next(a for a in out["arms"] if a.name == "third entry only")
    assert third.n_trades == 40


def test_ablation_is_scored_in_price_units_not_currency():
    """Currency folds sizing into the entry measurement: a martingale's currency
    P&L is dominated by its largest leg, so 'first entry only' would look
    terrible for reasons unrelated to the entry."""
    out = ablate(build_baskets(martingale()))
    assert "never currency" in out["note"]


def test_an_empty_book_decomposes_to_nothing_rather_than_crashing():
    out = ablate([])
    assert "no decomposition possible" in out["verdict"]


# ------------------------------------------------ fixed-risk normalisation

def test_a_martingale_return_does_not_survive_fixed_risk_normalisation():
    """THE TEST THAT SETTLES IT. As traded the record is positive; with every
    trade carrying the same risk it is not. The provider is paid for betting
    more after being wrong."""
    fr = fixed_risk_normalisation(build_baskets(martingale()))
    assert fr.lot_weighted > 0 > fr.equal_weighted
    assert "DOES NOT SURVIVE FIXED-RISK NORMALISATION" in fr.verdict
    assert "betting more after being wrong" in fr.verdict


def test_a_real_edge_survives_normalisation_and_is_named_as_the_thing_to_build():
    fr = fixed_risk_normalisation(build_baskets(single_entries()))
    assert fr.equal_weighted > 0
    assert "SURVIVES FIXED-RISK NORMALISATION" in fr.verdict
    assert "what a descendant should be built from" in fr.verdict


def test_equal_risk_beats_equal_size_when_stops_are_reported():
    """Equal SIZE on a $53 stop and a $6 stop are not the same bet."""
    trades = [
        Trade("1", "XAUUSD", "BUY", 0.01, T0, T0 + timedelta(hours=1),
              2000.0, 2010.0, sl=1990.0),                      # +1.0R on a $10 stop
        Trade("2", "XAUUSD", "BUY", 0.01, T0 + timedelta(days=2),
              T0 + timedelta(days=2, hours=1), 2000.0, 2010.0, sl=1900.0),  # +0.1R
    ]
    fr = fixed_risk_normalisation(build_baskets(trades))
    assert fr.basis == "STOP_DISTANCE"
    assert fr.risk_normalised == pytest.approx(0.55, abs=0.01)


def test_missing_stops_fall_back_to_equal_size_and_say_so():
    """Naming the gap is what tells you which extra data is worth chasing."""
    fr = fixed_risk_normalisation(build_baskets(martingale()))
    assert fr.basis == "EQUAL_WEIGHT" and fr.sl_coverage == 0.0
    assert "highest-value thing more data would buy" in fr.verdict


def test_the_sizing_share_of_the_return_is_quantified():
    fr = fixed_risk_normalisation(build_baskets(martingale()))
    assert fr.sizing_share is not None and fr.sizing_share > 0.5


def test_an_unprofitable_record_has_nothing_to_explain_away():
    losers = [t(i, opened=T0 + timedelta(days=i), op=2000.0, cp=1997.0)
              for i in range(20)]
    fr = fixed_risk_normalisation(build_baskets(losers))
    assert "nothing for normalisation to explain away" in fr.verdict


def test_no_closed_trades_is_reported_rather_than_crashing():
    assert fixed_risk_normalisation([]).n == 0


def test_the_report_carries_the_normalisation_verdict():
    assert "FIXED-RISK NORMALISATION" in report(martingale(), equity=10_000)


# ------------------------------------------------------------- replication

def test_the_model_never_sees_baskets_that_had_not_closed():
    """A replication score is exactly the number that looks spectacular if the
    model can fit the future."""
    bs = build_baskets(martingale(n_baskets=10))
    seen = []

    def predict(when, symbol, history):
        seen.append([h.opened for h in history])
        return {"direction": "BUY"}

    replicate(bs, predict)
    for i, hist in enumerate(seen):
        assert all(h < bs[i].opened for h in hist), "history contained the future"


def test_a_coin_flip_model_is_called_a_coin_flip():
    bs = build_baskets(martingale(n_baskets=40))
    flip = iter(["BUY", "SELL"] * 40)
    r = replicate(bs, lambda w, s, h: {"direction": next(flip)})
    assert 0.4 <= r.direction_match <= 0.6
    assert "coin flip" in r.why


def test_a_model_that_predicts_nothing_scores_nothing():
    bs = build_baskets(martingale(n_baskets=10))
    r = replicate(bs, lambda w, s, h: None)
    assert r.n == 0 and "predicted nothing" in r.why


def test_a_raising_model_does_not_take_the_run_with_it():
    bs = build_baskets(martingale(n_baskets=10))
    r = replicate(bs, lambda w, s, h: (_ for _ in ()).throw(RuntimeError("x")))
    assert r.n == 0


# ------------------------------------------------------------------ report

def test_the_report_refuses_to_promote_anything():
    txt = report(martingale(), equity=10_000)
    assert "Nothing here promotes anything" in txt
    assert "enters the registry as a run" in txt


def test_the_report_leads_with_the_structure_verdict():
    txt = report(martingale(), equity=10_000)
    assert "RECOVERY_GRID" in txt and "DO NOT INHERIT" in txt


# ------------------------------------------ ladder or structure

def _spaced(gaps, lot=0.05, n_baskets=4):
    """Baskets whose adds sit at the given gaps."""
    out, k = [], 0
    for b in range(n_baskets):
        base = T0 + timedelta(days=b)
        px = 2000.0
        for j, g in enumerate([0.0] + list(gaps)):
            px += g
            k += 1
            out.append(Trade(str(k), "XAUUSD", "BUY", lot,
                             base + timedelta(hours=j),
                             base + timedelta(hours=len(gaps) + 2), px, px + 3.0,
                             profit=3.0))
    return out


def test_a_fixed_ladder_is_recognised_as_a_grid():
    """A grid's gaps are constant by construction, however the market moved."""
    s = spacing(build_baskets(_spaced([10.0, 10.0, 10.0])))
    assert s.kind == "LADDER" and s.cv <= 0.35
    assert "chosen by arithmetic" in s.why


def test_irregular_gaps_are_recognised_as_structure_driven():
    """THE DISCRIMINATOR THAT WORKS WHEN ADDS-WHILE-LOSING DOES NOT."""
    s = spacing(build_baskets(_spaced([0.09, 1.0, 1.15, 16.48])))
    assert s.kind == "STRUCTURE" and s.cv >= 0.60
    assert "NOT a ladder" in s.why


def test_the_structure_verdict_names_the_unbounded_tail():
    """A grid's depth is bounded by its spacing; a structure basket's is bounded
    by how far the market runs."""
    s = spacing(build_baskets(_spaced([0.09, 1.0, 16.48])))
    assert "how far the market runs before it stops offering levels" in s.why
    assert "bounded by its own spacing" in s.why


def test_too_few_gaps_makes_no_claim():
    s = spacing(build_baskets(_spaced([5.0], n_baskets=1)))
    assert s.kind == "UNCLEAR" and "before the regularity" in s.why


def test_spacing_is_independent_of_pnl_direction():
    """A martingale can pyramid when price whipsaws through its ladder, so
    direction alone cannot separate them. Spacing can."""
    up = spacing(build_baskets(_spaced([10.0, 10.0, 10.0])))
    down = spacing(build_baskets(_spaced([-10.0, -10.0, -10.0])))
    assert up.kind == down.kind == "LADDER"


# ---------------------------------------------- confidence or equity scaling

def _tiered(pairs):
    """pairs: (day_offset, lot). One single-entry basket each."""
    out = []
    for i, (d, lot) in enumerate(pairs):
        t = T0 + timedelta(days=d)
        out.append(Trade(str(i), "XAUUSD", "BUY", lot, t, t + timedelta(hours=2),
                         2000.0, 2003.0, profit=3.0))
    return out


def test_tiers_that_overlap_in_time_are_confidence_not_equity():
    """An account grows monotonically; it cannot produce interleaved regimes."""
    lt = lot_tiers(build_baskets(_tiered(
        [(0, 0.01), (1, 0.20), (2, 0.01), (3, 0.20), (4, 0.01), (5, 0.20)])))
    assert lt.kind == "CONFIDENCE" and lt.interleaved
    assert "rarer than having signals" in lt.why


def test_tiers_in_separate_eras_are_equity_scaling():
    lt = lot_tiers(build_baskets(_tiered(
        [(0, 0.01), (1, 0.01), (2, 0.01),
         (200, 0.20), (201, 0.20), (202, 0.20)])))
    assert lt.kind == "EQUITY_SCALING" and not lt.interleaved
    assert "must not be copied as if it were a signal" in lt.why


def test_one_size_regime_has_nothing_to_explain():
    lt = lot_tiers(build_baskets(_tiered([(0, 0.05), (1, 0.05), (2, 0.052)])))
    assert lt.kind == "SINGLE"


def test_clustering_is_on_ratio_not_absolute_distance():
    """0.01 -> 0.02 is a doubling; 0.23 -> 0.24 is not. An absolute threshold
    cannot tell those apart across two orders of magnitude."""
    lt = lot_tiers(build_baskets(_tiered(
        [(0, 0.23), (1, 0.24), (2, 0.25), (3, 0.01), (4, 0.011)])))
    assert len(lt.tiers) == 2


# ------------------------------------------------------------- the skew

def _skewed(n_wins=45, win=20.0, n_tails=1, tail=-400.0):
    out = []
    for i in range(n_wins):
        t = T0 + timedelta(days=i)
        out.append(Trade(f"w{i}", "XAUUSD", "BUY", 0.05, t,
                         t + timedelta(hours=2), 2000.0, 2001.0, profit=win))
    for j in range(n_tails):
        t = T0 + timedelta(days=200 + j)
        out.append(Trade(f"L{j}", "XAUUSD", "BUY", 0.05, t,
                         t + timedelta(hours=8), 2000.0, 1980.0, profit=tail))
    return out


def test_a_high_win_rate_negatively_skewed_book_is_described_as_such():
    s = skew_profile(build_baskets(_skewed()))
    assert s.win_rate > 0.9
    assert s.tail_ratio >= 20


def test_the_verdict_names_the_tail_frequency_as_the_weak_parameter():
    """Two or three observations set the frequency of the thing that pays for
    everything else."""
    s = skew_profile(build_baskets(_skewed()))
    assert "rests on 1 observation" in s.why
    assert "decided by how OFTEN" in s.why


def test_it_says_what_break_even_looks_like():
    """A run of N clean trades between tails is what merely breaking even looks
    like — which is indistinguishable from working."""
    s = skew_profile(build_baskets(_skewed()))
    assert "merely breaking even" in s.why


def test_a_one_sided_record_gets_no_skew_profile():
    out = [t for t in _skewed(n_tails=0)]
    assert "needs both" in skew_profile(build_baskets(out)).why


def test_a_thin_record_describes_no_shape():
    assert "no shape to describe" in skew_profile(build_baskets(_skewed(3, 20.0, 0))).why
