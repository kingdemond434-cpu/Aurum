"""Precedent reaches the reasoner, and is labelled as precedent.

    python3 -m pytest test_memory_pack.py -q

THE GAP. The brief described the present in full and the past not at all. The
desk holds a ledger of every state it has traded and what the market then did,
and the only part of it reaching the reasoning layer was a scalar coverage
score — which cannot say WHICH trades, what happened in them, or where they
differ from now. A coverage number is a warning light; a memory pack is
evidence.

THE DISCIPLINE, and it is the reason most of these tests exist. Eight retrieved
analogues are eight anecdotes. They were SELECTED for resembling the present, so
any percentage taken from them is selection on the neighbours of the thing being
predicted and is worth nothing. The block says so every time it prints, and the
test below asserts that it does — because the moment somebody reads "5 of 8
worked" off this block, it has made the desk worse rather than better.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.memory_pack import K, MIN_SIMILARITY, build


def ctx(direction="UP", health="STRONG", vol="NORMAL", session="NY",
        maturity="MID", disp="CONFIRMED", sweep="NONE", reclaim="CONFIRMED",
        pull="MEDIUM", dist="MID", align="ALIGNED"):
    return {"trend_direction": direction, "trend_health": health,
            "volatility_state": vol, "session": session,
            "trend_maturity": maturity, "displacement_state": disp,
            "sweep_state": sweep, "reclaim_state": reclaim,
            "pullback_depth": pull, "distance_from_session_extreme": dist,
            "htf_alignment": align}


def sig(t0, context):
    return {"kind": "SIGNAL", "t0": t0, "decision": {"analyst_read": {}},
            "context": context}


def closed(t0, r, context, mech="m", reason="TARGET", mfe=1.8, mae=-0.4,
           valid=True):
    return {"kind": "TRADE_CLOSED", "ts": t0, "entry_t0": t0, "realised_r": r,
            "mfe_r": mfe, "mae_r": mae, "reason": reason, "context": context,
            "mechanism_name": mech, "direction": "LONG", "setup": "CONTINUATION",
            "evidence_valid": valid}


# ------------------------------------------------------------- it retrieves

def test_the_most_similar_states_come_back_first():
    now = ctx()
    rows = [closed("t-far", -1.0, ctx(direction="DOWN", health="WEAK",
                                      vol="EXTREME", session="ASIA",
                                      align="CONFLICTED")),
            closed("t-near", 2.0, ctx())]
    pack = build(now, rows)
    assert not pack.empty
    assert pack.analogues[0].when == "t-near"
    assert pack.analogues[0].similarity > 0.9


def test_a_dissimilar_state_is_not_offered_as_precedent():
    """Below the bar it is not a precedent, it is a different market with some
    fields that happen to agree."""
    now = ctx()
    far = ctx(direction="DOWN", health="WEAK", vol="EXTREME", session="ASIA",
              maturity="EXHAUSTED", disp="NONE", reclaim="NONE", pull="NONE",
              dist="FAR", align="CONFLICTED")
    pack = build(now, [closed("t", 1.0, far)])
    assert pack.empty
    assert pack.n_comparable == 1, "it was compared and then rejected, not skipped"


def test_no_precedent_says_so_rather_than_going_quiet():
    pack = build(ctx(), [])
    assert pack.empty
    text = pack.render()
    assert "PRECEDENT: none" in text
    assert "not a reason to refuse" in text


def test_the_pack_is_capped():
    rows = [closed(f"t{k}", 1.0, ctx()) for k in range(30)]
    assert len(build(ctx(), rows).analogues) == K


def test_a_quarantined_trade_is_never_offered_as_precedent():
    """Its mfe and mae are zeros that were never measured."""
    rows = [closed("t", 1.0, ctx(), valid=False)]
    assert build(ctx(), rows).empty


# --------------------------------------------- it carries what makes it usable

def test_each_analogue_carries_what_happened_and_how_far_it_went():
    pack = build(ctx(), [closed("2026-08-01T10:00:00", -0.4, ctx(),
                                reason="MANAGED_EXIT", mfe=1.9, mae=-0.7)])
    a = pack.analogues[0]
    assert a.realised_r == -0.4 and a.mfe_r == 1.9 and a.mae_r == -0.7
    line = a.line
    assert "-0.40R via MANAGED_EXIT" in line
    assert "+1.90R best" in line and "-0.70R worst" in line


def test_the_dimensions_that_differ_are_named():
    """The part that stops a superficial resemblance being read as a match."""
    then = ctx(maturity="YOUNG", pull="SHALLOW")
    pack = build(ctx(maturity="EXHAUSTED", pull="DEEP"), [closed("t", 1.0, then)])
    assert not pack.empty
    d = pack.analogues[0].differs
    assert any("trend_maturity" in x for x in d)
    assert any("pullback_depth" in x for x in d)
    assert "YOUNG then" in " ".join(d) and "EXHAUSTED now" in " ".join(d)


def test_an_identical_state_names_no_differences():
    pack = build(ctx(), [closed("t", 1.0, ctx())])
    assert pack.analogues[0].differs == ()
    assert "no named dimension differs" in pack.analogues[0].line


# ------------------------------------------- it refuses to become a statistic

def test_the_block_forbids_counting_itself():
    """The moment somebody reads '5 of 8 worked' off this, it has made the desk
    worse: the eight were selected for resembling the present."""
    rows = [closed(f"t{k}", 1.0 if k % 2 else -1.0, ctx()) for k in range(10)]
    text = build(ctx(), rows).render()
    assert "NOT A RATE" in text
    assert "Do not count them" in text
    assert "outcome distribution" in text, "it must point at where a real rate lives"


def test_it_never_raises_on_junk():
    assert build({}, [{"kind": "nonsense"}]).empty
    assert build(ctx(), [{"kind": "TRADE_CLOSED"}]).empty


# ----------------------------------------------------------------- it is WIRED

def test_the_brief_carries_precedent_when_it_is_given_some():
    from test_sessions import _market
    from golddesk.features import atr, classify, swings
    from golddesk.runner import build_brief

    bs = _market()
    sw = swings(bs)
    i = len(bs) - 2
    st = classify(bs, i, sw, atr(bs))
    assert st is not None
    brief = build_brief(bs, i, st, sw, bs[i].close - 0.1, bs[i].close + 0.1, 1.0,
                        timeframe="M15", precedent="PRECEDENT — a test block")
    assert "PRECEDENT — a test block" in brief.render()


def test_precedent_comes_last_so_the_model_reads_the_state_first():
    """Leading with analogues invites matching instead of reading."""
    from test_sessions import _market
    from golddesk.features import atr, classify, swings
    from golddesk.runner import build_brief

    bs = _market()
    sw = swings(bs)
    i = len(bs) - 2
    st = classify(bs, i, sw, atr(bs))
    brief = build_brief(bs, i, st, sw, bs[i].close - 0.1, bs[i].close + 0.1, 1.0,
                        timeframe="M15", precedent="PRECEDENT-MARKER")
    assert brief.blocks[-1] == "PRECEDENT-MARKER"


def test_the_query_context_is_the_shape_the_ledger_stores():
    """A private notion of 'the current context' would retrieve neighbours of a
    state the desk never recorded."""
    from golddesk.runner import context_of
    from test_sessions import _market
    from golddesk.features import atr, classify, swings

    bs = _market()
    sw = swings(bs)
    i = len(bs) - 2
    st = classify(bs, i, sw, atr(bs))
    q = context_of(st, None, "NY")
    from golddesk.regime import NOMINAL, ORDINAL
    for key in list(ORDINAL) + list(NOMINAL):
        assert key in q, f"{key} carries weight in similarity and is missing"


def test_the_live_desk_builds_it():
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk)
    assert "_precedent" in src and "memory_pack" in src
