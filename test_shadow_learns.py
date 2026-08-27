"""A gate that starves its own demotion review blocks forever.

THE DEADLOCK. `entry.fallback_min_rr` is the cold-start R:R prior applied to any
mechanism with no resolved history. Its registered rationale promises it "stops
blocking the moment that measurement says it costs more than it saves", and
constitution.py repeats it: "Demote as soon as measured false-negative cost
exceeds avoided loss."

That measurement can never be made:

    the prior blocks unknown mechanisms
      -> no resolved outcomes accumulate for them
        -> constitution.review() returns UNDETERMINED
          -> the prior never demotes
            -> it blocks forever

Every named mechanism the analyst invents — which is every mechanism, since
`mechanism_name` is free text — starts with no history and meets this gate. It
is a gate holding its own escape hatch shut, and it is why a desk that reads the
market correctly can still emit nothing for weeks.

THE FIX IS NOT A LOOSER BAR. The prior's whole purpose is to protect CAPITAL
from a hit rate nobody has measured. In shadow mode there is no capital at risk,
so the benefit side of that trade-off is exactly zero while the cost side — the
evidence never gathered — is the entire reason the desk is running. It therefore
does not enforce in shadow, and enforces unchanged the moment the desk is armed.

    python3 -m pytest test_shadow_learns.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.constitution import BY_ID, Kind, Status
from golddesk.opportunity import ev_gate

#: Well under the 1.5 cold-start prior — the shape that was being refused.
THIN_RR = 0.8


def test_the_prior_blocks_an_unknown_mechanism_when_armed():
    """The protection must still be there with money on the table."""
    v = ev_gate(THIN_RR, 0.05, "some-mechanism-never-seen", None, shadow=False)
    assert not v.take
    assert "prior ENFORCING" in v.reason


def test_the_prior_does_not_block_in_shadow():
    v = ev_gate(THIN_RR, 0.05, "some-mechanism-never-seen", None, shadow=True)
    assert v.take, v.reason
    assert "NOT ENFORCED IN SHADOW" in v.reason


def test_the_shadow_reason_says_why_rather_than_just_that():
    """A log line reading 'prior skipped' teaches nobody anything. This one has
    to survive a reader asking whether the desk just got sloppy."""
    v = ev_gate(THIN_RR, 0.05, "unseen", None, shadow=True)
    assert "no capital at risk" in v.reason
    assert "starves its own demotion review" in v.reason


def test_shadow_does_not_touch_a_mechanism_that_HAS_history():
    """Only the COLD-START branch is affected. A measured cohort keeps its
    verdict in shadow exactly as it has it live — otherwise this would be a
    blanket 'take everything', which it is not."""
    from golddesk.opportunity import CohortStat
    losing = CohortStat(key="known-loser", n=80, wins=8, mean_r=-0.4,
                        hit_rate_raw=0.10, hit_rate_shrunk=0.10,
                        informative=True)
    a = ev_gate(THIN_RR, 0.05, "known-loser", {"known-loser": losing}, shadow=False)
    b = ev_gate(THIN_RR, 0.05, "known-loser", {"known-loser": losing}, shadow=True)
    assert a.basis == b.basis == "COHORT"
    assert a.take == b.take is False
    assert a.reason == b.reason


def test_a_generous_rr_is_taken_either_way():
    """The gate is about the BAR, not about shadow. Clearing it needs no help."""
    for sh in (False, True):
        assert ev_gate(4.0, 0.05, "unseen", None, shadow=sh).take


def test_the_restriction_is_still_registered_and_still_discretionary():
    """Not enforcing in shadow must not quietly delete the restriction — it is
    still measured, still reviewed, and still enforces when armed."""
    r = BY_ID["entry.fallback_min_rr"]
    assert r.kind is Kind.DISCRETIONARY
    assert r.status is Status.ENFORCING
    assert not r.exempt


def test_an_explicit_demotion_still_wins_over_everything():
    """The constitutional path keeps precedence: a demoted restriction reports
    itself demoted, in shadow or armed, rather than being masked by it."""
    r = BY_ID["entry.fallback_min_rr"]
    before = r.status
    try:
        r.status = Status.ADVISORY
        for sh in (False, True):
            v = ev_gate(THIN_RR, 0.05, "unseen", None, shadow=sh)
            assert v.take
            assert "DEMOTED" in v.reason
    finally:
        r.status = before


def test_the_compiler_passes_shadow_through():
    """The gate is useless if the only caller never sets the flag. Both live
    paths — single read and universe — must forward it."""
    import inspect
    from golddesk.analyst import compile_signal
    from golddesk.universe import compile_universe
    assert "shadow" in inspect.signature(compile_signal).parameters
    assert "shadow" in inspect.signature(compile_universe).parameters
    live = Path(__file__).parent / "golddesk" / "live.py"
    src = live.read_text(encoding="utf-8")
    assert src.count("shadow=self.shadow") == 2, (
        "a live decision path is not forwarding shadow — the gate would enforce "
        "in shadow on that path and the desk would stay silent on it")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
