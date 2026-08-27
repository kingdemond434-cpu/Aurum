"""The analyst must not assert what it cannot observe — and must still trade.

WHERE THE OVERCLAIMING CAME FROM. The desk's first signal read:

    "That is forced supply that only exists at this level. Sitting on top of it
     are resting sell orders ... behind both is unfilled offer"

None of that is observable from what the analyst is given. It sees prices, a
level table and measured context — never the book, the tape, volume at price, or
anyone's position. Trapped buyers, resting orders and unfilled offer are all
INFERRED. Stated as fact, they cannot be scored against what happened; stated as
a hypothesis with a basis, they can.

And the cause was ours, not the model's. ANALYST_SYSTEM said: "If you cannot
state who is trapped, who must act, or what flow is forced, there is no setup."
That makes ASSERTING an unobservable a precondition for acting, so the analyst
asserted confidently — exactly as instructed.

THE RISK IN FIXING IT is turning a decisive analyst into a hedging one. This
desk's objective forbids timidity, and prose that qualifies everything is a way
of refusing without saying so. The instruction therefore separates the two
explicitly, and these tests pin that separation, because it is the half a future
edit is most likely to lose.

    python3 -m pytest test_epistemic_prompt.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import ANALYST_SYSTEM, AnalystRead


def test_the_mechanism_requirement_survives():
    """The fix must not cost the thing that makes this desk's setups mechanical
    rather than indicator-based. 'Bull flag' must still not be a setup."""
    assert "A setup needs a mechanism, not a pattern name" in ANALYST_SYSTEM
    assert "who is trapped, who must act, or what flow is forced" in ANALYST_SYSTEM


def test_the_analyst_is_told_what_it_cannot_see():
    """Naming the specific blind spots beats a general instruction to hedge."""
    for blind in ("order book", "volume at price", "position"):
        assert blind in ANALYST_SYSTEM, blind
    assert "INFERRING" in ANALYST_SYSTEM


def test_a_mechanism_claim_must_be_grounded_in_the_table():
    assert "OBSERVABLE it rests on" in ANALYST_SYSTEM
    assert "not something you pictured" in ANALYST_SYSTEM


def test_the_instruction_explicitly_refuses_to_reduce_trading():
    """THE LOAD-BEARING TEST. Without this the change reads as 'be more
    cautious', which is a frequency cut wearing an epistemics costume."""
    assert "CHANGES HOW YOU WRITE, NOT WHETHER YOU ACT" in ANALYST_SYSTEM
    assert "same confidence" in ANALYST_SYSTEM
    assert "same frequency" in ANALYST_SYSTEM
    assert "timidity is a defect" in ANALYST_SYSTEM


def test_it_says_why_rather_than_just_what():
    """An instruction whose reason is 'because I said so' is the first one a
    model discards under pressure."""
    assert "measurement, not manners" in ANALYST_SYSTEM
    assert "mechanism_name" in ANALYST_SYSTEM


def test_invalidation_must_carry_a_timing_condition():
    """'If it goes higher' is not falsifiable. Failure to reject WHEN the
    mechanism says it should is itself evidence — recordable only if the
    expectation was stated in advance."""
    assert "TIMING condition" in ANALYST_SYSTEM
    assert "Two M15" in ANALYST_SYSTEM


def test_the_worked_example_shows_both_halves():
    """A rule with no example is a rule that gets interpreted. This one carries
    the before and the after."""
    assert "That is forced supply that only exists at this level." in ANALYST_SYSTEM
    assert "Hypothesis:" in ANALYST_SYSTEM
    assert "conditional on" in ANALYST_SYSTEM


def test_no_decision_bearing_field_gained_a_length_cap():
    """The safety property that lets the repair layer truncate prose without
    ever altering a trade. Prose is capped; decisions are not. A future edit
    adding a cap to a ref or to confidence would make repair unsafe."""
    caps = {n: f.metadata for n, f in AnalystRead.model_fields.items()}
    for name in ("setup", "direction", "entry_ref", "stop_ref",
                 "tp1_ref", "tp2_ref", "confidence"):
        meta = str(caps[name])
        assert "max_length" not in meta, f"{name} gained a length cap"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
