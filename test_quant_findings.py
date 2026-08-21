"""The absorption channel actually absorbs, rather than filing a note nobody reads.

external/channels.txt and external/signals.jsonl sat at zero bytes while quant
produced real, measured, XAUUSD-specific results. This proves the channel is no
longer empty: both findings QUEUE through the real intake path and both SEAL
into real, matchable Hypotheses -- the second one used to stay honestly QUEUED
because Aurum had no prior-NY-session-state field to select on; golddesk.day_state
closed that gap, so both seal now.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from golddesk.absorb import SEALED
from golddesk.hypothesis import HypothesisBook
from golddesk.quant_findings import (
    TREND_STRENGTH_SELECTOR, XAUUSD_ASIA_NORMAL_DAY_FINDING,
    XAUUSD_ASIA_NORMAL_DAY_SELECTOR, apply, strength_bucket)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import golddesk.quant_findings as qf
    monkeypatch.setattr(qf, "FINDINGS_LOG", tmp_path / "findings.json")
    monkeypatch.setattr(qf, "HYPOTHESIS_BOOK_PATH", tmp_path / "hyp.json")
    return tmp_path


def test_both_findings_are_decided_not_ignored(isolated):
    ab = apply()
    assert len(ab.decisions) == 2
    statuses = {a.status for a in ab.decisions.values()}
    assert statuses == {SEALED}


def test_the_trend_finding_seals_into_a_real_matchable_hypothesis(isolated):
    apply()
    book = HypothesisBook(isolated / "hyp.json")
    assert len(book.items) == 2
    h = next(h for h in book.items.values() if h.selector == TREND_STRENGTH_SELECTOR)
    assert h.predicted_sign == 1
    # A real ledger-row shape, not a synthetic one built to pass.
    assert h.matches({}, {"trend_strength_bucket": "high", "realised_r": 0.8})
    assert not h.matches({}, {"trend_strength_bucket": "low", "realised_r": 0.8})
    assert not h.matches({}, {"realised_r": 0.8})   # key entirely absent


def test_the_asia_finding_now_seals_into_a_real_matchable_hypothesis(isolated):
    """This one used to stay QUEUED -- Aurum had no field to select on. Now
    golddesk.day_state provides it, so sealing it is safe rather than dead."""
    ab = apply()
    decided = ab.already_decided(XAUUSD_ASIA_NORMAL_DAY_FINDING)
    assert decided.status == SEALED
    assert decided.hypothesis_id is not None

    book = HypothesisBook(isolated / "hyp.json")
    h = next(h for h in book.items.values()
             if h.selector == XAUUSD_ASIA_NORMAL_DAY_SELECTOR)
    assert h.predicted_sign == 1
    assert h.matches({}, {"prior_ny_session_state": "NORMAL_DAY", "realised_r": 0.3})
    assert not h.matches({}, {"prior_ny_session_state": "RANGE_DAY", "realised_r": 0.3})
    assert not h.matches({}, {"realised_r": 0.3})   # key entirely absent


def test_reapplying_is_idempotent(isolated):
    """The whole point of content-hashing: re-running must not re-seal, spawn
    a second hypothesis, or otherwise treat the same finding as new."""
    apply()
    book_after_first = HypothesisBook(isolated / "hyp.json")
    n1 = len(book_after_first.items)

    ab2 = apply()
    book_after_second = HypothesisBook(isolated / "hyp.json")
    assert len(book_after_second.items) == n1
    assert len(ab2.decisions) == 2


def test_strength_bucket_matches_the_selector_it_feeds():
    """The function a real caller uses to label a ledger row, and the selector
    that has to recognise that label, must agree -- tested against each other
    rather than each independently looking right."""
    assert strength_bucket(0.85) == TREND_STRENGTH_SELECTOR["trend_strength_bucket"]
    assert strength_bucket(0.9) == "high"
    assert strength_bucket(0.6) == "medium"
    assert strength_bucket(0.4) == "low"
    assert strength_bucket(0.1) == "none"
    assert strength_bucket(0.0) == "none"


def test_apply_writes_a_persisted_record_that_reloads(isolated):
    apply()
    from golddesk.absorb import Absorber
    reloaded = Absorber.load(isolated / "findings.json")
    assert len(reloaded.decisions) == 2
