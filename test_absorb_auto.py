"""The pipe that had never carried anything, and the rules it must not break.

Two properties decide whether this can be put on a timer and forgotten:

  IDEMPOTENCE  running it hourly forever adds each finding exactly once. A
               channel that re-absorbs is a channel that manufactures apparent
               corroboration out of one observation.

  RELEVANCE    a CADJPY result must not enter a gold desk. Importing it because
               the same code produced it is the cargo-culting absorb.py exists
               to prevent, and it is invisible downstream once absorbed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from golddesk.absorb_auto import scan, sync


def make_quant(tmp: Path, rows, machinery=None) -> Path:
    root = tmp / "quant"
    rep = root / "desks" / "mt5" / "reports"
    rep.mkdir(parents=True)
    (rep / "hunt99.json").write_text(json.dumps({"all": rows}), "utf-8")
    for name, blob in (machinery or {}).items():
        (rep / name).write_text(json.dumps(blob), "utf-8")
    return root


ROWS = [
    {"sym": "XAUUSD", "win": "asia", "state": "NORMAL_DAY", "gate": True,
     "t": 6.07, "defl": 3.1, "n": 760, "pf": 1.67},
    {"sym": "XAUUSD", "win": "asia", "state": "RANGE_DAY", "gate": False,
     "t": 3.41, "defl": 0.5, "n": 700, "pf": 1.2},
    {"sym": "CADJPY", "win": "asia", "state": "FAILED_BREAK", "gate": True,
     "t": 5.90, "defl": 2.9, "n": 690, "pf": 1.73},
    {"sym": "EURJPY", "win": "asia", "state": "NORMAL_DAY", "gate": True,
     "t": 5.24, "defl": 2.3, "n": 871, "pf": 1.55},
]


# ------------------------------------------------------------- relevance

def test_non_gold_findings_are_dropped(tmp_path):
    findings, dropped = scan(make_quant(tmp_path, ROWS))
    syms = {f.meta["cell"].split(".")[0] for f in findings}
    assert syms == {"XAUUSD"}, f"a non-gold instrument got through: {syms}"
    assert dropped == 2, "the drop count must be reported, not silent"


def test_silver_counts_as_relevant(tmp_path):
    """XAGUSD is the same complex and quant's gold work routinely spans both."""
    rows = ROWS + [{"sym": "XAGUSD", "win": "asia", "state": "TREND_DAY",
                    "gate": False, "t": -0.6, "defl": -3.5, "n": 500, "pf": 0.9}]
    findings, _ = scan(make_quant(tmp_path, rows))
    assert any(f.meta["cell"].startswith("XAGUSD") for f in findings)


def test_negative_results_are_carried_too(tmp_path):
    """The half that compounds: a closed door is worth carrying."""
    findings, _ = scan(make_quant(tmp_path, ROWS))
    assert any("FAILS" in f.statement for f in findings)
    assert any("CLEARS" in f.statement for f in findings)


def test_machinery_transfers_even_though_it_is_not_gold(tmp_path):
    """A risk rule is arithmetic, not a market claim."""
    root = make_quant(tmp_path, ROWS, machinery={
        "daily_stop.json": {"arms": [{"limit": 2.0, "dd": 0.388}]}})
    findings, _ = scan(root)
    assert any(f.source.endswith("daily_stop.json") for f in findings)


# ------------------------------------------------------------ idempotence

def test_running_twice_adds_nothing_the_second_time(tmp_path):
    root = make_quant(tmp_path, ROWS)
    state, journal = tmp_path / "s.json", tmp_path / "j.jsonl"
    first = sync(root, state, journal)
    second = sync(root, state, journal)
    assert first["new"] > 0
    assert second["new"] == 0
    assert second["already_known"] == first["new"]
    assert len(journal.read_text().strip().splitlines()) == first["new"]


def test_a_new_finding_still_gets_through_after_a_quiet_run(tmp_path):
    root = make_quant(tmp_path, ROWS)
    state, journal = tmp_path / "s.json", tmp_path / "j.jsonl"
    sync(root, state, journal)
    assert sync(root, state, journal)["new"] == 0
    rep = root / "desks" / "mt5" / "reports"
    rep.joinpath("hunt99.json").write_text(json.dumps({"all": ROWS + [
        {"sym": "XAUUSD", "win": "ny_open", "state": "TREND_DAY",
         "gate": True, "t": 5.0, "defl": 2.2, "n": 300, "pf": 1.4}]}), "utf-8")
    assert sync(root, state, journal)["new"] == 1


def test_dry_run_writes_nothing(tmp_path):
    root = make_quant(tmp_path, ROWS)
    state, journal = tmp_path / "s.json", tmp_path / "j.jsonl"
    out = sync(root, state, journal, dry_run=True)
    assert out["new"] > 0
    assert not state.exists() and not journal.exists()


# ------------------------------------------------------------- the refusals

def test_a_wrong_path_refuses_instead_of_absorbing_nothing(tmp_path):
    """Silence here is indistinguishable from 'nothing relevant was found'."""
    with pytest.raises(FileNotFoundError, match="repository root"):
        scan(tmp_path / "not-quant")


def test_every_finding_states_what_would_have_to_be_true_here(tmp_path):
    """A finding with no transfer test is a note, and notes change nothing."""
    findings, _ = scan(make_quant(tmp_path, ROWS))
    assert findings
    for f in findings:
        assert f.transfer_test.strip(), f"{f.statement[:60]} has no transfer test"
        assert f.measured_on.strip(), "measured_on is what stops cargo-culting"


def test_findings_record_what_they_were_measured_on(tmp_path):
    findings, _ = scan(make_quant(tmp_path, ROWS))
    assert all("XAU" in f.measured_on or "XAG" in f.measured_on
               for f in findings)
