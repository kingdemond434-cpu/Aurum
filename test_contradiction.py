r"""The counter-arguments were right, and they were prose, so they lost.

THE MEASUREMENT THAT ASKED FOR THIS, from the desk's own read_quality audit:

    selection: taken trades resolve -0.14R while refusals reached +0.56R at
    best. The analyst is selecting AGAINST itself.

Not a frequency problem. The desk is not taking too few trades; it is choosing
the wrong ones from a set that contained better. More selectivity makes that
worse, and so do more signals. What is missing is RANKING.

THE LIVE CASE. The 2026-08-28 short wrote, unprompted: "TRENDMATURITY already
reads EXHAUSTED"; "ratio moves are not a timing tool"; "gold can fall for a week
on this premise and still bounce 40 points first, which the L3 stop will not
survive". Every one was correct. All of them were sentences in a paragraph,
weighted by nobody, and the trade went out at conf 2/5 with no number behind it.
Weighed as facts, that same state scores net -3.

IT SCORES, IT DOES NOT GATE — deliberately, not timidly. The standing order is
maximum frequency and the evidence says the fault is ordering, not volume. A
gate would cut the tail of good trades with the bad, and fourteen resolved
trades is nowhere near enough to know where the line belongs. The score is
recorded so "does a negative balance predict a worse outcome" becomes
answerable; if it does, it can earn authority then.

    python3 -m pytest test_contradiction.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.contradiction import weigh


@dataclass
class Ctx:
    trend_direction: str = "NONE"
    trend_health: str = "WEAK"
    trend_maturity: str = "MID"
    volatility_state: str = "NORMAL"
    displacement_state: str = "NONE"
    sweep_state: str = "NONE"
    reclaim_state: str = "NONE"
    distance_from_session_extreme: str = "MID"


# --------------------------------------------------------------------------
# The live case.

def test_the_2026_08_28_short_scores_negative_on_measured_state_alone():
    """Its own prose said all of this. Nothing counted it."""
    b = weigh("SHORT", Ctx(trend_direction="DOWN", trend_health="WEAK",
                           trend_maturity="EXHAUSTED", volatility_state="LOW",
                           reclaim_state="WEAK",
                           distance_from_session_extreme="NEAR"))
    assert b.net < 0
    assert b.contradicted is True
    assert "MORE MEASURED EVIDENCE AGAINST THAN FOR" in b.render()


def test_the_exhausted_trend_appears_as_its_own_line():
    b = weigh("SHORT", Ctx(trend_direction="DOWN", trend_maturity="EXHAUSTED"))
    assert any("EXHAUSTED" in i.fact for i in b.items if not i.supports)


# --------------------------------------------------------------------------
# A genuinely good setup scores positive.

def test_a_clean_with_trend_setup_scores_well():
    """The score must be able to be POSITIVE, or it is a pessimism generator
    rather than a ranking."""
    b = weigh("LONG", Ctx(trend_direction="UP", trend_health="STRONG",
                          trend_maturity="YOUNG", displacement_state="CONFIRMED",
                          reclaim_state="CONFIRMED", volatility_state="NORMAL"))
    assert b.net > 0
    assert b.contradicted is False


def test_the_trend_is_the_heaviest_single_fact():
    """Weights are ranked by RELIABILITY, and the trend's direction is the most
    directly observed thing in the set."""
    b = weigh("LONG", Ctx(trend_direction="UP"))
    assert max(i.weight for i in b.items) == 3


# --------------------------------------------------------------------------
# Direction is symmetric.

def test_the_same_state_flips_sign_with_the_trade():
    up = Ctx(trend_direction="UP", trend_health="STRONG")
    assert weigh("LONG", up).net > 0
    assert weigh("SHORT", up).net < 0


def test_entering_INTO_a_confirmed_opposing_impulse_carries_the_top_weight():
    """It ties with the opposing trend at 3, which is right — both are directly
    observed, and neither should outrank the other on a scale this coarse."""
    b = weigh("LONG", Ctx(trend_direction="DOWN", displacement_state="CONFIRMED"))
    impulse = next(i for i in b.items if "impulse is being entered into" in i.fact)
    assert impulse.supports is False
    assert impulse.weight == max(i.weight for i in b.items)


# --------------------------------------------------------------------------
# Absence is not neutrality.

def test_no_measured_trend_counts_AGAINST_rather_than_as_neutral():
    """'Nothing supports this either way' is not the same as 'nothing opposes
    it', and scoring it zero would let a directionless market look clean."""
    b = weigh("LONG", Ctx(trend_direction="NONE"))
    assert b.net < 0
    assert any("no structural support" in i.fact for i in b.items)


def test_an_unmeasured_context_yields_an_empty_balance_not_a_zero():
    """A state the desk did not compute contributes NOTHING, and render says
    UNMEASURED — because a net of 0 would read as 'evenly balanced', which is a
    claim nobody made."""
    class Bare:
        pass

    b = weigh("LONG", Bare())
    assert b.items == ()
    assert "UNMEASURED" in b.render()
    assert "Absence of contradiction is not support" in b.render()
    assert b.contradicted is False


def test_a_direction_of_NONE_is_not_scored():
    assert weigh("NONE", Ctx(trend_direction="UP")).items == ()


# --------------------------------------------------------------------------
# It scores. It does not gate.

def test_nothing_here_refuses_anything():
    """The standing order is maximum frequency, and the measured fault is
    ordering rather than volume. A gate would cut the tail of good trades along
    with the bad on a sample of fourteen."""
    import inspect

    from golddesk import contradiction
    src = inspect.getsource(contradiction)
    assert "Refusal" not in src
    assert "raise" not in src


def test_contradicted_is_a_label_not_a_veto():
    b = weigh("LONG", Ctx(trend_direction="DOWN"))
    assert b.contradicted is True
    assert isinstance(b.contradicted, bool)     # a flag to record and rank by


def test_the_balance_serialises_for_the_ledger():
    """It has to survive into the row, or the question it exists to make
    answerable stays unanswerable."""
    d = weigh("SHORT", Ctx(trend_direction="DOWN", trend_maturity="EXHAUSTED")).to_dict()
    assert d["direction"] == "SHORT"
    assert isinstance(d["net"], int)
    assert d["items"] and "fact" in d["items"][0]


def test_every_line_is_readable_by_a_human_at_a_glance():
    """It goes on a phone. A weight with no sentence beside it is a number
    nobody can act on."""
    for i in weigh("SHORT", Ctx(trend_direction="DOWN",
                                trend_maturity="EXHAUSTED")).items:
        assert len(i.fact) > 10
        assert i.line.strip()[0] in "+-"


# --------------------------------------------------------------------------
# Wired, and never fatal.

def test_the_desk_records_it_and_cannot_die_doing_so():
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._evidence_balance)
    assert "except Exception" in src and "return {}" in src


def test_it_reaches_the_signal_row():
    import inspect

    from golddesk import live
    assert '"evidence_balance"' in inspect.getsource(live.LiveDesk)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
