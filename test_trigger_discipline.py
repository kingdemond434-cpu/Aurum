r"""The thesis was a breakout. The order was a market buy before the breakout.

OBSERVED LIVE, 2026-08-28, on a real Telegram signal:

    "L6 and L9 print the identical price — the swing high ... has never been
     retested ... A trade through L9 would be the first time this session that
     the high has been taken."
    "the first print above L9 forces cover rather than meeting fresh supply"

    ENTRY LONG XAUUSD   entry 4614.66   HOW  at market

The argument is entirely about what happens WHEN L9 breaks. The order is a
market buy placed below L9, before it broke. Those are two different strategies:
one waits for the event and one bets it will occur. The desk recorded the second
while measuring the first, so the mechanism's cohort accumulates evidence about
a trade nobody intended to describe — and the −1R that followed is not evidence
the mechanism failed, because the mechanism was never tested.

Prose could not catch this: the compiler cannot read `why`. So the trigger is a
FIELD now, and compile_signal refuses a market entry whose own trigger has not
printed.

    python3 -m pytest test_trigger_discipline.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import AnalystRead, Setup


def test_the_schema_carries_the_trigger_as_a_FIELD():
    """A mechanism's precondition has to be machine-readable, or enforcement is
    impossible and every check is a guess about prose."""
    assert "trigger_ref" in AnalystRead.model_fields


def test_it_defaults_to_NONE_so_ordinary_reads_are_unaffected():
    """Most setups are already complete. A required field would make every read
    carry ceremony for the minority case."""
    assert AnalystRead.model_fields["trigger_ref"].default == "NONE"


def test_the_prompt_tells_the_analyst_when_to_use_it():
    """A field the model is never told about is a field that stays at its
    default forever — the III.16 failure in prompt form."""
    from golddesk.analyst import ANALYST_SYSTEM
    assert "trigger_ref" in ANALYST_SYSTEM
    assert "THE EVENT YOUR MECHANISM DEPENDS ON" in ANALYST_SYSTEM
    assert "the compiler will refuse it and tell you so" in ANALYST_SYSTEM


def test_the_schema_the_model_is_shown_includes_it():
    """ANALYST_SCHEMA is derived, but the CLI provider sends it in-band and a
    field missing there cannot be returned."""
    from golddesk.analyst import ANALYST_SCHEMA
    assert "trigger_ref" in ANALYST_SCHEMA["properties"]


# -------------------------------------------------- run the real compiler

from golddesk.analyst import Refusal, Thresholds, compile_signal   # noqa: E402
from test_projected_levels import _brief_at_new_low, _read         # noqa: E402


def _above_and_below(brief):
    """A confirmed level above mid and one below, from the real level table."""
    above = [lv for lv in brief.levels
             if lv.confirmed and not lv.projected and lv.price > brief.mid]
    below = [lv for lv in brief.levels
             if lv.confirmed and not lv.projected and lv.price < brief.mid]
    if not above or not below:
        pytest.skip("fixture has no level on both sides of mid")
    return above[0], below[0]


def test_a_LONG_at_market_below_its_own_trigger_is_refused():
    """THE LIVE CASE, through the real compiler. The mechanism was 'the first
    print above L9 forces cover' and the order was a market buy with L9
    untouched."""
    brief, _ = _brief_at_new_low()
    above, _below = _above_and_below(brief)
    res = compile_signal(brief, _read(direction="LONG", entry_ref="MARKET",
                                      trigger_ref=above.id), Thresholds())
    assert isinstance(res, Refusal)
    assert "CONDITIONAL IDEA ENTERED UNCONDITIONALLY" in res.reason


def test_the_refusal_names_the_fix_rather_than_only_the_fault():
    """A refusal the analyst cannot act on produces the same read next bar."""
    brief, _ = _brief_at_new_low()
    above, _below = _above_and_below(brief)
    res = compile_signal(brief, _read(direction="LONG", entry_ref="MARKET",
                                      trigger_ref=above.id), Thresholds())
    assert f"Set entry_ref to {above.id}" in res.reason
    assert "drop trigger_ref" in res.reason


def test_a_trigger_that_HAS_printed_does_not_block_the_entry():
    """The check is about an event that has not happened. Once it has, a market
    entry is exactly the right order and must pass straight through."""
    brief, _ = _brief_at_new_low()
    _above, below = _above_and_below(brief)
    res = compile_signal(brief, _read(direction="LONG", entry_ref="MARKET",
                                      trigger_ref=below.id), Thresholds())
    if isinstance(res, Refusal):
        assert "CONDITIONAL IDEA" not in res.reason, res.reason


def test_an_unlocatable_trigger_is_refused_not_ignored():
    """If the level cannot be found, nothing can say whether the event happened
    — and treating that as 'no trigger' would restore the defect silently."""
    brief, _ = _brief_at_new_low()
    res = compile_signal(brief, _read(trigger_ref="L999"), Thresholds())
    assert isinstance(res, Refusal)
    assert "not a confirmed level" in res.reason


def test_a_read_with_no_trigger_reaches_the_rest_of_the_compiler():
    """The ordinary path must be untaxed: trigger_ref NONE has to behave exactly
    as before this field existed."""
    brief, _ = _brief_at_new_low()
    res = compile_signal(brief, _read(), Thresholds())
    if isinstance(res, Refusal):
        assert "CONDITIONAL IDEA" not in res.reason
        assert "trigger_ref" not in res.reason


def test_a_completed_setup_is_untouched():
    """The check must not tax the ordinary path. trigger_ref NONE means the
    analyst is saying the setup is live now, and nothing changes for it."""
    r = AnalystRead(setup=Setup.NO_SETUP, direction="NONE", entry_ref="NONE",
                    stop_ref="NONE", tp1_ref="NONE", tp2_ref="NONE",
                    mechanism_name="none", confidence=1, read="x", why="y",
                    why_not="z", invalidation="w")
    assert r.trigger_ref == "NONE"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
