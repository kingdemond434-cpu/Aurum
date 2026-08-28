r"""A setup label is a CLAIM ABOUT STATE, and the model was the only thing checking it.

The label decides which cohort an outcome joins. So a read asserting
TREND_CONTINUATION in fluent prose while the measured trend is NONE does not
merely describe the trade badly — it files the result into a
trend-continuation cohort the trade was never an instance of, and every
expectancy figure computed from that cohort is then about a mixture nobody
chose.

The desk got this right once by luck. A live read on 2026-08-28 said, unprompted:

    "TRENDDIRECTION is NONE and TRENDHEALTH WEAK, so there is no measured trend
     to continue ... which is why this is NOVEL not TRENDCONTINUATION"

The analyst reasoned correctly and nothing REQUIRED it to. A label enforced only
by the model's good judgement is one that will eventually be wrong quietly.

DOWNGRADED, NOT REFUSED. Entering at market before a breakout is a different
trade and cannot be kept (see test_trigger_discipline). A mislabelled setup is
the SAME trade with the wrong name — same entry, stop and targets — so it
becomes NOVEL, which routes to shadow by construction. The idea still generates
evidence; it just cannot borrow a named mechanism's history to do it.

    python3 -m pytest test_setup_labels.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import Setup, setup_contradiction


@dataclass
class Ctx:
    trend_direction: str = "NONE"
    trend_health: str = "WEAK"


# --------------------------------------------------------------------------
# The contradictions that are unambiguous from measured state.

def test_no_trend_cannot_be_continued():
    """THE LIVE CASE."""
    why = setup_contradiction(Setup.TREND_CONTINUATION, "SHORT", Ctx("NONE"))
    assert why is not None
    assert "no measured trend to continue" in why


def test_continuation_against_the_measured_trend_is_a_reversal():
    why = setup_contradiction(Setup.TREND_CONTINUATION, "LONG", Ctx("DOWN"))
    assert why is not None
    assert "that is a reversal, not a continuation" in why


def test_a_genuine_continuation_passes():
    assert setup_contradiction(Setup.TREND_CONTINUATION, "LONG", Ctx("UP")) is None
    assert setup_contradiction(Setup.TREND_CONTINUATION, "SHORT", Ctx("DOWN")) is None


def test_a_reversal_INTO_a_strong_trend_reverses_nothing():
    why = setup_contradiction(Setup.SWING_REVERSAL, "LONG", Ctx("UP", "STRONG"))
    assert why is not None
    assert "nothing is being reversed" in why


def test_a_reversal_AGAINST_the_trend_is_exactly_what_it_says():
    assert setup_contradiction(Setup.SWING_REVERSAL, "SHORT", Ctx("UP", "STRONG")) is None


# --------------------------------------------------------------------------
# It stays narrow on purpose. A precondition that is arguable refuses good
# trades on a definitional quibble.

def test_a_reversal_against_a_WEAK_trend_is_not_second_guessed():
    """Weak-trend reversals are the ordinary case, not a contradiction."""
    assert setup_contradiction(Setup.SWING_REVERSAL, "LONG", Ctx("UP", "WEAK")) is None


def test_NOVEL_always_passes():
    """It is the honest label for an unnamed mechanism and routes to shadow by
    construction. Gating it would leave nowhere for a new idea to go."""
    for trend in ("UP", "DOWN", "NONE"):
        for d in ("LONG", "SHORT"):
            assert setup_contradiction(Setup.NOVEL, d, Ctx(trend, "STRONG")) is None


def test_a_context_missing_the_fields_does_not_manufacture_a_contradiction():
    """UNMEASURED is not a violation. A context without trend fields must pass,
    or an un-wired caller silently loses every named setup."""
    class Bare:
        pass

    assert setup_contradiction(Setup.TREND_CONTINUATION, "LONG", Bare()) is None


# --------------------------------------------------------------------------
# The compiler downgrades rather than refusing.

from golddesk.analyst import Refusal, Thresholds, compile_signal   # noqa: E402
from test_projected_levels import _brief_at_new_low, _read         # noqa: E402


def test_a_mislabelled_setup_is_relabelled_NOVEL_not_thrown_away():
    """Refusing would discard a real proposition over a naming error. The trade
    is unchanged; only the cohort it may join is."""
    brief, _ = _brief_at_new_low()
    read = _read(setup=Setup.TREND_CONTINUATION, direction="LONG")
    res = compile_signal(brief, read, Thresholds())
    if isinstance(res, Refusal):
        # Refused for some OTHER reason is fine; refused for the LABEL is not.
        assert "continuation" not in res.reason.lower(), res.reason
    else:
        assert res.setup is Setup.NOVEL


def test_the_original_read_object_is_not_mutated():
    """model_copy, not assignment. The caller's read is journalled elsewhere and
    a compiler that edits its input makes the ledger disagree with itself."""
    brief, _ = _brief_at_new_low()
    read = _read(setup=Setup.TREND_CONTINUATION, direction="LONG")
    compile_signal(brief, read, Thresholds())
    assert read.setup is Setup.TREND_CONTINUATION


def test_a_correctly_labelled_setup_keeps_its_label():
    """The downgrade must not fire on the ordinary path, or every named
    mechanism decays into NOVEL and cohorts never accumulate."""
    brief, st = _brief_at_new_low()
    want = "SHORT" if st.trend_direction == "DOWN" else "LONG"
    if st.trend_direction == "NONE":
        pytest.skip("fixture has no measured trend to continue")
    res = compile_signal(brief, _read(setup=Setup.TREND_CONTINUATION,
                                      direction=want), Thresholds())
    if not isinstance(res, Refusal):
        assert res.setup is Setup.TREND_CONTINUATION


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
