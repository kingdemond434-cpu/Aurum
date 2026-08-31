"""The ported gauge keeps the three properties that made it worth porting.

Scale-free, symmetric, causal. If any of them fails the port is not the same
detector, it is a new one with a borrowed validation record -- which would be
worse than having no detector, because the measured effect sizes in
TrendGauge.render() would be advertising something that was never tested.
"""
from __future__ import annotations

import math

import pytest

from golddesk.trend import TrendGauge, efficiency_ratio, gauge_from_bars


def bars(close, wick=0.4):
    close = list(map(float, close))
    high = [max(c, close[max(i - 1, 0)]) + wick for i, c in enumerate(close)]
    low = [min(c, close[max(i - 1, 0)]) - wick for i, c in enumerate(close)]
    return high, low, close


def ramp(n=200, slope=1.0, base=2000.0):
    return [base + slope * i for i in range(n)]


def chop(n=200, amp=5.0, base=2000.0):
    return [base + amp * math.sin(i / 2.0) for i in range(n)]


# ------------------------------------------------------------- scale-free

def test_multiplying_every_price_changes_nothing():
    h, l, c = bars(ramp())
    a = gauge_from_bars(h, l, c)
    b = gauge_from_bars([x * 3 for x in h], [x * 3 for x in l],
                        [x * 3 for x in c])
    assert a.strength == pytest.approx(b.strength, abs=1e-9)
    assert a.direction == b.direction


def test_a_quiet_trend_and_a_violent_one_both_register():
    """The point of ratios: small trend days must not be binned as chop."""
    quiet = gauge_from_bars(*bars(ramp(slope=0.05), wick=0.02))
    loud = gauge_from_bars(*bars(ramp(slope=1.0), wick=0.4))
    assert quiet.strength > 0.6 and loud.strength > 0.6
    assert abs(quiet.strength - loud.strength) < 0.15


# --------------------------------------------------------------- symmetric

def test_mirroring_flips_direction_and_preserves_strength():
    c = ramp(slope=1.0)
    mirrored = [2 * c[0] - x for x in c]
    up = gauge_from_bars(*bars(c))
    dn = gauge_from_bars(*bars(mirrored))
    assert up.strength == pytest.approx(dn.strength, abs=1e-9), \
        "strength is not mirror-invariant: the gauge has a side"
    assert up.direction == 1 and dn.direction == -1


# ------------------------------------------------------------------ causal

def test_only_bars_up_to_now_are_used():
    """Appending future bars must not change the reading for the earlier one."""
    c = ramp(n=150)
    a = gauge_from_bars(*bars(c))
    longer = c + [c[-1] - 40 * i for i in range(1, 40)]
    h2, l2, c2 = bars(longer)
    b = gauge_from_bars(h2[:150], l2[:150], c2[:150])
    assert a.strength == pytest.approx(b.strength, abs=1e-12)
    assert a.direction == b.direction


# ------------------------------------------------------- does it discriminate

def test_chop_scores_below_trend_and_reports_no_direction():
    ch = gauge_from_bars(*bars(chop()))
    tr = gauge_from_bars(*bars(ramp()))
    assert ch.strength < TrendGauge.FLOOR <= tr.strength
    assert ch.direction == 0 and tr.direction == 1


def test_efficiency_ratio_endpoints():
    assert efficiency_ratio([float(i) for i in range(60)], 10) == pytest.approx(1.0)
    saw = ([float(i) for i in range(6)] + [float(i) for i in range(4, -1, -1)]) * 10
    assert efficiency_ratio(saw, 10) < 0.35


def test_too_little_history_returns_none_rather_than_a_guess():
    assert gauge_from_bars(*bars(ramp(n=5))) is None


# ---------------------------------------------------------------- the dying

def test_a_flip_against_the_held_direction_is_called_dying():
    c = ramp(n=120) + [2119.0 - 3.0 * i for i in range(1, 40)]
    g = gauge_from_bars(*bars(c), prior_direction=1)
    assert g.direction == -1
    assert g.dying is True


def test_an_adverse_shock_is_called_dying_even_without_a_flip():
    c = ramp(n=120)
    c[-1] = c[-2] - 60.0                    # one violent bar against the trend
    g = gauge_from_bars(*bars(c), prior_direction=1)
    assert g.dying is True


def test_a_healthy_trend_is_not_dying():
    assert gauge_from_bars(*bars(ramp()), prior_direction=1).dying is False


# ------------------------------------------------- what the brief will say

def test_render_carries_the_effect_size_and_the_provenance():
    """The analyst must not read 0.8 as a conviction multiplier."""
    txt = gauge_from_bars(*bars(ramp())).render()
    assert "TREND_MEASURED_EDGE" in txt
    assert "ATR / 24 bars" in txt
    assert "NOT yet confirmed on XAUUSD" in txt


def test_labels_are_readable():
    assert gauge_from_bars(*bars(ramp())).label.endswith("_UP")
    assert gauge_from_bars(*bars(chop())).label == "CHOP"
