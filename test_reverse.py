"""A copy provider's track record cannot contain the drawdown that ends the
account. These tests are mostly about making that inescapable in the output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from golddesk.reverse import (
    MIN_BASKETS, Basket, Trade, ablate, build_baskets, infer_structure,
    replicate, report, ruin_forensics)

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def t(i, *, side="BUY", lots=0.01, opened=None, closed=None, op=2000.0,
      cp=2001.0, mae=None):
    o = opened or (T0 + timedelta(hours=i))
    return Trade(ticket=str(i), symbol="XAUUSD", direction=side, lots=lots,
                 open_utc=o, close_utc=closed or (o + timedelta(hours=1)),
                 open_price=op, close_price=cp, mae_price=mae)


def martingale(n_baskets=40, depth=4, esc=2.0, retrace=True):
    """A grid: adds only while underwater, all legs exit together, small net win."""
    out = []
    k = 0
    for b in range(n_baskets):
        base = T0 + timedelta(days=b)
        entry = 2000.0
        exit_all = base + timedelta(hours=depth + 2)
        # the basket exits at a price that nets a small win overall
        exit_px = entry - 3.0 if retrace else entry - 40.0
        for d in range(depth):
            k += 1
            out.append(Trade(
                ticket=str(k), symbol="XAUUSD", direction="BUY",
                lots=round(0.01 * (esc ** d), 4),
                open_utc=base + timedelta(hours=d),
                close_utc=exit_all,
                open_price=entry - 10.0 * d,        # each add is deeper underwater
                close_price=exit_px,
                mae_price=-10.0 * (depth - 1) - 5.0))
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
    assert out["recovery_free_mean"] <= 0 < out["original_mean"]
    assert "THE RETURN IS THE RECOVERY LAYER" in out["verdict"]
    assert "rare, total loss" in out["verdict"]


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
