"""A trend making new lows must have something below it to aim at.

THE DEFECT THESE GUARD, observed live on 2026-08-27.

Gold sold off one-way through the London morning. The desk refused every single
bar for six hours. The analyst was not confused and did not disagree — it named
the cause itself, in `why_not`, over and over:

    "the nearest downside objective is untabled, so the target would have to be
     invented"
    "It fails on execution, not on bias ... the reward is unmapped"
    "the table has no level below L10 to run to"
    "DISPLACEMENT and SWEEP are both CONFIRMED and the trend is DOWN and
     ALIGNED — a short looks obvious. It is not tradeable."

Every LevelKind was retrospective: swing highs and lows, session extremes, a
reclaim. All of them are places price has ALREADY BEEN. So in a market printing
new lows the set of candidate targets below spot is EMPTY by construction, the
stop must sit above a swing high two-plus ATR away, R:R computes under the bar,
and the expectancy gate refuses. The arithmetic was right. The inputs were not.

The bias is systematic and runs one way: the harder a market trends, the more
certainly it is rejected. That is not conservatism, it is a desk that can only
express mean reversion between existing levels.

Two fixes, and they are different in kind:

  PRIOR_DAY_HIGH / PRIOR_DAY_LOW are REAL STRUCTURE that the enum has always
  named and nothing ever built. Not projections; they may carry a stop.

  ATR_PROJECTION is DERIVED. It may be aimed at and never risked on — the rule
  in Level.projected, enforced in compile_signal, tested here both ways.

    python3 -m pytest test_projected_levels.py -q
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import (AnalystRead, LevelKind, Setup, Thresholds,
                              compile_signal)
from golddesk.analyst import Refusal
from golddesk.features import Bar, atr, classify, swings
from golddesk.runner import _prior_day_bars, build_brief

UTC = timezone.utc


def _downtrend(n=300, drift=-1.1, amp=6.0, per=17):
    """A market stepping down to a NEW SESSION LOW on the last bar.

    This is the shape that was failing: every retrospective level is ABOVE
    price, because every one of them is somewhere the market already traded and
    price has since gone lower than all of it.

    The oscillation and the jitter are both load-bearing. A monotone decline
    makes each bar's low TIE with its neighbour's, the fractal swing test needs
    a STRICT local extreme, and the fixture then yields zero swings and
    `classify` returns None -- a green run over an empty market. The first
    version of this file did exactly that.
    """
    import math
    seed = 20260827

    def jitter() -> float:
        nonlocal seed
        seed = (1103515245 * seed + 12345) % (1 << 31)
        return (seed / (1 << 31) - 0.5) * 1.6

    out, prev = [], 4700.0
    t = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    for k in range(n):
        px = 4700.0 + drift * k + amp * math.sin(2 * math.pi * k / per) + jitter()
        h = max(prev, px) + 0.5 + abs(jitter())
        lo = min(prev, px) - 0.5 - abs(jitter())
        out.append(Bar(t + timedelta(minutes=15 * k), prev, h, lo, px, 100.0, 0.20))
        prev = px
    return out


def _brief_at_new_low():
    bars = _downtrend()
    i = len(bars) - 2
    sw, atrs = swings(bars), atr(bars)
    st = classify(bars, i, sw, atrs)
    assert st is not None, "fixture produced no structure; the test would be vacuous"
    return build_brief(bars, i, st, sw, bars[i].close - 0.1,
                       bars[i].close + 0.1, 1.0, timeframe="M15"), st


# ------------------------------------------------- the table has room now

def test_a_new_low_used_to_have_nothing_beneath_it():
    """The defect, stated as a measurement of the OLD level kinds only."""
    brief, _ = _brief_at_new_low()
    spot = brief.mid
    retrospective = {LevelKind.SWING_HIGH, LevelKind.SWING_LOW,
                     LevelKind.SESSION_HIGH, LevelKind.SESSION_LOW,
                     LevelKind.RECLAIM}
    below = [lv for lv in brief.levels
             if lv.kind in retrospective and lv.price < spot - 1.0]
    assert not below, (
        f"fixture is not reproducing the failure — {len(below)} retrospective "
        f"levels sit below spot, so this test proves nothing")


def test_there_is_now_a_target_below_a_new_low():
    brief, _ = _brief_at_new_low()
    spot = brief.mid
    below = [lv for lv in brief.levels if lv.price < spot - 1.0]
    assert below, "a market at a new low still has nothing to aim at"
    assert any(lv.kind is LevelKind.ATR_PROJECTION for lv in below)


def test_the_projections_straddle_the_session_and_are_ordered():
    """One and two ATR beyond EACH extreme — not clustered on one side."""
    brief, st = _brief_at_new_low()
    proj = [lv for lv in brief.levels if lv.kind is LevelKind.ATR_PROJECTION]
    assert len(proj) == 4, [(p.id, p.price) for p in proj]
    spot = brief.mid
    assert len([p for p in proj if p.price < spot]) == 2
    assert len([p for p in proj if p.price > spot]) == 2
    lo = sorted(p.price for p in proj if p.price < spot)
    assert lo[0] < lo[1], "the 2-ATR projection is not beyond the 1-ATR one"
    assert abs((lo[1] - lo[0]) - st.atr) < 0.05, "spacing is not one ATR"


def test_every_projection_is_flagged_projected_and_nothing_else_is():
    brief, _ = _brief_at_new_low()
    for lv in brief.levels:
        derived = lv.kind in (LevelKind.ATR_PROJECTION, LevelKind.MEASURED_MOVE)
        assert lv.projected is derived, f"{lv.id} {lv.kind} projected={lv.projected}"


# ------------------------------------- prior-day levels: named, never built

def test_prior_day_levels_are_actually_built():
    """PRIOR_DAY_HIGH/LOW sat in LevelKind unbuilt — visible in the vocabulary
    the analyst was shown, absent from every table it was ever handed."""
    brief, _ = _brief_at_new_low()
    kinds = {lv.kind for lv in brief.levels}
    assert LevelKind.PRIOR_DAY_HIGH in kinds
    assert LevelKind.PRIOR_DAY_LOW in kinds


def test_prior_day_levels_are_real_structure_not_projections():
    """They may carry a stop. That is the whole difference from an ATR level."""
    brief, _ = _brief_at_new_low()
    for lv in brief.levels:
        if lv.kind in (LevelKind.PRIOR_DAY_HIGH, LevelKind.PRIOR_DAY_LOW):
            assert lv.projected is False
            assert lv.confirmed is True


def test_the_prior_day_window_is_one_calendar_day_and_never_today():
    bars = _downtrend()
    i = len(bars) - 2
    prior = _prior_day_bars(bars, i)
    assert prior, "no prior day found in a three-day fixture"
    days = {b.ts.date() for b in prior}
    assert len(days) == 1, days
    assert bars[i].ts.date() not in days
    assert all(b.ts < bars[i].ts for b in prior), "a prior-day bar is not in the past"


def test_a_single_day_of_bars_yields_no_prior_day_rather_than_a_guess():
    """Inventing yesterday's range out of today's bars would put a fabricated
    price in front of the analyst wearing a `confirmed` label."""
    bars = _downtrend()
    same_day = [b for b in bars if b.ts.date() == bars[0].ts.date()]
    assert len(same_day) > 2
    assert _prior_day_bars(same_day, len(same_day) - 1) == []


# ------------------------- a projection may be aimed at, never risked on

def _read(**kw):
    base = dict(setup=Setup.TREND_CONTINUATION, direction="SHORT",
                mechanism_name="trend-continuation", confidence=3,
                read="stepping down", why="lower highs", why_not="could reverse",
                invalidation="close above", entry_ref="MARKET",
                stop_ref="L1", tp1_ref="L1", tp2_ref="L1")
    base.update(kw)
    return AnalystRead(**base)


def _ids(brief, kind):
    return [lv.id for lv in brief.levels if lv.kind is kind]


def test_a_projection_cannot_be_a_stop():
    """A stop answers 'where is this thesis wrong'. Only structure answers it —
    a stop at a derived price is one nobody defends, and noise that never
    touched the idea will take it."""
    brief, _ = _brief_at_new_low()
    proj = _ids(brief, LevelKind.ATR_PROJECTION)[0]
    res = compile_signal(brief, _read(stop_ref=proj), Thresholds())
    assert isinstance(res, Refusal)
    assert "never a stop" in res.reason, res.reason


def test_a_projection_cannot_be_an_entry():
    brief, _ = _brief_at_new_low()
    proj = _ids(brief, LevelKind.ATR_PROJECTION)[0]
    res = compile_signal(brief, _read(entry_ref=proj, stop_ref="L1"), Thresholds())
    assert isinstance(res, Refusal)
    assert "never an entry" in res.reason, res.reason


def test_the_refusal_names_the_level_kind_not_just_the_id():
    """'L14 is unusable' sends the reader to the table. Naming the kind says
    what the rule actually is."""
    brief, _ = _brief_at_new_low()
    proj = _ids(brief, LevelKind.ATR_PROJECTION)[0]
    res = compile_signal(brief, _read(stop_ref=proj), Thresholds())
    assert "ATR_PROJECTION" in res.reason


def test_a_projection_IS_allowed_as_a_target():
    """The point of the whole change. Refused for any reason EXCEPT the ref."""
    brief, _ = _brief_at_new_low()
    below = sorted((lv for lv in brief.levels
                    if lv.kind is LevelKind.ATR_PROJECTION and lv.price < brief.mid),
                   key=lambda lv: lv.price)
    assert below, "no downside projection to aim at"
    highs = _ids(brief, LevelKind.SWING_HIGH)
    assert highs, "fixture has no swing high to stop above"
    res = compile_signal(brief,
                         _read(stop_ref=highs[-1], tp1_ref=below[0].id,
                               tp2_ref=below[-1].id),
                         Thresholds())
    if isinstance(res, Refusal):
        assert "never a stop" not in res.reason
        assert "never an entry" not in res.reason
        assert "not a level in this brief" not in res.reason


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


#: The expectancy gate runs BEFORE anti-chase and would otherwise refuse first,
#: making the drift tests below pass for a reason they are not testing. Lowered
#: here and nowhere else -- these two assert what the DRIFT gate does.
_DRIFT_ONLY = Thresholds(fallback_min_rr=0.05, max_entry_drift_r=0.05)


def _short_at_market(brief):
    """The trade this fixture actually offers: stop above at real structure,
    target below at a projection. Exactly the shape that had nowhere to aim."""
    highs = _ids(brief, LevelKind.SWING_HIGH)
    below = sorted((lv for lv in brief.levels
                    if lv.kind is LevelKind.ATR_PROJECTION and lv.price < brief.mid),
                   key=lambda lv: lv.price)
    assert highs and below, "fixture offers no short"
    return _read(direction="SHORT", setup=Setup.SWING_REVERSAL,
                 entry_ref="MARKET", stop_ref=highs[-1],
                 tp1_ref=below[0].id, tp2_ref=below[-1].id)


# ------------- a missing trigger is not a reason to refuse a real trade

def test_a_market_entry_without_a_trigger_is_no_longer_refused_outright():
    """THE SECOND BLOCKER, from the same morning.

    Two SWING_REVERSAL LONGs at confidence 3 — entry=MARKET stop=L10 tp1=L8
    tp2=L3, fully specified — were refused with "MARKET entry with no
    trigger_price, drift is unmeasurable". Neither was judged on its merits.
    Absence read as a FAILURE; it just fails in the safe-looking direction.
    """
    brief, _ = _brief_at_new_low()
    assert brief.bar_close is not None, "build_brief no longer carries bar_close"
    stripped = replace(brief, trigger_price=None)
    res = compile_signal(stripped, _short_at_market(brief), Thresholds())
    if isinstance(res, Refusal):
        assert "drift is unmeasurable" not in res.reason, res.reason


def test_drift_is_measured_from_the_bar_the_analyst_actually_read():
    """The fallback origin is not a shrug — it must still catch a chase."""
    brief, _ = _brief_at_new_low()
    highs = _ids(brief, LevelKind.SWING_HIGH)
    far = min((lv for lv in brief.levels
               if lv.kind is LevelKind.ATR_PROJECTION), key=lambda lv: lv.price)
    # Stretch the downside target well clear of spot so the EXPECTANCY gate
    # cannot refuse first. Without this the test passes on the wrong gate —
    # which is exactly what it did twice while being written.
    generous = replace(
        brief, trigger_price=None,
        levels=tuple(lv for lv in brief.levels if lv.id != far.id)
        + (replace(far, price=brief.mid - 200.0),))
    read = _read(direction="SHORT", setup=Setup.SWING_REVERSAL,
                 entry_ref="MARKET", stop_ref=highs[-1],
                 tp1_ref=far.id, tp2_ref=far.id)

    # Entered at the bar it read: the drift gate does NOT fire.
    ok = compile_signal(generous, read, _DRIFT_ONLY)
    assert not (isinstance(ok, Refusal) and "do not chase" in ok.reason), ok

    # Same trade, same levels. The ONLY change is that price has run four
    # dollars past the close the analyst formed its view on.
    ran = replace(generous, bid=brief.bar_close - 4.2, ask=brief.bar_close - 4.0)
    res = compile_signal(ran, read, _DRIFT_ONLY)
    assert isinstance(res, Refusal)
    assert "do not chase" in res.reason, res.reason


def test_with_no_bar_close_at_all_it_still_refuses():
    """The narrow honest case survives: a brief carrying neither reference
    genuinely cannot measure drift, and must say so rather than wave it past."""
    brief, _ = _brief_at_new_low()
    bare = replace(brief, trigger_price=None, bar_close=None)
    res = compile_signal(bare, _short_at_market(brief), _DRIFT_ONLY)
    assert isinstance(res, Refusal)
    assert "drift is unmeasurable" in res.reason
