r"""The numbers that set stops and targets, and the discipline of not inventing them.

`build_cohorts` answers one question well — the shrunk hit rate feeding the EV
gate — and it is not enough to decide anything else. A hit rate says nothing
about how far winners travelled AGAINST the entry before working, or whether TP2
arrives often enough to justify keeping a runner. Those two decide stop distance
and partial policy, and neither was measured anywhere.

THE LAW THIS FILE ENFORCES. A figure computed from three trades is not a small
number, it is a wrong one, and printing it beside a real one launders it. With 14
resolved trades across every mechanism, the correct output of most of this module
is the word UNMEASURED — and a statistics module is exactly where absence
resolving to a clean answer (L1.28a / WS-005) does the most damage, because its
output looks authoritative by construction.

    python3 -m pytest test_cohort_stats.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.cohort_stats import (MIN_FOR_EXPECTANCY, MIN_FOR_MEASURED,
                                   MIN_WINNERS_FOR_MAE, build, render_all,
                                   summarise)


def _o(r, mae=None, mfe=None, mech="m"):
    d = {"mechanism_name": mech, "realised_r": r}
    if mae is not None:
        d["mae_r"], d["mfe_r"] = mae, mfe if mfe is not None else abs(r)
    return d


def _row(r, mae=None, mfe=None, mech="m", valid=True):
    d = {"kind": "TRADE_CLOSED", "ts": "2026-08-28T12:00:00+00:00",
         "entry_t0": "2026-08-28T11:00:00+00:00",
         "mechanism_name": mech, "realised_r": r, "evidence_valid": valid}
    if mae is not None:
        d["mae_r"], d["mfe_r"] = mae, mfe if mfe is not None else abs(r)
    return d


# --------------------------------------------------------------------------
# It refuses to compute what it cannot.

def test_a_thin_cohort_reports_UNMEASURED_rather_than_a_mean():
    c = summarise("m", [_o(1.0), _o(-1.0), _o(0.5)])
    assert c.verdict == "UNMEASURED"
    assert c.net_expectancy_r is None
    assert c.win_rate is None


def test_UNMEASURED_says_so_in_words_not_with_a_number():
    c = summarise("m", [_o(1.0), _o(-1.0)])
    text = c.render()
    assert "UNMEASURED" in text
    assert "hypothesis, not a measurement" in text
    assert "0.00R" not in text


def test_an_unmeasured_cohort_may_not_price_a_live_decision():
    """It is still allowed to FIRE — the desk runs experiments deliberately —
    but it cannot claim an expectancy while doing it."""
    assert summarise("m", [_o(1.0)] * 3).capital_bearing is False


def test_a_thin_cohort_is_not_called_measured():
    c = summarise("m", [_o(0.5)] * MIN_FOR_EXPECTANCY)
    assert c.verdict == "THIN"
    assert c.capital_bearing is False


def test_a_real_cohort_is_measured():
    c = summarise("m", [_o(0.5)] * MIN_FOR_MEASURED)
    assert c.verdict == "MEASURED"
    assert c.capital_bearing is True


# --------------------------------------------------------------------------
# Intervals, not points.

def test_expectancy_comes_with_an_interval():
    """'+0.17R' and '+0.17R [-0.41, +0.75]' support completely different
    decisions, and only one of them is honest at this sample size."""
    c = summarise("m", [_o(x) for x in
                        (1.9, -1.0, 0.4, -1.0, 2.2, -1.0, 0.8, -1.0, 1.4, -1.0)])
    assert c.expectancy_ci is not None
    lo, hi = c.expectancy_ci
    assert lo < c.net_expectancy_r < hi


def test_the_interval_is_rendered_beside_the_estimate():
    c = summarise("m", [_o(x) for x in
                        (1.9, -1.0, 0.4, -1.0, 2.2, -1.0, 0.8, -1.0, 1.4, -1.0)])
    assert "[" in c.render() and "]" in c.render()


def test_the_win_rate_interval_cannot_leave_zero_to_one():
    """A normal-approximation interval routinely runs below 0 or above 1 at
    these sample sizes, and an interval containing impossible values is a strong
    hint the method does not apply. Wilson cannot."""
    c = summarise("m", [_o(1.0)] * MIN_FOR_EXPECTANCY)
    lo, hi = c.win_rate_ci
    assert 0.0 <= lo <= hi <= 1.0


# --------------------------------------------------------------------------
# The stop question: how far do WINNERS go against you first?

def test_stop_guidance_needs_enough_WINNERS_not_enough_trades():
    """A cohort can be large and still have four winners. Stop placement off
    four winners is a stop placed by four coin flips."""
    outs = ([_o(1.5, mae=-0.6) for _ in range(3)]
            + [_o(-1.0, mae=-1.0) for _ in range(9)])
    c = summarise("m", outs)
    assert c.n >= MIN_FOR_EXPECTANCY
    assert c.winner_mae_median_r is None
    assert "stop guidance UNMEASURED" in c.render()


def test_it_reports_how_deep_winners_dug_before_working():
    outs = ([_o(1.5, mae=-0.65) for _ in range(6)]
            + [_o(-1.0, mae=-1.0) for _ in range(4)])
    c = summarise("m", outs)
    assert c.winner_mae_median_r == pytest.approx(-0.65)
    assert "against first" in c.render()


def test_the_p80_stop_is_the_DEEP_end_of_the_winner_distribution():
    """The stop that survives 80% of winners is the deepest excursion they
    routinely produce, not the shallowest. Taking the wrong tail here would
    recommend a stop that removes most of the mechanism's winners."""
    maes = [-0.2, -0.3, -0.4, -0.5, -0.6, -0.9]
    c = summarise("m", [_o(1.5, mae=m) for m in maes] + [_o(-1.0, mae=-1.0)] * 4)
    assert c.winner_mae_p80_r <= c.winner_mae_median_r


def test_the_render_says_what_a_tighter_stop_would_cost():
    outs = ([_o(1.5, mae=-0.65) for _ in range(6)]
            + [_o(-1.0, mae=-1.0) for _ in range(4)])
    assert "removes the trades this mechanism wins on" in summarise("m", outs).render()


# --------------------------------------------------------------------------
# Excursion is counted over its OWN sample.

def test_rows_without_excursion_count_for_expectancy_and_not_for_stops():
    """Conflating the two denominators is how a statistic quietly describes a
    different set of trades than its label claims."""
    outs = [_o(0.5) for _ in range(6)] + [_o(1.5, mae=-0.5) for _ in range(4)]
    c = summarise("m", outs)
    assert c.n == 10
    assert c.n_excursion == 4


def test_the_render_says_when_the_two_samples_differ():
    outs = [_o(0.5) for _ in range(6)] + [_o(1.5, mae=-0.5) for _ in range(4)]
    assert "excursion on 4" in summarise("m", outs).render()


# --------------------------------------------------------------------------
# It reads through the quarantine.

def test_quarantined_rows_never_reach_the_statistics():
    """A quarantined row carries mae 0 and mfe 0 — two zeros that would drag
    every excursion figure toward zero, hardest on losers, which is exactly
    where stop placement is decided."""
    rows = ([_row(1.5, mae=-0.7, mech="a") for _ in range(8)]
            + [_row(-1.02, mae=0.0, mfe=0.0, mech="a", valid=False)
               for _ in range(20)])
    c = build(rows)["a"]
    assert c.n == 8, "quarantined rows entered the cohort"
    assert c.net_expectancy_r > 0


def test_mechanisms_are_kept_apart():
    rows = [_row(1.0, mech="a")] * 3 + [_row(-1.0, mech="b")] * 3
    got = build(rows)
    assert set(got) == {"a", "b"}


# --------------------------------------------------------------------------
# The desk-wide view.

def test_an_empty_desk_says_every_signal_is_a_hypothesis():
    text = render_all({})
    assert "no resolved trades" in text
    assert "hypothesis" in text


def test_measured_cohorts_are_read_first():
    rows = ([_row(0.5, mech="thin")] * MIN_FOR_EXPECTANCY
            + [_row(0.5, mech="solid")] * MIN_FOR_MEASURED
            + [_row(0.5, mech="none")] * 2)
    # BODY ONLY. The header counts verdicts ("1 measured, 1 thin, ..."), so
    # searching the whole string finds the word "thin" in the summary line
    # rather than in the cohort it names.
    body = "\n".join(render_all(build(rows)).splitlines()[1:])
    assert body.index("solid") < body.index("thin") < body.index("none")


def test_the_header_counts_each_verdict():
    rows = ([_row(0.5, mech="solid")] * MIN_FOR_MEASURED
            + [_row(0.5, mech="none")] * 2)
    head = render_all(build(rows)).splitlines()[0]
    assert "1 measured" in head and "1 unmeasured" in head


# --------------------------------------------------------------------------
# It runs in the cycle. Unwired is a defect.

def test_the_daily_cycle_runs_it():
    import aurum_cycle
    assert any(n == "cohorts" for n, _ in aurum_cycle.STEPS)


def test_no_step_is_registered_twice():
    """stop_autopsy was defined twice AND registered twice, so it ran twice and
    printed its whole report twice, every cycle."""
    import collections

    import aurum_cycle
    dupes = [k for k, v in
             collections.Counter(n for n, _ in aurum_cycle.STEPS).items() if v > 1]
    assert dupes == [], dupes


def test_cohorts_run_before_the_steps_that_reason_about_selection():
    """missed_money and levers both ask whether the desk is taking the right
    trades, which cannot be answered before the mechanisms have histories."""
    import aurum_cycle
    names = [n for n, _ in aurum_cycle.STEPS]
    assert names.index("cohorts") < names.index("missed_money")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
