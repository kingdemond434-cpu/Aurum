"""Tests for the known-answer harness — including that it catches known bugs.

A calibration suite is only worth its runtime if it fails on a defect it was
written for. Building this one, three of its four probes were themselves wrong
in ways that produced confident false verdicts, so these tests pin the fixtures
as hard as the engine.
"""
from __future__ import annotations

import math
import random

from golddesk.calibration import (Probe, cost_recovery, edge_recovery,
                                  lookahead, monotone_costs, random_walk,
                                  run_all, with_edge)


def test_random_walk_has_no_drift():
    b = random_walk(n=20000, seed=1)
    steps = [x["close"] - x["open"] for x in b]
    se = (sum(s * s for s in steps) / len(steps)) ** 0.5 / math.sqrt(len(steps))
    assert abs(sum(steps) / len(steps)) < 4 * se, "the null world has a drift"


def test_random_walk_bars_are_well_formed():
    for x in random_walk(n=2000, seed=2):
        assert x["low"] <= min(x["open"], x["close"])
        assert x["high"] >= max(x["open"], x["close"])
        assert x["low"] > 0


def test_with_edge_reports_what_it_drew_not_what_it_asked_for():
    """THE BUG THIS FIXTURE HAD. Drawing 6,666 outcomes at p=0.4 lands near it,
    not on it, and comparing an engine to the intended value reported a 0.83x
    defect in an engine that was exactly right."""
    _, entries, realised = with_edge(n=9000, edge_r=0.20, seed=3)
    exact = sum(e["r"] for e in entries) / len(entries)
    assert abs(realised - exact) < 1e-12
    assert realised != 0.20, "a realised draw landing exactly on target is suspect"


def test_with_edge_leaves_a_bar_between_signal_and_the_move():
    """The engine fills at the NEXT open. The first fixture moved the full
    distance inside the signal bar, so entry landed on the target and every
    trade closed at 0.0000R."""
    bars, entries, _ = with_edge(n=300, edge_r=0.2, seed=4)
    for e in entries[:20]:
        i = e["i"]
        entry_bar = bars[i + 1]
        assert entry_bar["open"] == entry_bar["close"], "entry bar must be flat"
        assert e["stop"] < entry_bar["open"] < e["target"]


def test_with_edge_outcomes_are_exactly_2R_or_minus_1R():
    _, entries, _ = with_edge(n=3000, edge_r=0.2, seed=5)
    assert {e["r"] for e in entries} == {2.0, -1.0}


# --------------------------------------------------- the probes catch real bugs

def _engine(charge_factor: float = 1.0, truth_cost: float = 0.23):
    """A toy engine that charges `charge_factor` x the true cost."""
    def no_edge_with_stop(bars, mult=1.0):
        return -truth_cost * charge_factor * mult / 10.0, 10.0

    def no_edge(bars, mult=1.0):
        return -truth_cost * charge_factor * mult / 10.0

    def planted(bars, entries):
        return sum(e["r"] for e in entries) / len(entries)

    return {"no_edge": no_edge, "no_edge_with_stop": no_edge_with_stop,
            "planted": planted, "truth_cost_per_unit": truth_cost,
            "stop": 10.0, "planted_r": 0.20}


def test_cost_probe_passes_a_correct_engine():
    assert run_all(_engine(1.0)).probes[0].passed


def test_cost_probe_catches_a_33x_undercharge():
    """THE GOLD BUG, reproduced. 0.03x of the real spread must fail loudly."""
    p = run_all(_engine(0.03)).probes[0]
    assert not p.passed
    assert "UNITS error" in p.detail


def test_cost_probe_catches_an_overcharge_too():
    assert not run_all(_engine(3.0)).probes[0].passed


def test_cost_probe_ignores_what_the_adapter_claims():
    """THE DEFECT THE HARNESS ITSELF HAD. Its first version took the expected
    cost from the adapter, so a wrong configuration was compared against its own
    wrong number and PASSED at 0.64x while certifying a 33x error. Truth must
    come from outside the thing under test."""
    eng = _engine(0.03)
    eng["truth_cost_per_unit"] = 0.23        # ground truth, not the 0.03 charge
    assert not run_all(eng).probes[0].passed


def test_edge_probe_catches_a_scaled_engine():
    eng = _engine(1.0)
    base = eng["planted"]
    eng["planted"] = lambda b, e: base(b, e) * 0.5
    assert not run_all(eng).probes[1].passed


def test_edge_probe_catches_a_sign_flip():
    eng = _engine(1.0)
    base = eng["planted"]
    eng["planted"] = lambda b, e: -base(b, e)
    assert not run_all(eng).probes[1].passed


def test_lookahead_probe_fires_on_a_clairvoyant_engine():
    eng = _engine(1.0)
    eng["no_edge"] = lambda b, mult=1.0: 0.35      # profits on pure noise
    assert not run_all(eng).probes[2].passed


def test_monotone_probe_catches_a_cost_sign_error():
    eng = _engine(1.0)
    eng["no_edge"] = lambda b, mult=1.0: +0.01 * mult   # costs INCREASE returns
    assert not run_all(eng).probes[3].passed


def test_report_is_honest_about_what_passing_means():
    r = run_all(_engine(1.0))
    assert r.passed
    assert "does not prove the" in r.render()
