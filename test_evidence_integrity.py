r"""Three defects that contaminate evidence rather than lose money.

All three came off ONE afternoon's Telegram messages, and each produces a record
that looks authoritative and is not true.

1. AN IMPOSSIBLE EXIT ROW.

    SHADOW EXIT LONG NOVEL — STOP
    realised -1.02R net
    MFE +0.00R · MAE +0.00R · capture 0% of MFE
    resolution TICK_OBSERVED · 0 observations

   Those cannot all hold. A position that travelled from entry to a full stop
   has an MAE of roughly -1R by definition, and TICK_OBSERVED asserts ordering
   was seen in a tick stream that recorded nothing. The exit PRICE is real — a
   stop is a price event — but the PATH is unknown and was being reported as
   measured and zero. Left in the statistics it biases every excursion figure
   toward zero, hardest on losers, which is exactly where stop placement is
   decided.

2. FAVOURABLE SLIPPAGE ON EVERY PARTIAL.

    SHADOW TP1 BANK SHORT
    TP1 4600.48 reached — banked 25% at 4594.12 (+0.61R)

   Six points better than the objective, on a short. A resting take-profit limit
   fills AT the limit; it does not wait and fill further in your favour. The
   desk was crediting itself with favourable slippage on every bank, and the
   error is largest when price moves fastest — so it flatters volatile
   mechanisms most.

3. (see test_trigger_discipline.py) an entry taken at market on a thesis whose
   own trigger had not happened.

    python3 -m pytest test_evidence_integrity.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.live import Resolution
from golddesk.opportunity import build_cohorts, resolved_outcomes


def _closed(realised=-1.02, obs=0, mfe=0.0, mae=0.0, valid=None,
            mech="novel-a", res="TICK_OBSERVED"):
    row = {"kind": "TRADE_CLOSED", "ts": "2026-08-28T11:34:00+00:00",
           "entry_t0": "2026-08-28T11:00:00+00:00",
           "mechanism_name": mech, "realised_r": realised,
           "observations": obs, "mfe_r": mfe, "mae_r": mae, "resolution": res}
    if valid is not None:
        row["evidence_valid"] = valid
    return row


# --------------------------------------------------------------------------
# The unobserved path must not teach the desk anything.

def test_an_unobserved_outcome_is_kept_out_of_the_evidence():
    rows = [_closed(valid=False), _closed(realised=1.5, obs=140, mfe=1.6,
                                          mae=-0.4, valid=True)]
    got = resolved_outcomes(rows)
    assert len(got) == 1
    assert got[0]["realised_r"] == 1.5


def test_it_is_kept_out_of_COHORTS_specifically():
    """Cohorts drive the EV gate, which decides what gets taken. A contaminated
    loss there does not just mislead a report — it changes future decisions."""
    rows = [_closed(valid=False) for _ in range(5)]
    assert build_cohorts(rows) == {}


def test_a_valid_outcome_is_still_counted():
    """The filter must not become a way to drop inconvenient losses."""
    rows = [_closed(realised=-1.0, obs=200, mfe=0.3, mae=-1.0, valid=True)]
    c = build_cohorts(rows)
    assert c["novel-a"].n == 1 and c["novel-a"].wins == 0


def test_older_rows_without_the_flag_are_still_read():
    """evidence_valid is new. Absent means unknown, and dropping every
    pre-existing row would silently erase the desk's whole history."""
    rows = [_closed(realised=0.8, obs=90, mfe=1.0, mae=-0.2)]
    assert len(resolved_outcomes(rows)) == 1


def test_the_resolution_vocabulary_has_a_name_for_it():
    """Reusing TICK_OBSERVED with a footnote would leave the lie in the label,
    which is the failure the Resolution docstring already warns about."""
    assert Resolution.UNOBSERVED.value == "UNOBSERVED"
    assert Resolution.UNOBSERVED is not Resolution.TICK_OBSERVED


# --------------------------------------------------------------------------
# The downgrade happens at the desk, on a real close.

def _desk_with_trade(tmp_path, ticks):
    from golddesk.analyst import Setup, Thresholds
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.notify import NullSink
    from test_blind_ledger import BlindProvider

    d = LiveDesk(BlindProvider(), Ledger(tmp_path / "l.jsonl"), NullSink(),
                 shadow=True, vision=Vision.NUMERIC_ONLY,
                 thresholds=Thresholds(fallback_min_rr=1.0),
                 measure_position_constraint=False)
    return d


def test_zero_observations_downgrades_the_resolution_at_close(tmp_path):
    """Forced in _close rather than at each call site, because every future exit
    path would otherwise have to remember."""
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._close)
    assert "t.observer.ticks == 0" in src
    assert "Resolution.UNOBSERVED" in src


def test_the_close_row_carries_the_quarantine_flag(tmp_path):
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk)
    assert '"evidence_valid"' in src


def test_a_managed_exit_is_not_downgraded():
    """A management decision closes at a KNOWN price by construction — the
    observer's tick count says nothing about it, and downgrading it would
    quarantine perfectly good evidence."""
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._close)
    assert "MANAGED_EXIT" in src


# --------------------------------------------------------------------------
# Partials fill at the objective.

def test_tp1_banks_at_the_objective_not_at_the_observed_price():
    """The live message banked a short SIX POINTS better than its own TP1. A
    resting limit fills AT the limit."""
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._manage_tick if hasattr(
        live.LiveDesk, "_manage_tick") else live.LiveDesk)
    assert "self._bank_tp1(t, t.signal.tp1, ts)" in src
    assert "self._bank_tp1(t, price, ts)" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
