"""How much to bank at TP1 must come from conditions, not from a constant.

A fixed half treats a YOUNG, ALIGNED, LOW-volatility trend exactly like an
EXHAUSTED one in an EXTREME tape. Those want opposite treatment: the first has a
runner worth protecting FROM the bank, the second a runner worth protecting
AGAINST. A constant is a decision not to look.

WHAT THESE TESTS DO AND DO NOT CLAIM. They pin the DIRECTION of each term and
the bounds — that banking rises with giveback risk and falls with continuation
support, and that it can never leave the band. They do NOT claim the magnitudes
are right. Nothing here is fitted; there are zero resolved trades behind it.
Every decision carries `why` into the ledger precisely so a later analysis can
price each term from evidence rather than from this module's opinion.

    python3 -m pytest test_partial_policy.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.partial_policy import BASE, MAX, MIN, tp1_fraction


def test_the_base_applies_only_when_nothing_is_known():
    """MID / NORMAL / NEUTRAL are READINGS, not the absence of one — MID carries
    its own small -0.05 because a mid-maturity trend genuinely has more left in
    it than a mature one. Only an unrecognised state contributes nothing, and
    that is the case where the base stands alone."""
    p = tp1_fraction(trend_maturity="?", volatility_state="?", htf_alignment="?")
    assert p.fraction == pytest.approx(BASE)
    assert "no adjustment" in p.why


def test_the_default_arguments_are_a_mid_reading_not_a_neutral_one():
    """Guards the trap the line above describes: if someone later 'tidies' MID
    to 0.0 the policy silently loses its middle gradient."""
    assert tp1_fraction().fraction < BASE


# ------------------------------------------------ direction of each term

def test_an_exhausted_trend_banks_more_than_a_young_one():
    """The core hypothesis: EXHAUSTED is where MFE most often becomes giveback."""
    young = tp1_fraction(trend_maturity="YOUNG")
    old = tp1_fraction(trend_maturity="EXHAUSTED")
    assert old.fraction > young.fraction


def test_extreme_volatility_banks_more_than_quiet_tape():
    assert (tp1_fraction(volatility_state="EXTREME").fraction
            > tp1_fraction(volatility_state="LOW").fraction)


def test_aligned_structure_lets_a_with_trend_runner_breathe():
    assert (tp1_fraction(htf_alignment="ALIGNED", with_trend=True).fraction
            < tp1_fraction(htf_alignment="CONFLICTED", with_trend=True).fraction)


def test_alignment_flips_sign_against_the_trend():
    """The same ALIGNED reading that supports a with-trend runner argues for
    banking MORE against one — counter-trend into an aligned move is the desk's
    worst measured cohort, so the term flips rather than disappearing."""
    with_t = tp1_fraction(htf_alignment="ALIGNED", with_trend=True)
    against = tp1_fraction(htf_alignment="ALIGNED", with_trend=False)
    assert against.fraction > with_t.fraction
    assert "AGAINST" in against.why


def test_a_far_tp2_leaves_more_runner_on():
    """If TP2 is far, the runner IS the trade; banking heavily at the first
    objective throws away the part that pays."""
    near = tp1_fraction(rr_tp1=2.0, rr_tp2=2.2)     # 10% headroom
    far = tp1_fraction(rr_tp1=1.0, rr_tp2=3.0)      # 200% headroom
    assert far.fraction < near.fraction


def test_headroom_is_ignored_when_it_cannot_be_computed():
    """Missing R:R must not silently read as 'TP2 is close' — which would bank
    MORE on exactly the trades the desk knows least about."""
    p = tp1_fraction(rr_tp1=None, rr_tp2=None)
    assert p.fraction == pytest.approx(tp1_fraction().fraction)
    assert "TP2" not in p.why


# ---------------------------------------------------------- the bounds

def test_it_can_never_bank_less_than_the_floor():
    """A bank too small to change the trade's character is theatre."""
    p = tp1_fraction(trend_maturity="YOUNG", volatility_state="LOW",
                     htf_alignment="ALIGNED", with_trend=True,
                     rr_tp1=1.0, rr_tp2=4.0)
    assert p.fraction == pytest.approx(MIN)
    assert "clamped" in p.why


def test_it_can_never_bank_more_than_the_ceiling():
    """A bank so large the runner cannot pay for the give-up defeats having one."""
    p = tp1_fraction(trend_maturity="EXHAUSTED", volatility_state="EXTREME",
                     htf_alignment="ALIGNED", with_trend=False,
                     rr_tp1=2.0, rr_tp2=2.1)
    assert p.fraction == pytest.approx(MAX)
    assert "clamped" in p.why


def test_every_reachable_combination_stays_inside_the_band():
    """The band is what keeps a WRONG term from being catastrophic while it is
    still unpriced. Exhaustive, because the failure would be silent."""
    for mat in ("YOUNG", "MID", "MATURE", "EXHAUSTED", "UNKNOWN"):
        for vol in ("LOW", "NORMAL", "ELEVATED", "EXTREME", "UNKNOWN"):
            for al in ("ALIGNED", "NEUTRAL", "CONFLICTED", "UNKNOWN"):
                for wt in (True, False):
                    for rr in ((None, None), (1.0, 1.05), (1.0, 5.0), (2.0, 2.3)):
                        p = tp1_fraction(trend_maturity=mat, volatility_state=vol,
                                         htf_alignment=al, with_trend=wt,
                                         rr_tp1=rr[0], rr_tp2=rr[1])
                        assert MIN <= p.fraction <= MAX, (mat, vol, al, wt, rr, p)


def test_an_unknown_field_is_ignored_rather_than_guessed():
    """A state string this desk does not know must contribute nothing, not a
    default adjustment invented to fill the gap."""
    p = tp1_fraction(trend_maturity="SOMETHING_NEW", volatility_state="ODD",
                     htf_alignment="???")
    assert p.fraction == pytest.approx(BASE)


# ------------------------------------------------- it explains itself

def test_every_adjustment_names_itself_in_why():
    p = tp1_fraction(trend_maturity="EXHAUSTED", volatility_state="ELEVATED",
                     htf_alignment="ALIGNED", with_trend=True,
                     rr_tp1=1.78, rr_tp2=2.33)
    for token in ("maturity EXHAUSTED", "volatility ELEVATED", "HTF ALIGNED"):
        assert token in p.why, p.why


def test_the_2708_short_would_have_banked_above_the_base():
    """The trade that exposed all of this: EXHAUSTED maturity, TP2 barely beyond
    TP1. It should bank MORE than half, not less."""
    p = tp1_fraction(trend_maturity="EXHAUSTED", volatility_state="ELEVATED",
                     htf_alignment="ALIGNED", with_trend=True,
                     rr_tp1=1.78, rr_tp2=2.33)
    assert p.fraction > BASE


def test_the_version_travels_with_the_decision():
    """A fraction in the ledger with no policy version behind it cannot be
    re-read once the policy changes."""
    assert tp1_fraction().version.startswith("partial-")


def test_the_policy_cannot_refuse_or_resize_a_trade():
    """Source-level: it decides a BANK FRACTION and nothing else."""
    import ast
    tree = ast.parse((Path(__file__).parent / "golddesk" / "partial_policy.py")
                     .read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("Refusal", "compile_signal", "ev_gate", "current_stop",
                      "is_enforcing", "risk_r"):
        assert forbidden not in names, f"partial_policy references {forbidden!r}"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
