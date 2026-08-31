r"""Is the analyst any GOOD, not merely responding.

analyst_health measures whether reads arrive, how fast, and as what model. All
three can be perfect while the reads are worthless. "Is this analysis correct"
is answerable only against what the market then did — so this reads OUTCOMES,
never the prose, never confidence on its own, never how convincing a `why`
sounded.

MOST OF THESE TESTS PIN A REFUSAL. The desk has two resolved trades; a
calibration curve over two is arithmetic wearing a percentage sign. UNMEASURED
is the correct answer for weeks yet, and saying so is the module's entire value
until then — so the tests that matter most are the ones asserting it declines.

    python3 -m pytest test_read_quality.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.read_quality import (MIN_FOR_CALIBRATION, MIN_FOR_EDGE,
                                   MIN_FOR_SELECTION, audit, render)


def _by(fs, name):
    return next(f for f in fs if f.check == name)


def _trade(i, r, conf=3):
    t0 = f"2026-08-{(i % 27) + 1:02d}T10:00:00+00:00"
    return [{"kind": "SIGNAL", "t0": t0,
             "decision": {"analyst_read": {"confidence": conf}}},
            {"kind": "TRADE_CLOSED", "entry_t0": t0, "realised_r": r}]


def _refusal(i, mfe):
    return {"kind": "REFUSAL_COMPILER", "t0": f"r{i}", "outcome": {"mfe_r": mfe}}


# ------------------------------------------------- it refuses to guess

def test_an_empty_ledger_measures_nothing_and_says_so():
    text = render(audit([]))
    assert "NOTHING IS MEASURABLE YET" in text
    assert "not the same as fine" in text


def test_calibration_declines_below_its_denominator():
    rows = [r for i in range(MIN_FOR_CALIBRATION - 1) for r in _trade(i, 1.0)]
    f = _by(audit(rows), "calibration")
    assert f.ok and "UNMEASURED" in f.detail
    assert "arithmetic, not" in f.detail


def test_edge_declines_below_its_denominator():
    rows = [r for i in range(MIN_FOR_EDGE - 1) for r in _trade(i, -1.0)]
    f = _by(audit(rows), "edge")
    assert f.ok and "UNMEASURED" in f.detail
    assert "NOT" in f.detail and "answered yet" in f.detail


def test_calibration_needs_BOTH_tails_not_just_a_count():
    """25 trades all at confidence 3 says nothing about whether confidence
    discriminates — the count is met and the comparison is still impossible."""
    rows = [r for i in range(MIN_FOR_CALIBRATION + 5) for r in _trade(i, 1.0, conf=3)]
    f = _by(audit(rows), "calibration")
    assert f.ok and "both tails" in f.detail


# ----------------------------------------------- it catches the real faults

def test_confidence_that_carries_no_information_is_caught():
    """If conf-4 pays no more than conf-2, the field is noise — and sizing, the
    evidence tier and the operator all read it as though it does."""
    rows = []
    for i in range(15):
        rows += _trade(i, -0.5, conf=5)
    for i in range(15, 30):
        rows += _trade(i, +1.5, conf=1)
    f = _by(audit(rows), "calibration")
    assert not f.ok
    assert "carries NO information" in f.detail


def test_good_calibration_passes():
    rows = []
    for i in range(15):
        rows += _trade(i, +1.5, conf=5)
    for i in range(15, 30):
        rows += _trade(i, -0.5, conf=1)
    assert _by(audit(rows), "calibration").ok


def test_a_negative_edge_is_caught():
    rows = [r for i in range(MIN_FOR_EDGE + 5) for r in _trade(i, -0.3)]
    f = _by(audit(rows), "edge")
    assert not f.ok
    assert "tuning noise" in f.detail


def test_a_positive_edge_passes():
    rows = [r for i in range(MIN_FOR_EDGE + 5) for r in _trade(i, +0.4)]
    assert _by(audit(rows), "edge").ok


def test_selecting_against_itself_is_caught():
    """The sharpest of the three: an analyst refusing better trades than it
    takes is not cautious, it is wrong in a direction no win-rate can show,
    because the refused trades never enter the numerator."""
    rows = [r for i in range(12) for r in _trade(i, -0.6)]
    rows += [_refusal(i, +2.0) for i in range(MIN_FOR_SELECTION + 2)]
    f = _by(audit(rows), "selection")
    assert not f.ok
    assert "selecting AGAINST itself" in f.detail


def test_selection_declines_without_enough_refusals():
    rows = [r for i in range(12) for r in _trade(i, -0.6)]
    rows += [_refusal(i, +2.0) for i in range(5)]
    assert _by(audit(rows), "selection").ok


def test_the_comparison_is_deliberately_unfair_to_the_desk():
    """Refusals are scored on MFE — an upper bound on what they could have paid
    — against realised on the taken side. If the desk still wins, it is real."""
    src = (Path(__file__).parent / "golddesk" / "read_quality.py").read_text(encoding="utf-8")
    assert "unfair TO THE" in src
    assert "mfe_r" in src


# ------------------------------------------- it never claims too much

def test_the_report_states_that_no_single_read_can_be_judged():
    """One trade is one draw from a distribution nobody has measured."""
    for rows in ([], [r for i in range(40) for r in _trade(i, 0.5)]):
        text = render(audit(rows))
        assert "say a single read was right" in text
        assert "ACROSS many reads" in text


def test_it_reads_outcomes_and_never_prose():
    """The prose is what a model is best at producing regardless of whether it
    is right, so judging quality by it measures fluency."""
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "read_quality.py")
                     .read_text(encoding="utf-8"))
    src = ast.dump(tree)
    for field in ("'why'", "'read'", "'why_not'", "'invalidation'", "'mechanism_name'"):
        assert field not in src, f"read_quality inspects {field}"


def test_nothing_here_can_change_a_threshold():
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "read_quality.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for f in ("Thresholds", "ev_gate", "compile_signal", "is_enforcing"):
        assert f not in names, f
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]


def test_a_bad_edge_escalates_and_is_never_auto_fixed():
    """'The reads resolve negative' is not fixed by restarting anything. A
    process responding to a bad edge by adjusting its own inputs would be a desk
    tuning itself toward a scorecard."""
    from golddesk.read_quality import Finding
    from golddesk.remediate import plan
    for check in ("calibration", "edge", "selection"):
        rem, esc = plan([Finding(check, False, "x")], restart_desk=lambda: True)
        assert not rem, check
        assert [f.check for f in esc] == [check]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
