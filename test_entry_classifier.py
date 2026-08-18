"""'His entries cluster near fair value gaps' is not a finding if fair value
gaps are everywhere. Almost every test here is about the null.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from golddesk.entry_classifier import (
    MIN_ENTRIES, Bar, classify, f_round_number, f_sweep, report)

UTC = timezone.utc
T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


def bars(n=4000, seed=0):
    rng = random.Random(seed)
    out, px = [], 2000.0
    for i in range(n):
        px += rng.gauss(0, 1.2)
        hi, lo = px + abs(rng.gauss(0, 1)), px - abs(rng.gauss(0, 1))
        out.append(Bar(T0 + timedelta(minutes=5 * i), px, hi, lo,
                       px + rng.gauss(0, 0.3)))
    return out


def test_random_entries_clear_no_feature():
    """THE TEST THAT MAKES A POSITIVE FINDING MEAN ANYTHING. If random entries
    score lift, the analysis confirms whatever it is handed."""
    b = bars()
    rng = random.Random(3)
    entries = [rng.choice(b[50:]).ts for _ in range(120)]
    hits = classify(entries, b, n_null=60)
    sig = [h for h in hits if h.significant]
    assert not sig, f"random entries 'clustered' on {[h.name for h in sig]}"


def test_planted_clustering_is_found():
    """It must be able to find something, or the refusals prove nothing."""
    b = bars()
    idx = [i for i in range(60, len(b) - 5) if f_sweep(b, i)]
    assert len(idx) >= MIN_ENTRIES, "fixture produced too few sweeps"
    hits = classify([b[i].ts for i in idx[:150]], b, n_null=60)
    sweep = next(h for h in hits if h.name == "liquidity_sweep")
    assert sweep.lift > 1.5 and sweep.p_value <= 0.05


def test_the_null_matches_weekday_and_hour():
    """A provider who trades only London against a null that trades all day
    differs on every feature for reasons unrelated to his trigger."""
    from golddesk.entry_classifier import _matched_null
    b = bars()
    entries = [x.ts for x in b if x.ts.hour == 13][:80]
    null = _matched_null(entries, b, seed=0)
    assert null and all(t.hour == 13 for t in null)


def test_a_control_feature_is_scored_alongside_the_real_ones():
    """Price spends a fixed fraction of its life near round numbers whatever the
    strategy."""
    b = bars()
    hits = classify([x.ts for x in b[100:400]], b, n_null=40)
    assert any("CONTROL" in h.name for h in hits)


def test_the_control_firing_invalidates_comparable_lifts():
    b = bars()
    rng = random.Random(5)
    idx = [i for i in range(60, len(b) - 5) if f_round_number(b, i)]
    if len(idx) >= MIN_ENTRIES:
        hits = classify([b[i].ts for i in idx[:150]], b, n_null=40)
        txt = report(hits)
        ctrl = next(h for h in hits if "CONTROL" in h.name)
        if ctrl.significant:
            assert "THE CONTROL FIRED" in txt


def test_too_few_entries_makes_no_claim():
    b = bars()
    hits = classify([x.ts for x in b[100:110]], b, n_null=20)
    assert all(h.p_value == 1.0 for h in hits)
    assert "required" in hits[0].why


def test_finding_nothing_is_reported_as_a_result():
    """It rules out the SMC trigger family as stated, which is worth knowing."""
    b = bars()
    rng = random.Random(9)
    entries = [rng.choice(b[50:]).ts for _ in range(120)]
    txt = report(classify(entries, b, n_null=60))
    assert "NO STRUCTURAL FEATURE CLEARS" in txt
    assert "That is a result" in txt


def test_a_positive_finding_refuses_to_claim_causation():
    b = bars()
    idx = [i for i in range(60, len(b) - 5) if f_sweep(b, i)]
    txt = report(classify([b[i].ts for i in idx[:150]], b, n_null=40))
    if "Clusters on" in txt:
        assert "CLUSTERING IS NOT CAUSATION" in txt


def test_entries_outside_the_bar_range_are_dropped_not_snapped():
    b = bars()
    early = [T0 - timedelta(days=10)] * 50
    hits = classify(early, b, n_null=20)
    assert hits[0].n == 0


def test_the_family_is_corrected_for_multiplicity():
    """Six features at p<=0.05 each is a ~26% chance one fires on noise. This
    module's own test caught exactly that — a random draw scored 'displacement'
    as significant before the correction existed."""
    from golddesk.entry_classifier import FAMILY_ALPHA, FEATURES
    b = bars()
    hits = classify([x.ts for x in b[100:400]], b, n_null=30)
    assert all(h.n_features == len(FEATURES) for h in hits)
    assert hits[0].alpha == pytest.approx(FAMILY_ALPHA / len(FEATURES))


def test_the_correction_is_stated_in_the_report():
    b = bars()
    txt = report(classify([x.ts for x in b[100:400]], b, n_null=30))
    assert "features tested" in txt and "~26%" in txt


def test_random_entries_clear_nothing_across_many_seeds():
    """The property the correction buys: not one lucky seed, but a rate."""
    b = bars()
    fired = 0
    for s in range(8):
        rng = random.Random(100 + s)
        entries = [rng.choice(b[50:]).ts for _ in range(120)]
        if any(h.significant for h in classify(entries, b, n_null=40, seed=s)):
            fired += 1
    assert fired <= 1, f"{fired}/8 random draws produced a 'finding'"
