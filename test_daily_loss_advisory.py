r"""A ruin control on a desk that holds no capital suppresses information, not risk.

MEASURED, 2026-08-28, on the live desk:

    dominant gate: 28/78 (36%) — 'deferred: portfolio heat: daily loss -3.19R at limit'

The single largest suppressor of output on the desk, and not one of those was
the analyst declining a setup. They were refusals to LOOK.

WHY IT IS WRONG HERE SPECIFICALLY. A daily-loss cap is ruin control, and ruin
control protects an account. Aurum has no account: it is an advisory desk that
sends signals to Telegram, and the operator decides what to take and at what
size. So the cap cannot prevent a loss. What it prevents is the operator SEEING
a setup — and it does that on exactly the day they have most reason to want the
information. The exposure is theirs and they are the only one who can manage it.

WHAT THIS IS NOT. It is not deleting the number. day_loss_r is still tracked,
still reported, still sizes through allocation.drawdown_scalar, and now rides in
the reason string so every signal sent past the cap is labelled. "Did signals
after a -3R day do worse" stays an answerable question instead of quietly
becoming an assumption — which it necessarily was while the cap refused to
generate the evidence that would settle it.

The switch flips back the moment anything here places its own orders.

    python3 -m pytest test_daily_loss_advisory.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.opportunity import Heat
from golddesk.runner import RiskLimits, RiskState, risk_check

DEEP_LOSS = -3.19          # the exact figure the live desk was refusing on


class _Sig:
    direction = "LONG"


# --------------------------------------------------------------------------
# It no longer refuses.

def test_a_deep_daily_loss_no_longer_blocks_the_universe_arm():
    ok, why = Heat().room_for([], 0, 1.0, DEEP_LOSS)
    assert ok, why


def test_a_deep_daily_loss_no_longer_blocks_the_live_arm():
    st = RiskState(day_loss_r=DEEP_LOSS)
    ok, why = risk_check(_Sig(), st, RiskLimits())
    assert ok, why


def test_both_arms_agree():
    """They must. The universe arm and the single-read arm are compared against
    each other, so a cap that binds in one and not the other would make the
    comparison measure the discrepancy rather than the reads."""
    assert Heat().daily_loss_blocks is RiskLimits().daily_loss_blocks is False


# --------------------------------------------------------------------------
# It still says so. This is the half that keeps it honest.

def test_the_signal_is_LABELLED_as_past_the_cap():
    """Not deleted, carried. The reason string lands in the ledger row, so a
    signal sent past the cap stays separable in every later analysis."""
    _, why = Heat().room_for([], 0, 1.0, DEEP_LOSS)
    assert "-3.19R" in why
    assert "ADVISORY" in why


def test_the_live_arm_labels_it_too():
    _, why = risk_check(_Sig(), RiskState(day_loss_r=DEEP_LOSS), RiskLimits())
    assert "-3.19R" in why and "ADVISORY" in why


def test_an_ordinary_day_carries_no_note():
    """The label must mean something. Appending it to every signal would make it
    invisible within a day."""
    _, why = Heat().room_for([], 0, 1.0, -0.5)
    assert "ADVISORY" not in why
    _, why2 = risk_check(_Sig(), RiskState(day_loss_r=-0.5), RiskLimits())
    assert "ADVISORY" not in why2


# --------------------------------------------------------------------------
# It comes back the moment capital is at stake.

def test_it_still_blocks_when_the_desk_holds_capital():
    """The switch is the whole argument: this is a property of an ADVISORY desk,
    not a view that daily-loss limits are wrong."""
    ok, why = Heat(daily_loss_blocks=True).room_for([], 0, 1.0, DEEP_LOSS)
    assert not ok
    assert "at limit" in why


def test_the_live_arm_blocks_when_armed_too():
    ok, why = risk_check(_Sig(), RiskState(day_loss_r=DEEP_LOSS),
                         RiskLimits(daily_loss_blocks=True))
    assert not ok
    assert "ruin limit" in why


# --------------------------------------------------------------------------
# Nothing else moved.

def test_portfolio_heat_still_binds():
    """Only the daily-loss line became advisory. Open risk is a live exposure
    the operator is already carrying, and it still refuses."""
    ok, why = Heat(max_open_risk_r=2.0).room_for([2.0], 0, 1.0, 0.0)
    assert not ok
    assert "would exceed" in why


def test_portfolio_heat_still_binds_past_the_daily_cap():
    """The advisory note must not smuggle a signal past a limit that DOES bind."""
    ok, why = Heat(max_open_risk_r=2.0).room_for([2.0], 0, 1.0, DEEP_LOSS)
    assert not ok
    assert "would exceed" in why
    assert "ADVISORY" in why, "the daily-loss state was dropped from the reason"


def test_the_number_still_sizes():
    """drawdown_scalar is ADVICE, not a gate — it tells the operator to go
    smaller after a bad day, which is information rather than a refusal. It must
    survive, or the change would have thrown away the useful half."""
    from golddesk.allocation import drawdown_scalar
    assert drawdown_scalar(DEEP_LOSS, 3.0) < drawdown_scalar(0.0, 3.0)


def test_day_loss_is_still_tracked():
    st = RiskState()
    st.day_loss_r += -1.5
    assert st.day_loss_r == -1.5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
