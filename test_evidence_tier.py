"""A conf-2 experiment and a measured signal must not look alike on a phone.

WHAT HAPPENED. On 2026-08-27 the desk sent a NOVEL counter-trend long at
confidence 2, into the cohort its own why_not called "the worst cohort this desk
measures", with SWEEP and RECLAIM both reading NONE — a mechanism asserted from
context fields rather than confirmed by structure. The message said all of that.
It said `conf 2/5`, `no measured edge yet for this mechanism`, `RISK estimation
HIGH`, and "filed NOVEL and expected to be shadowed rather than sized".

Every caveat was present and the operator traded it with real money.

The message was not wrong. It was UNRANKED: five separate qualifications, none
of them on the first line, and the first line is what gets read on a phone. An
unranked caveat is one the reader has to assemble for themselves at the moment
they are least inclined to.

THIS IS NOT A GATE, and the tests below pin that. Nothing refuses a trade, moves
a threshold, or changes what reaches the ledger. The desk should keep firing
low-evidence NOVEL trades — that is how cohorts get built — it should just never
let one arrive looking like a proven setup.

    python3 -m pytest test_evidence_tier.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.tiers import MEASURED_N, EvidenceTier, evidence_tier

#: The signal that prompted this file, field for field.
THE_2608_LONG = dict(setup="NOVEL", mechanism_name="exhaustion-squeeze-long",
                     confidence=2, sweep_state="NONE", reclaim_state="NONE",
                     displacement_state="NONE", htf_alignment="ALIGNED",
                     with_trend=False)

#: A clean signal on a named family with structure actually confirming it.
CONFIRMED = dict(setup="SWING_REVERSAL", mechanism_name="sweep-reclaim",
                 confidence=4, sweep_state="CONFIRMED",
                 reclaim_state="CONFIRMED", displacement_state="NONE",
                 htf_alignment="NEUTRAL", with_trend=True)


def test_the_trade_that_caused_this_ranks_bottom():
    t = evidence_tier(**THE_2608_LONG)
    assert t.rank == 4 and t.label == "EXPERIMENT"
    assert "NOVEL" in t.why and "GENERATE evidence" in t.why


def test_a_confirmed_named_setup_ranks_above_it():
    a, b = evidence_tier(**CONFIRMED), evidence_tier(**THE_2608_LONG)
    assert a.rank < b.rank
    assert a.label == "CONFIRMED"


def test_a_measured_cohort_is_the_only_way_to_reach_the_top():
    """T1 is earned by resolved trades, not by anything the model says."""
    assert evidence_tier(**CONFIRMED, cohort_n=MEASURED_N, cohort_ev_r=0.21).rank == 1
    assert evidence_tier(**CONFIRMED, cohort_n=MEASURED_N - 1, cohort_ev_r=0.21).rank != 1


def test_a_measured_but_LOSING_cohort_does_not_reach_the_top():
    """History that says the mechanism loses is not evidence FOR it."""
    assert evidence_tier(**CONFIRMED, cohort_n=200, cohort_ev_r=-0.30).rank != 1


def test_confidence_can_only_demote_never_promote():
    """THE LOAD-BEARING RULE. Confidence is the model's opinion of itself. If it
    could raise a tier, a model could talk its way into looking proven and the
    entire ranking would be worthless."""
    high = evidence_tier(**{**THE_2608_LONG, "confidence": 5})
    assert high.rank == 4, "a NOVEL experiment reached a better tier by asserting confidence"

    sure = evidence_tier(**CONFIRMED)                       # confidence 4
    unsure = evidence_tier(**{**CONFIRMED, "confidence": 2})
    assert unsure.rank > sure.rank                          # demotion works
    assert unsure.label == "UNMEASURED"


def test_unconfirmed_structure_is_an_experiment_however_clean_it_looks():
    """A named family with nothing confirming it is still asserted, not shown."""
    t = evidence_tier(**{**CONFIRMED, "sweep_state": "NONE",
                         "reclaim_state": "WEAK", "displacement_state": "FORMING"})
    assert t.rank == 4
    assert "asserted from context fields" in t.why


def test_counter_trend_into_an_aligned_move_is_an_experiment():
    t = evidence_tier(**{**CONFIRMED, "with_trend": False,
                         "htf_alignment": "ALIGNED"})
    assert t.rank == 4
    assert "worst cohort" in t.why


def test_a_displacement_alone_counts_as_confirmed_structure():
    """TREND_CONTINUATION confirms through displacement, not sweep/reclaim —
    requiring the wrong one would file every continuation as an experiment."""
    t = evidence_tier(setup="TREND_CONTINUATION", mechanism_name="disp-retrace",
                      confidence=3, sweep_state="NONE", reclaim_state="NONE",
                      displacement_state="CONFIRMED", htf_alignment="ALIGNED",
                      with_trend=True)
    assert t.rank == 2


def test_the_banner_leads_with_the_rank_and_carries_the_reason():
    """It has to survive being the only line someone reads."""
    b = evidence_tier(**THE_2608_LONG).banner
    assert b.startswith("*[T4 EXPERIMENT]*")
    assert "NOVEL" in b


def test_every_tier_states_a_reason():
    for kw in (THE_2608_LONG, CONFIRMED,
               {**CONFIRMED, "confidence": 2},
               {**CONFIRMED, "cohort_n": 99, "cohort_ev_r": 0.4}):
        t = evidence_tier(**kw)
        assert isinstance(t, EvidenceTier)
        assert len(t.why) > 20, t


# ------------------------------------------------- it is not a gate

def test_the_tier_module_cannot_refuse_anything():
    """Source-level, because the danger is a later edit quietly turning a label
    into a veto.

    Walks the AST rather than grepping text: the module DOCSTRING says the words
    "refuses a trade" while promising not to, and a naive substring check fails
    on its own explanation. Identifiers and imports are what matter.
    """
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "tiers.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            names.add(n.module or "")
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
    for forbidden in ("Refusal", "refuse", "route", "ev_gate", "compile_signal",
                      "is_enforcing", "permitted"):
        assert forbidden not in names, (
            f"tiers.py references {forbidden!r} — it is a label, not a gate")
    # And it must not be able to stop anything by raising, either.
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)], (
        "tiers.py raises — ranking a signal must never be able to kill one")


def test_the_tier_is_journalled_not_only_displayed():
    """A ranking the operator sees and the ledger does not is one no later
    analysis can group by — and "do T4 experiments resolve worse than T2
    signals" is the question this exists to make answerable."""
    src = (Path(__file__).parent / "golddesk" / "live.py").read_text(encoding="utf-8")
    assert '"evidence_tier"' in src
    assert "tier.banner" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
