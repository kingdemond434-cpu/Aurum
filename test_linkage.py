"""The link is mandatory because the FDR denominator depends on it, and the runs
most likely to go unlinked are the ones that found nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from golddesk.linkage import (
    Invalidation, LinkedRegistry, OrphanRun, Run, config_hash, render)


def reg(hids=("H1", "H2")):
    r = LinkedRegistry()
    r.register_hypotheses(hids)
    return r


def run(rid="R1", hids=("H1",), outcome="SUPPORTS", cfg="c1"):
    return Run(rid=rid, hypothesis_ids=tuple(hids), kind="backtest",
               started_utc="2026-08-18T00:00:00+00:00", config_hash=cfg,
               n_observations=200, outcome=outcome)


# ------------------------------------------------------------ the weld

def test_a_run_naming_no_hypothesis_is_refused():
    """THE ENFORCEMENT. An unlinked run is an uncounted trial."""
    with pytest.raises(OrphanRun) as e:
        reg().register_run(run(hids=()))
    assert "uncounted" in str(e.value)


def test_a_run_pointing_at_an_undefined_hypothesis_is_refused():
    with pytest.raises(OrphanRun, match="unregistered"):
        reg().register_run(run(hids=("H99",)))


def test_there_is_no_flag_to_skip_the_link():
    """A skippable version would be skipped in exactly the circumstances that
    corrupt the count."""
    import inspect
    sig = inspect.signature(LinkedRegistry.register_run)
    assert "allow_unlinked" not in sig.parameters
    r = reg()
    with pytest.raises(OrphanRun):
        r.register_run(run(hids=()), allow_unknown_hid=True)


def test_a_legitimate_run_links_cleanly():
    r = reg()
    r.register_run(run())
    assert [x.rid for x in r.runs_for("H1")] == ["R1"]


def test_a_rerun_needs_its_own_id_because_it_is_a_new_trial():
    r = reg()
    r.register_run(run())
    with pytest.raises(ValueError, match="NEW trial"):
        r.register_run(run())


def test_one_run_may_test_several_hypotheses():
    r = reg()
    r.register_run(run(hids=("H1", "H2")))
    assert r.runs_for("H1") and r.runs_for("H2")


# --------------------------------------------------------------- the census

def test_every_run_counts_toward_the_denominator():
    r = reg(("H1",))
    for i, o in enumerate(("SUPPORTS", "CONTRADICTS", "INCONCLUSIVE")):
        r.register_run(run(rid=f"R{i}", outcome=o, cfg=f"c{i}"))
    assert r.trial_census()["trials_for_fdr"] == 3


def test_an_abandoned_run_is_still_a_trial():
    """A run stopped halfway because it looked unpromising is a peek at the
    data, and a peek is a trial. Excluding it is the same selection that makes a
    backtest look good."""
    r = reg(("H1",))
    r.register_run(run(rid="R1", outcome="SUPPORTS"))
    r.register_run(run(rid="R2", outcome="ABANDONED", cfg="c2"))
    c = r.trial_census()
    assert c["trials_for_fdr"] == 2
    assert c["by_outcome"]["ABANDONED"] == 1
    assert "peek is a trial" in c["note"]


def test_the_same_experiment_at_different_parameters_counts_twice():
    """One experiment re-run with a different parameter is a second trial
    however similar the prose."""
    r = reg(("H1",))
    r.register_run(run(rid="R1", cfg=config_hash({"lookback": 20})))
    r.register_run(run(rid="R2", cfg=config_hash({"lookback": 50})))
    assert r.trial_census()["distinct_configs"] == 2


def test_the_same_config_twice_is_still_two_runs():
    r = reg(("H1",))
    r.register_run(run(rid="R1", cfg="same"))
    r.register_run(run(rid="R2", cfg="same"))
    c = r.trial_census()
    assert c["trials_for_fdr"] == 2 and c["distinct_configs"] == 1


def test_config_hash_is_order_independent():
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


# ------------------------------------------------------------- the worklist

def test_an_untested_hypothesis_is_surfaced_as_a_worklist_not_an_error():
    r = reg(("H1", "H2"))
    r.register_run(run(hids=("H1",)))
    assert r.orphan_hypotheses() == ["H2"]
    ok, _ = r.audit()
    assert ok, "an untested claim is legitimate, not a broken record"
    assert "not an error; a worklist" in render(r).lower().replace("Not", "not")


# ------------------------------------------------------------ invalidation

def test_a_rejection_needs_a_run_behind_it():
    with pytest.raises(ValueError, match="is an opinion"):
        reg().invalidate("H1", "abc", "R_nonexistent", "it lost")


def test_an_invalidation_records_what_killed_it():
    r = reg(("H1",))
    r.register_run(run(outcome="CONTRADICTS"))
    inv = r.invalidate("H1", "hash1", "R1", "mean -0.3R over 180 post-seal trades",
                       "a quarter of positive out-of-sample evidence")
    assert inv.killed_by_run == "R1"
    assert "180 post-seal" in inv.render()


def test_an_invalidation_with_no_revisit_condition_fails_the_audit():
    """Without one, nobody knows what evidence would be enough, so the idea
    comes back by accident."""
    r = reg(("H1",))
    r.register_run(run(outcome="CONTRADICTS"))
    r.invalidate("H1", "hash1", "R1", "it lost")
    ok, why = r.audit()
    assert not ok and "revisit condition" in why


def test_the_render_says_so_when_nobody_stated_one():
    r = reg(("H1",))
    r.register_run(run(outcome="CONTRADICTS"))
    r.invalidate("H1", "hash1", "R1", "it lost")
    assert "NOT STATED" in render(r)


# ---------------------------------------------------------- resurrection

def test_re_proposing_a_killed_claim_surfaces_the_note():
    """Six weeks later somebody proposes it again in different words, it gets a
    fresh seal and a fresh chance to clear the bar by luck."""
    r = reg(("H1",))
    r.register_run(run(outcome="CONTRADICTS"))
    r.invalidate("H1", "claimhash", "R1", "mean -0.3R", "new venue")
    found = r.check_resurrection("claimhash")
    assert found is not None and found.killed_by_run == "R1"


def test_resurrection_reports_rather_than_blocks():
    """A genuine re-test after new data is legitimate; the requirement is that
    it be a decision, not an accident."""
    r = reg(("H1",))
    r.register_run(run(outcome="CONTRADICTS"))
    r.invalidate("H1", "claimhash", "R1", "e", "w")
    assert r.check_resurrection("claimhash") is not None
    assert r.check_resurrection("different") is None


def test_the_registry_uses_the_hypothesis_modules_own_hash():
    """Two implementations of 'is this the same claim' is how a duplicate slips
    through."""
    from golddesk.hypothesis import Hypothesis
    h = Hypothesis(hid="H1", statement="fades of strong trends lose",
                   selector={"health": "STRONG"}, predicted_sign=-1,
                   discovered_on="2026-08-01", seal_ts="2026-08-01T00:00:00+00:00",
                   discovery_n=40, discovery_mean_r=-0.2)
    r = reg(("H1",))
    r.register_run(run(outcome="CONTRADICTS"))
    r.invalidate("H1", h.content_hash(), "R1", "e", "w")
    assert r.check_resurrection(h.content_hash()) is not None


# ------------------------------------------------------------- persistence

def test_a_round_trip_preserves_the_census(tmp_path):
    r = reg(("H1", "H2"))
    r.register_run(run(rid="R1"))
    r.register_run(run(rid="R2", hids=("H2",), outcome="ABANDONED", cfg="c2"))
    r.invalidate("H2", "h2hash", "R2", "abandoned early", "more data")
    p = tmp_path / "link.json"
    r.save(p)
    back = LinkedRegistry.load(p)
    assert back.trial_census() == r.trial_census()
    assert back.check_resurrection("h2hash") is not None


def test_a_legacy_file_with_unlinked_runs_still_loads_so_it_can_be_audited(tmp_path):
    """Refusing to load is how a bad record becomes an invisible record."""
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps({
        "known_hids": ["H1"],
        "runs": [{"rid": "OLD", "hypothesis_ids": [], "kind": "backtest",
                  "started_utc": "2026-01-01T00:00:00+00:00"}],
        "invalidations": []}), encoding="utf-8")
    back = LinkedRegistry.load(p)
    assert back.unlinked_runs() == ["OLD"]
    ok, why = back.audit()
    assert not ok and "no hypothesis" in why


def test_a_dangling_reference_is_caught_by_the_audit(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({
        "known_hids": ["H1"],
        "runs": [{"rid": "R1", "hypothesis_ids": ["H_GONE"], "kind": "backtest",
                  "started_utc": "2026-01-01T00:00:00+00:00"}],
        "invalidations": []}), encoding="utf-8")
    ok, why = LinkedRegistry.load(p).audit()
    assert not ok and "unknown hypotheses" in why


def test_loading_a_missing_file_is_an_empty_registry_not_a_crash(tmp_path):
    assert LinkedRegistry.load(tmp_path / "nope.json").runs == {}


def test_a_clean_registry_passes_its_own_audit():
    r = reg(("H1",))
    r.register_run(run())
    ok, why = r.audit()
    assert ok and "welded" in why
