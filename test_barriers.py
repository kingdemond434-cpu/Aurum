"""What the trade is likely to DO, and the honesty that makes it usable.

    python3 -m pytest test_barriers.py -q

THE UPGRADE. The desk's answer was a direction and a confidence out of five —
a point estimate of a label, which cannot answer any of the questions that
decide a position: how often does this reach +1R before the stop, how far does
it go against me first, when does it get there, what does the bad tenth look
like. All four are estimable from rows the desk was already writing and never
reading.

THE HONESTY, which is most of what is tested below:

  UNMEASURED BELOW ITS FLOOR   a barrier probability from six trades is not an
                               estimate with wide error bars, it is the sample
                               with a percent sign on it.

  EVERY FIGURE IS A FLOOR      a managed exit closes a trade that was still
                               open, so its MFE is the best it reached BEFORE
                               being closed, not the best it would have. This
                               desk banks partials and moves stops, so that is
                               most of its closes, and the report says so every
                               single time rather than once in a docstring.

  WILSON, NOT TEXTBOOK         15 hits out of 15 has a real upper bound of 1.0
                               and a lower bound well under it. The normal
                               approximation returns [1.0, 1.0], which is a
                               claim of certainty from fifteen observations.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.barriers import (BARRIERS_R, MIN_FOR_BARRIERS, MIN_FOR_MEASURED,
                               MIN_FOR_QUANTILES, estimate, render_all)


def closed(r, mfe, mae, reason="STOP", mech="m", t_mfe=600.0, valid=True):
    return {"kind": "TRADE_CLOSED", "realised_r": r, "mfe_r": mfe, "mae_r": mae,
            "reason": reason, "mechanism_name": mech, "t_mfe": t_mfe,
            "evidence_valid": valid}


def sample(n, mfe=1.5, mae=-0.6, reason="TARGET"):
    return [closed(1.0 if mfe >= 1 else -1.0, mfe, mae, reason) for _ in range(n)]


# ------------------------------------------------------------ it refuses early

def test_below_the_floor_there_is_no_number_at_all():
    b = estimate(sample(6))
    assert b.verdict == "UNMEASURED"
    assert b.barriers == [] and b.mfe_mean is None
    assert b.p_at(1.0) is None
    assert "UNMEASURED" in b.render()
    assert "percent sign" in b.render()


def test_thin_is_distinguished_from_measured():
    assert estimate(sample(MIN_FOR_BARRIERS)).verdict == "THIN"
    assert estimate(sample(MIN_FOR_MEASURED)).verdict == "MEASURED"


def test_quantiles_have_their_own_higher_floor():
    """A 10th percentile over fifteen samples is the second-worst trade in the
    set with a decimal point after it."""
    assert estimate(sample(MIN_FOR_BARRIERS)).r_q10 is None
    assert estimate(sample(MIN_FOR_QUANTILES)).r_q10 is not None


def test_a_quarantined_row_never_reaches_the_distribution():
    """An unobserved path carries mfe 0 and mae 0 — two numbers that are not
    measurements, and they drag every probability toward zero."""
    rows = sample(20) + [closed(-1.0, 0.0, 0.0, valid=False) for _ in range(20)]
    assert estimate(rows).n == 20


# ------------------------------------------------------------ it measures right

def test_the_barrier_is_the_share_that_reached_it():
    """The stop terminates at -1R, so any trade whose MFE reached +xR reached it
    BEFORE -1R. No path reconstruction is needed and none is done."""
    rows = ([closed(1.0, 2.5, -0.4, "TARGET") for _ in range(10)]
            + [closed(-1.0, 0.3, -1.0, "STOP") for _ in range(10)])
    b = estimate(rows)
    assert b.p_at(1.0) == pytest.approx(0.5)
    assert b.p_at(2.0) == pytest.approx(0.5)
    assert b.p_at(3.0) == pytest.approx(0.0)
    assert b.p_at(0.5) == pytest.approx(0.5)


def test_every_barrier_in_the_set_is_reported():
    b = estimate(sample(MIN_FOR_MEASURED))
    assert [x.r for x in b.barriers] == list(BARRIERS_R)


def test_a_perfect_record_does_not_claim_certainty():
    """Wilson, not the normal approximation: 20/20 is not proof of 100%."""
    b = estimate([closed(1.0, 2.0, -0.2, "TARGET") for _ in range(20)])
    iv = next(x for x in b.barriers if x.r == 1.0).interval
    assert iv is not None
    assert iv[0] < 1.0, "a lower bound of 1.0 from twenty trades is a false claim"
    assert iv[1] <= 1.0


def test_the_drawdown_quoted_is_the_tail_not_the_average():
    """A stop has to clear the drawdown most trades survive, not the mean one."""
    rows = ([closed(1.0, 2.0, -0.2, "TARGET") for _ in range(15)]
            + [closed(1.0, 2.0, -1.0, "TARGET") for _ in range(5)])
    b = estimate(rows)
    assert b.mae_p80 is not None and b.mae_mean is not None
    assert b.mae_p80 < b.mae_mean, "p80 is not deeper than the mean"


def test_time_to_the_best_point_is_reported_in_minutes():
    b = estimate([closed(1.0, 2.0, -0.3, "TARGET", t_mfe=1800.0) for _ in range(20)])
    assert b.minutes_to_mfe_median == pytest.approx(30.0)


# ------------------------------------------------- it says what it cannot know

def test_managed_exits_are_declared_as_censoring_the_upside():
    rows = [closed(0.4, 0.9, -0.3, "MANAGED_EXIT") for _ in range(20)]
    b = estimate(rows)
    assert b.n_managed == 20 and b.censored_share == 1.0
    assert "LOWER BOUND" in b.render()


def test_a_clean_stop_or_target_sample_is_not_called_censored():
    rows = ([closed(1.0, 2.0, -0.3, "TARGET") for _ in range(10)]
            + [closed(-1.0, 0.2, -1.0, "STOP") for _ in range(10)])
    b = estimate(rows)
    assert b.n_managed == 0
    assert "LOWER BOUND" not in b.render()


def test_a_mechanism_with_no_history_gets_no_distribution_of_its_own():
    rows = sample(20) + [closed(1.0, 2.0, -0.3, "TARGET", mech="rare")
                         for _ in range(3)]
    assert estimate(rows, "rare").verdict == "UNMEASURED"
    assert estimate(rows, "m").verdict != "UNMEASURED"


def test_render_all_says_so_when_no_mechanism_qualifies():
    """Spread thin across many mechanisms, the desk-wide figure is the only
    honest one — and the report says that rather than leaving a blank."""
    rows = [closed(1.0, 2.0, -0.3, "TARGET", mech=f"m{k % 9}") for k in range(20)]
    text = render_all(rows)
    assert "No mechanism has enough resolved trades" in text


def test_render_all_shows_a_mechanism_that_does_qualify():
    text = render_all(sample(20))
    assert text.count("OUTCOME DISTRIBUTION") == 2
    assert "[m]" in text


# ----------------------------------------------------------------- it is WIRED

def test_the_distribution_runs_daily():
    import aurum_cycle
    names = [n for n, _ in aurum_cycle.STEPS]
    assert "barriers" in names
    assert names.index("cohorts") < names.index("barriers")


def test_the_signal_message_carries_it():
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk)
    assert "_outcome_line" in src
    assert "P(+1R first)" in src


def test_the_message_falls_back_to_the_desk_wide_figure_and_labels_it():
    from golddesk.live import LiveDesk
    d = LiveDesk.__new__(LiveDesk)
    d._closed_rows = sample(30)
    line = d._outcome_line("a-mechanism-with-no-history")
    assert "[all]" in line, "the fallback must say which population it used"
    assert "P(+1R first)" in line


def test_the_message_says_unmeasured_rather_than_inventing_one():
    from golddesk.live import LiveDesk
    d = LiveDesk.__new__(LiveDesk)
    d._closed_rows = sample(3)
    line = d._outcome_line("m")
    assert "UNMEASURED" in line and "judgement, not a frequency" in line


def test_a_close_is_remembered_without_re_reading_the_ledger():
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._close)
    assert "_closed_rows.append" in src
