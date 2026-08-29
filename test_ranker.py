"""The daily ranker: does it correct itself, and does it refuse to?

    python3 -m pytest test_ranker.py -q

THE TWO FAILURES THIS FILE IS AIMED AT, and they point in opposite directions.

The first is the one the desk measured and asked for: taken trades resolve
-0.14R while refusals reached +0.56R, so SOMETHING has to reorder candidates on
evidence. A ranker that never fires is the fault left in place.

The second is the failure a daily self-tuner invites and which is far easier to
ship: fitting fourteen trades every morning, producing a different ordering each
day, and calling the resulting noise "learning". Most of what is asserted below
is that the ranker REFUSES — on sample, on Holm across everything tested, on the
sample's own cost of expressing a difference, and on three consecutive days at
one sign before anything reaches live ordering.

The single most important assertion in the file is the one that says the sort
key is unchanged when nothing has been measured. That is the state the desk is
in today, and it is what makes this safe to run live from the first minute.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import aurum_cycle
from golddesk import ranker
from golddesk.constitution import REGISTRY


# ------------------------------------------------------------------ fixtures

def sig(t0: str, **decision) -> dict:
    return {"kind": "SIGNAL", "t0": t0, "decision": decision}


def closed(t0: str, r: float, **extra) -> dict:
    return {"kind": "TRADE_CLOSED", "entry_t0": t0, "realised_r": r, **extra}


def planted(n: int = 40, low_r: float = -0.5, high_r: float = 1.0) -> list[dict]:
    """`evidence_net` perfectly separates outcomes. As strong as a signal gets."""
    rows: list[dict] = []
    for i in range(n):
        t0 = f"2026-08-{i:02d}T00:00:00Z"
        net = -3 if i < n // 2 else 3
        rows.append(sig(t0, evidence_balance={"net": net}))
        rows.append(closed(t0, low_r if i < n // 2 else high_r))
    return rows


# ------------------------------------------------------- it refuses thin data

def test_empty_ledger_is_unmeasured_and_orders_nothing():
    rep = ranker.measure([])
    assert rep.verdict == "UNMEASURED"
    art = ranker.advance(None, rep, "2026-08-29")
    assert art["used"] == []
    assert ranker.from_artifact(art) is ranker.EMPTY or not ranker.from_artifact(art).active
    # And the word appears, because absence must not read as a clean verdict.
    assert "UNMEASURED" in ranker.render(art)


def test_a_huge_effect_on_a_thin_sample_still_qualifies_nothing():
    """The whole point. A perfect separation on 14 trades is not a finding."""
    rows = planted(n=14)
    rep = ranker.measure(rows)
    assert rep.n_resolved == 14
    assert rep.verdict == "UNMEASURED"
    assert not any(r.qualified for r in rep.results)
    assert "under 30" in (rep.get("evidence_net").reason or "")


def test_a_real_effect_smaller_than_its_own_cost_is_refused():
    """Perfect separation, vanishing size. Statistically certain, economically nothing."""
    rows: list[dict] = []
    for i in range(40):
        t0 = f"t{i}"
        rows.append(sig(t0, evidence_balance={"net": -1 if i < 20 else 1},
                        cost_r=0.12))
        rows.append(closed(t0, 0.0 if i < 20 else 0.05))
    res = ranker.measure(rows).get("evidence_net")
    assert res.tested and res.holm_ok            # the statistics are not in doubt
    assert not res.economic_ok                   # the economics are
    assert not res.qualified
    assert "cost of expressing it" in res.reason


def test_pure_noise_qualifies_nothing():
    rows: list[dict] = []
    for i in range(40):
        t0 = f"t{i}"
        rows.append(sig(t0, evidence_balance={"net": -1 if i < 20 else 1}))
        rows.append(closed(t0, 0.5 if i % 2 else -0.5))
    res = ranker.measure(rows).get("evidence_net")
    assert res.tested and not res.qualified


def test_unobserved_paths_are_excluded_like_everywhere_else():
    """A quarantined row carries a realised_r that measured nothing."""
    rows = planted(n=40)
    for r in rows:
        if r["kind"] == "TRADE_CLOSED":
            r["evidence_valid"] = False
    assert ranker.measure(rows).n_resolved == 0


# --------------------------------------------------- it does fire, eventually

def test_a_strong_measured_feature_qualifies_but_is_not_used_on_day_one():
    rep = ranker.measure(planted())
    res = rep.get("evidence_net")
    assert res.qualified and res.sign == 1
    art = ranker.advance(None, rep, "2026-08-29")
    assert art["features"]["evidence_net"]["streak"] == 1
    assert art["features"]["evidence_net"]["used"] is False
    assert art["used"] == []                     # stability bar not yet cleared


def test_three_consecutive_days_at_one_sign_promote_it():
    rep = ranker.measure(planted())
    art = None
    for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
        art = ranker.advance(art, rep, day)
    assert art["features"]["evidence_net"]["streak"] == ranker.MIN_STREAK_DAYS
    assert art["used"] == ["evidence_net"]
    r = ranker.from_artifact(art)
    assert r.active
    assert r.score({"evidence_net": 3.0}) == 1
    assert r.score({"evidence_net": -3.0}) == -1
    assert r.score({"evidence_net": None}) == 0          # absence votes nothing
    assert r.score({}) == 0


def test_rerunning_the_same_day_cannot_walk_a_feature_into_use():
    """`--force` three times before lunch must not promote anything."""
    rep = ranker.measure(planted())
    art = ranker.advance(None, rep, "2026-08-29")
    for _ in range(5):
        art = ranker.advance(art, rep, "2026-08-29")
    assert art["features"]["evidence_net"]["streak"] == 1
    assert art["used"] == []


def test_a_sign_flip_resets_the_streak_rather_than_continuing_it():
    up = ranker.measure(planted(low_r=-0.5, high_r=1.0))
    down = ranker.measure(planted(low_r=1.0, high_r=-0.5))
    art = ranker.advance(None, up, "2026-08-27")
    art = ranker.advance(art, up, "2026-08-28")
    assert art["features"]["evidence_net"]["streak"] == 2
    art = ranker.advance(art, down, "2026-08-29")
    assert art["features"]["evidence_net"]["sign"] == -1
    assert art["features"]["evidence_net"]["streak"] == 1
    assert art["used"] == []


def test_a_day_that_fails_to_qualify_breaks_the_streak():
    good = ranker.measure(planted())
    art = ranker.advance(None, good, "2026-08-27")
    art = ranker.advance(art, good, "2026-08-28")
    art = ranker.advance(art, ranker.measure([]), "2026-08-29")
    assert art["features"]["evidence_net"]["streak"] == 0


# -------------------------------------------------- what it may and may not do

def test_a_feature_computed_after_selection_is_measured_but_never_used():
    """III.16 in the honest direction: report the gap, do not paper it over."""
    rows: list[dict] = []
    for i in range(40):
        t0 = f"t{i}"
        rows.append(sig(t0, evidence_tier={"rank": 1 if i < 20 else 4}))
        rows.append(closed(t0, -0.5 if i < 20 else 1.0))
    art = ranker.advance(None, ranker.measure(rows), "2026-08-29")
    for day in ("2026-08-30", "2026-08-31"):
        art = ranker.advance(art, ranker.measure(rows), day)
    assert art["features"]["tier_rank"]["qualified"] is True
    assert art["features"]["tier_rank"]["used"] is False
    assert art["unwired"] == ["tier_rank"]
    assert "MEASURED BUT UNWIRED" in ranker.render(art)
    assert not ranker.from_artifact(art).active


def test_an_artifact_claiming_an_uncomputable_feature_is_ignored():
    art = {"version": "x", "day": "d", "features": {
        "tier_rank": {"used": True, "split": 2.0, "sign": 1, "scoreable": False}}}
    assert not ranker.from_artifact(art).active


@pytest.mark.parametrize("bad", [None, {}, {"features": "nonsense"},
                                 {"features": {"confidence": {"used": True}}},
                                 {"features": {"confidence": {"used": True,
                                                              "split": None,
                                                              "sign": 1}}}])
def test_a_malformed_artifact_degrades_to_no_ranking(bad):
    assert not ranker.from_artifact(bad).active


def test_a_corrupt_file_on_disk_never_raises(tmp_path):
    p = tmp_path / "ranker.json"
    p.write_text("{not json", encoding="utf-8")
    assert not ranker.load(p).active
    assert not ranker.load(tmp_path / "absent.json").active


def test_publish_then_load_round_trips(tmp_path):
    rep = ranker.measure(planted())
    art = None
    for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
        art = ranker.advance(art, rep, day)
    p = tmp_path / "ranker.json"
    ranker.publish(art, p)
    assert json.loads(p.read_text(encoding="utf-8"))["used"] == ["evidence_net"]
    assert ranker.load(p).active


# ------------------------------------------------------- the live ordering seam

def _cands():
    from golddesk.universe import Candidate
    from golddesk.analyst import AnalystRead, Setup

    def one(idx, mech, rr, net, base):
        read = AnalystRead(setup=Setup.NOVEL, direction="LONG", confidence=3,
                           mechanism_name=mech, entry_ref="E", stop_ref="S",
                           tp1_ref="T1", tp2_ref="T2", read="r", why="x",
                           why_not="z", invalidation="y")
        c = Candidate(idx, read, _Compiled(rr, base), None, None, "unmeasured")
        c.rank_features = {"evidence_net": net}
        return c
    # NON-OVERLAPPING BANDS on purpose. Two candidates in the same band are one
    # idea and get deferred as redundant, which would mean these tests never
    # reached the ordering they exist to exercise.
    return [one(0, "aaa", 3.0, -3.0, 100.0), one(1, "bbb", 2.0, 3.0, 200.0)]


class _Compiled:
    """Just enough geometry for the sort key and the redundancy zone."""
    def __init__(self, rr, base=100.0):
        self.rr_tp2, self.direction = rr, "LONG"
        self.entry, self.stop = base, base - 1.0
        self.tp1, self.tp2 = base + 1.0, base + 2.0
        self.risk, self.cost_r = 1.0, 0.05


def test_with_no_ranking_the_order_is_exactly_what_it_was():
    from golddesk.universe import _sort_key
    a, b = _cands()
    assert a.rank_votes == b.rank_votes == 0
    # Higher R:R first, which is the declared tiebreak and nothing else.
    assert sorted([b, a], key=_sort_key)[0] is a


def test_a_measured_ranking_reorders_and_says_that_it_did():
    from golddesk.opportunity import Heat
    from golddesk.universe import select
    rep = ranker.measure(planted())
    art = None
    for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
        art = ranker.advance(art, rep, day)
    r = ranker.from_artifact(art)

    heat = Heat(max_open_risk_r=1.0)
    sel = select(_cands(), heat, max_concurrent=1, ranking=r)
    assert [c.index for c in sel.taken] == [1]      # the one the evidence favours
    assert sel.budget_bound and sel.ranking_used
    assert "MEASURED RANKING WAS LOAD-BEARING" in sel.render()
    j = sel.to_journal()
    assert j["ranking_used"] is True
    assert j["candidates"][1]["rank_votes"] == 1
    assert j["candidates"][0]["rank_votes"] == -1


def test_ranking_that_scores_everything_alike_is_not_billed_as_decisive():
    from golddesk.opportunity import Heat
    from golddesk.universe import select
    rep = ranker.measure(planted())
    art = None
    for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
        art = ranker.advance(art, rep, day)
    cands = _cands()
    for c in cands:
        c.rank_features = {"evidence_net": 3.0}
    sel = select(cands, Heat(max_open_risk_r=1.0), max_concurrent=1,
                 ranking=ranker.from_artifact(art))
    assert sel.budget_bound and not sel.ranking_used


def test_votes_never_refuse_anything():
    """Frequency is untouched: with room, everything positive is still taken."""
    from golddesk.opportunity import Heat
    from golddesk.universe import select
    rep = ranker.measure(planted())
    art = None
    for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
        art = ranker.advance(art, rep, day)
    sel = select(_cands(), Heat(max_open_risk_r=10.0), max_concurrent=4,
                 ranking=ranker.from_artifact(art))
    assert len(sel.taken) == 2
    assert not sel.budget_bound and not sel.ranking_used


def test_features_are_computed_the_same_way_the_ledger_records_them():
    from types import SimpleNamespace
    ctx = SimpleNamespace(trend_direction="UP", trend_health="STRONG",
                          displacement_state="CONFIRMED")
    f = ranker.features_for(SimpleNamespace(confidence=4, direction="LONG"),
                            _Compiled(2.5), ctx)
    assert f["confidence"] == 4.0 and f["rr_tp2"] == 2.5 and f["cost_r"] == 0.05
    from golddesk.contradiction import weigh
    assert f["evidence_net"] == float(weigh("LONG", ctx).net)


def test_an_unmeasurable_context_yields_none_and_not_zero():
    from types import SimpleNamespace
    f = ranker.features_for(SimpleNamespace(confidence=2, direction="LONG"),
                            _Compiled(1.8), SimpleNamespace())
    assert f["evidence_net"] is None


# ------------------------------------------------------------------ it is WIRED

def test_the_ranker_runs_daily():
    assert any(n == "ranker" for n, _ in aurum_cycle.STEPS)


def test_the_ranker_runs_before_the_step_that_grades_selection():
    names = [n for n, _ in aurum_cycle.STEPS]
    assert names.index("ranker") < names.index("missed_money")
    assert names.index("cohorts") < names.index("ranker")


def test_the_cycle_step_publishes_an_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(ranker, "ARTIFACT", tmp_path / "ranker.json")
    ctx = {"rows": planted(), "as_of": "2026-08-29"}
    out = aurum_cycle.step_ranker(ctx)
    assert (tmp_path / "ranker.json").exists()
    assert "RANKING" in out and ctx["ranker"]["n_resolved"] == 40


def test_the_ordering_is_a_registered_restriction():
    ids = {r.id for r in REGISTRY}
    assert "entry.rank_votes" in ids
    r = next(x for x in REGISTRY if x.id == "entry.rank_votes")
    assert r.site == "ranker:score"
