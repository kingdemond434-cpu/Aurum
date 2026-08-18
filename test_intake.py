"""Tests for the automated hunt-to-shadow intake.

The load-bearing test is idempotence. This runs every morning against a file
that mostly does not change, and a re-registration bug would reset every shadow
clock daily — a pipeline that looks busy and can never promote anything.
"""
from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

from golddesk.intake import (budget_note, intake, promote_queue, read_sources,
                             record_day, run)
from golddesk.promotion import (MIN_SHADOW_DAYS, Status, load, save, screen,
                                to_shadow)


def _rows(n=3):
    return [{"cell": f"SYM{i}|fam|rr=2.0", "in_sample_sharpe": 1.0 + i * 0.1,
             "psr_raw": 0.99, "dsr_deflated": 0.1 * i,
             "n_trials_searched": 3168} for i in range(n)]


# ------------------------------------------------------------------ idempotence

def test_second_run_registers_nothing_new():
    book, added, skipped, _ = intake(_rows(3), [])
    assert len(added) == 3
    book, added2, skipped2, _ = intake(_rows(3), book)
    assert added2 == []
    assert len(skipped2) == 3
    assert len(book) == 3


def test_rerun_does_not_reset_the_shadow_clock():
    """THE ONE THAT MATTERS. A daily re-register would zero every counter."""
    book, _, _, _ = intake(_rows(1), [])
    promote_queue(book)
    for _ in range(40):
        record_day(book, {book[0].cell: 0.3})
    assert book[0].shadow_days == 40
    book, added, _, _ = intake(_rows(1), book)
    assert added == []
    assert book[0].shadow_days == 40, "re-intake wiped the forward record"


def test_new_cells_join_an_existing_book():
    book, _, _, _ = intake(_rows(2), [])
    book, added, skipped, _ = intake(_rows(4), book)
    assert len(added) == 2 and len(skipped) == 2
    assert len(book) == 4


# ------------------------------------------------------------------- screening

def test_below_threshold_never_enters_the_book():
    rows = [{"cell": "bad", "in_sample_sharpe": 0.2, "psr_raw": 0.10}]
    book, added, _, rejected = intake(rows, [])
    assert added == [] and rejected == ["bad"] and book == []


def test_deflation_does_not_block_entry():
    rows = [{"cell": "x", "in_sample_sharpe": 1.0, "psr_raw": 0.99,
             "dsr_deflated": 0.0, "n_trials_searched": 3168}]
    _, added, _, _ = intake(rows, [])
    assert len(added) == 1


def test_queue_order_follows_deflated_sharpe():
    book, _, _, _ = intake(_rows(3), [])
    started = promote_queue(book)
    assert [c.cell for c in started][0] == "SYM2|fam|rr=2.0"


def test_slots_limit_how_many_start_not_who_is_admitted():
    book, _, _, _ = intake(_rows(5), [])
    started = promote_queue(book, slots=2)
    assert len(started) == 2
    assert sum(1 for c in book if c.status is Status.CANDIDATE) == 3


# ---------------------------------------------------------------- forward days

def test_absent_cells_get_nothing_not_zero():
    """A day a sleeve did not trade is not a break-even day."""
    book, _, _, _ = intake(_rows(2), [])
    promote_queue(book)
    record_day(book, {book[0].cell: 0.5})
    assert book[0].shadow_days == 1
    assert book[1].shadow_days == 0


def test_candidates_do_not_accrue_days():
    book, _, _, _ = intake(_rows(1), [])
    record_day(book, {book[0].cell: 0.5})
    assert book[0].shadow_days == 0


def test_forward_evidence_promotes_through_the_daily_path():
    book, _, _, _ = intake(_rows(1), [])
    promote_queue(book)
    rng = random.Random(2)
    for _ in range(MIN_SHADOW_DAYS + 25):
        record_day(book, {book[0].cell: 0.30 + rng.gauss(0, 0.4)})
    assert book[0].status is Status.LIVE


def test_a_book_of_noise_promotes_almost_nothing():
    """THE CALIBRATION TEST, and it caught a real defect.

    The first version shadowed ONE noise cell and asserted it stayed put. It did
    not: it reached LIVE at t=+2.14 before review() retired it. At t>=1.5 a
    noise cell promotes about 6.7% of the time, so 40 of them promote two or
    three -- and being retired later is no comfort, they carried capital in
    between. promote_book applies Benjamini-Hochberg across the concurrent
    shadow set, which is what the forward gate was missing.
    """
    rows = [{"cell": f"noise{i}|fam|rr=2.0", "in_sample_sharpe": 1.0,
             "psr_raw": 0.99} for i in range(40)]
    book, _, _, _ = intake(rows, [])
    promote_queue(book)
    rng = random.Random(8)
    for _ in range(MIN_SHADOW_DAYS + 60):
        record_day(book, {c.cell: rng.gauss(0, 0.5) for c in book})
    live = sum(1 for c in book if c.status is Status.LIVE)
    assert live <= 2, f"{live} of 40 pure-noise cells reached LIVE"


def test_a_real_edge_still_promotes_among_noise():
    """The correction must not be so strict that nothing genuine gets through."""
    rows = [{"cell": f"n{i}|fam|rr=2.0", "in_sample_sharpe": 1.0,
             "psr_raw": 0.99} for i in range(20)]
    rows.append({"cell": "REAL|fam|rr=2.0", "in_sample_sharpe": 1.0,
                 "psr_raw": 0.99})
    book, _, _, _ = intake(rows, [])
    promote_queue(book)
    rng = random.Random(5)
    for _ in range(MIN_SHADOW_DAYS + 60):
        obs = {c.cell: rng.gauss(0, 0.5) for c in book}
        obs["REAL|fam|rr=2.0"] = 0.45 + rng.gauss(0, 0.4)
        record_day(book, obs)
    real = next(c for c in book if c.cell == "REAL|fam|rr=2.0")
    assert real.status is Status.LIVE, "FDR control refused a genuine edge"


# --------------------------------------------------------------------- sources

def test_missing_source_is_not_an_error():
    assert read_sources((Path("/nonexistent/x.json"),)) == []


def test_malformed_source_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.json"
        p.write_text("{not json", "utf-8")
        assert read_sources((p,)) == []


def test_rows_without_a_cell_are_ignored():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.json"
        p.write_text(json.dumps([{"in_sample_sharpe": 1.0}, {"cell": "ok"}]),
                     "utf-8")
        assert [r["cell"] for r in read_sources((p,))] == ["ok"]


# ------------------------------------------------------------- the daily driver

def test_run_persists_and_resumes():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "cand.json"
        src.write_text(json.dumps(_rows(2)), "utf-8")
        bp = Path(d) / "pipe.json"
        book, text = run(book_path=bp, sources=(src,))
        assert len(book) == 2
        assert "INTAKE" in text
        book2, _ = run(book_path=bp, sources=(src,), returns={})
        assert len(book2) == 2, "second run duplicated the book"


def test_run_with_no_sources_still_reports():
    with tempfile.TemporaryDirectory() as d:
        book, text = run(book_path=Path(d) / "p.json", sources=())
        assert book == []
        assert "NOTHING IS LIVE" in text


def test_run_posts_forward_returns():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "c.json"
        src.write_text(json.dumps(_rows(1)), "utf-8")
        bp = Path(d) / "p.json"
        run(book_path=bp, sources=(src,))
        cell = _rows(1)[0]["cell"]
        book, _ = run(book_path=bp, sources=(src,), returns={cell: 0.4})
        assert book[0].forward_r == [0.4]


# ---------------------------------------------------------------- budget note

def test_budget_is_reported_never_applied():
    rng = random.Random(3)
    days = [0.05 + rng.gauss(0, 0.5) for _ in range(400)]
    note = budget_note([], days)
    assert "NOT APPLIED" in note


def test_budget_note_handles_a_dead_book():
    assert "no expectancy" in budget_note([], [-0.4] * 200)
