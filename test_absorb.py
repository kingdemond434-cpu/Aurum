"""A transfer channel fails by succeeding at the wrong thing: faithfully filing
every finding into a folder nobody reads. These tests are about the difference
between absorbing and storing.
"""
from __future__ import annotations

import pytest

from golddesk.absorb import (
    NON_TRANSFERABLE, NOTE, QUEUED, SEALED, TRANSFERRED, Absorber, Finding,
    absorption_value)


def f(statement="session range predicts continuation on XAUUSD",
      grade="E4", measured_on="XAUUSD H1 2018-2026",
      test="replicates on Aurum's own M15 ledger at ESS >= 30",
      source="mt5desk.hunt5"):
    return Finding(statement=statement, source=source, grade=grade,
                   measured_on=measured_on, transfer_test=test)


# ------------------------------------------------ absorbing is not storing

def test_a_finding_with_no_transfer_test_is_a_note_not_an_absorption():
    """THE CHECK THAT KEEPS THIS FROM BECOMING A FOLDER. A finding that names
    nothing Aurum could measure cannot be absorbed, only filed."""
    a = Absorber().queue(f(test=""))
    assert a.status == NOTE and "no transfer test stated" in a.reason


def test_a_low_grade_finding_is_not_worth_testing():
    a = Absorber().queue(f(grade="E1"))
    assert a.status == NOTE and "below E2" in a.reason


def test_an_ungraded_finding_is_recorded_not_weighted():
    """Absence of grading is how the loudest source wins by default."""
    a = Absorber().queue(f(grade="probably true"))
    assert a.status == NOTE and "not on the E0-E5 scale" in a.reason


def test_a_well_formed_finding_is_queued_not_applied():
    a = Absorber().queue(f())
    assert a.status == QUEUED


# ------------------------------------------- external evidence never outranks

def test_even_an_E5_finding_enters_at_zero_authority():
    """The contributor brief says it; this is where it becomes a type rather
    than a policy."""
    a = Absorber().queue(f(grade="E5"))
    assert a.status == QUEUED
    assert "ZERO authority" in a.reason
    assert "still only a hypothesis here" in a.reason


def test_the_reason_names_what_the_finding_was_measured_on():
    """A mechanism that worked on CADJPY is evidence about CADJPY."""
    a = Absorber().queue(f(measured_on="CADJPY asia"))
    assert "evidence about CADJPY asia" in a.reason


def test_sealing_takes_an_id_rather_than_minting_one():
    """The hypothesis book owns hypothesis identity; a second module inventing
    ids is how two registries drift apart."""
    ab = Absorber()
    fi = f()
    ab.queue(fi)
    a = ab.seal(fi, "H-2026-08-18-01")
    assert a.status == SEALED and a.hypothesis_id == "H-2026-08-18-01"
    assert "may not refuse a single trade" in a.reason


def test_an_unqueued_finding_cannot_be_sealed():
    with pytest.raises(ValueError, match="promoted past the check"):
        Absorber().seal(f(), "H1")


def test_a_note_cannot_be_sealed_by_going_around_the_queue():
    ab = Absorber()
    fi = f(test="")
    ab.queue(fi)
    with pytest.raises(ValueError):
        ab.seal(fi, "H1")


# ------------------------------------------ the negatives are what compound

def test_a_failed_transfer_is_recorded_permanently():
    """A loop that only records successes does not get smarter — it gets more
    confident, and re-runs the same failures forever."""
    ab = Absorber()
    fi = f()
    ab.queue(fi)
    a = ab.record_result(fi, transferred=False, evidence="ESS 41, mean -0.06R")
    assert a.status == NON_TRANSFERABLE
    assert "not tried again" in a.reason


def test_re_absorbing_a_dead_finding_returns_the_memory_not_a_fresh_queue():
    ab = Absorber()
    fi = f()
    ab.queue(fi)
    ab.record_result(fi, transferred=False, evidence="did not replicate")
    again = ab.queue(f())               # same claim, arriving again next cycle
    assert again.status == NON_TRANSFERABLE
    assert "already decided" in again.reason
    assert "re-runs its own failures forever" in again.reason


def test_a_successful_transfer_becomes_Aurums_own_finding():
    ab = Absorber()
    fi = f()
    ab.queue(fi)
    ab.seal(fi, "H9")
    a = ab.record_result(fi, transferred=True, evidence="ESS 55, mean +0.19R")
    assert a.status == TRANSFERRED
    assert a.hypothesis_id == "H9", "the hypothesis link survived the verdict"
    assert "held to Aurum's gate" in a.reason


def test_regrading_a_claim_does_not_let_it_back_in():
    """Otherwise the same idea returns every time somebody upgrades its label."""
    ab = Absorber()
    ab.queue(f(grade="E3"))
    ab.record_result(f(grade="E3"), transferred=False, evidence="no")
    assert ab.queue(f(grade="E5")).status == NON_TRANSFERABLE


def test_the_same_claim_measured_somewhere_else_IS_a_new_question():
    """Re-measuring on a different universe is genuinely new evidence."""
    ab = Absorber()
    ab.queue(f(measured_on="CADJPY"))
    ab.record_result(f(measured_on="CADJPY"), transferred=False, evidence="no")
    assert ab.queue(f(measured_on="XAUUSD")).status == QUEUED


def test_wording_changes_do_not_create_a_duplicate():
    ab = Absorber()
    ab.queue(f(statement="Session range predicts continuation on XAUUSD "))
    assert ab.queue(f(statement="session range predicts continuation on xauusd")).status \
        != QUEUED or len(ab.decisions) == 1


# ------------------------------------------------------------- reporting

def test_nothing_tested_is_reported_as_nothing_absorbed():
    ab = Absorber()
    ab.queue(f())
    assert "nothing has been absorbed, only queued" in ab.report()


def test_a_low_transfer_rate_is_named_as_the_honest_outcome():
    ab = Absorber()
    for i in range(4):
        fi = f(statement=f"claim {i}")
        ab.queue(fi)
        ab.record_result(fi, transferred=(i == 0), evidence="e")
    r = ab.report()
    assert "1/4 (25%)" in r
    assert "most findings from another universe are about that universe" in r


def test_the_report_lists_what_will_not_be_retried():
    ab = Absorber()
    fi = f(statement="COT positioning leads gold by two weeks")
    ab.queue(fi)
    ab.record_result(fi, transferred=False, evidence="no")
    assert "will not be retried" in ab.report()
    assert "COT positioning leads gold" in ab.report()


# ----------------------------------------------------------- persistence

def test_the_memory_survives_a_restart(tmp_path):
    """A channel that forgets on restart re-absorbs everything it ever killed."""
    ab = Absorber()
    fi = f()
    ab.queue(fi)
    ab.record_result(fi, transferred=False, evidence="ESS 41, -0.06R")
    p = tmp_path / "absorb.json"
    ab.save(p)
    back = Absorber.load(p)
    assert back.queue(f()).status == NON_TRANSFERABLE


def test_loading_a_missing_file_is_empty_not_an_error(tmp_path):
    assert Absorber.load(tmp_path / "nope.json").decisions == {}


# -------------------------------------------- was absorbing worth anything?

def test_a_thin_window_yields_no_claim():
    assert absorption_value([0.1] * 5, [0.2] * 5)["verdict"] == "INSUFFICIENT"


def test_sequential_windows_are_not_reported_as_an_estimate_of_value():
    """These are different periods and cannot be paired, so the difference
    includes whatever the market did between them."""
    out = absorption_value([0.1] * 40, [0.3] * 40)
    assert out["verdict"] == "OBSERVED"
    assert "not an estimate of what absorption was worth" in out["why"]
    assert "marginal-value test" in out["why"]
