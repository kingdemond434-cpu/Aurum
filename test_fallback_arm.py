r"""BLIND is the only output guaranteed to be worth nothing.

WHAT HAPPENED. The Claude CLI's OAuth session expired and could not refresh. The
desk booked BLIND on 59 of 59 wakes — no signals at all, for hours — while the
operator, who places every trade by hand on this ADVISORY desk, got silence.
Silence is indistinguishable from a quiet market, so it does not even read as a
failure.

The desk has shipped a rule-based reader the whole time. DeterministicAnalyst is
ARM A, the baseline every intelligent arm must beat, and it travels the identical
LiveDesk path: same compiler, same router, same risk gate, same ledger. It needs
no model, no network and no login. Nothing used it when the analyst died.

A rule-based read is WORSE evidence than a model read — structure only, no
context, no macro, no judgement. It is enormously better than nothing.

THE HALF THAT KEEPS IT HONEST, and it is the larger half. A fallback that
produced ordinary-looking SIGNAL rows would MASK the outage it exists to
survive: the desk would look healthiest precisely while its analyst was dead.
That is WS-005 with extra steps. So every degraded read is stamped, kept out of
the analyst's cohort, and counted against the answer rate rather than for it.

    python3 -m pytest test_fallback_arm.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst_health import MIN_WAKES, check_answer_rate
from golddesk.providers import AnalystError, DeterministicProvider

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

EXPIRED = AnalystError(
    "claude cannot authenticate: Failed to authenticate: OAuth session expired")


class _Desk:
    """The two methods under test, lifted onto a stand-in.

    LiveDesk needs a broker, a feed, a ledger and a provider to construct, none
    of which this behaviour touches. Binding the real functions keeps the test
    about the fallback rather than about assembling a desk.
    """

    def __init__(self, provider=None, on=True):
        from golddesk.live import LiveDesk, LiveStats
        self.provider = provider or object()
        self.fallback_when_blind = on
        self._fallback = None
        self._degraded_notified = False
        self.stats = LiveStats()
        self.sent = []
        self._fallback_read = LiveDesk._fallback_read.__get__(self)

    def _notify(self, m):
        self.sent.append(m)


def _brief():
    from test_claudecode_provider import brief
    return brief()


# --------------------------------------------------------------------------
# It produces something.

def test_an_unreachable_analyst_produces_a_read_instead_of_silence():
    d = _Desk()
    pr = d._fallback_read(_brief(), "read", EXPIRED)
    assert pr is not None, "the desk went dark when it did not have to"
    assert pr.read is not None


def test_the_fallback_needs_no_login_network_or_model():
    """The property that makes it useful during exactly this outage."""
    p = DeterministicProvider()
    assert p.model == "rules-v1"
    assert p.read(_brief()).read is not None


def test_it_can_be_turned_off_and_then_the_desk_books_blind():
    d = _Desk(on=False)
    assert d._fallback_read(_brief(), "read", EXPIRED) is None


def test_a_fallback_that_fails_too_returns_None_rather_than_half_a_read():
    """Then the caller books BLIND exactly as it always did. A fallback that
    failed quietly would turn one outage into a different outage with no record
    of either."""
    class Broken:
        def read(self, brief, charts=()):
            raise RuntimeError("no")

    d = _Desk()
    d._fallback = Broken()
    assert d._fallback_read(_brief(), "read", EXPIRED) is None


# --------------------------------------------------------------------------
# It is labelled. This is the safety argument.

def test_every_degraded_read_is_stamped_with_what_it_is():
    d = _Desk()
    pr = d._fallback_read(_brief(), "read", EXPIRED)
    assert pr.usage["degraded"] is True
    assert pr.provider == "deterministic" and pr.model == "rules-v1"


def test_the_stamp_carries_why_it_degraded():
    """Six weeks later, 'why is there a block of rules-v1 reads here' must be
    answerable from the ledger alone."""
    d = _Desk()
    pr = d._fallback_read(_brief(), "survey", EXPIRED)
    assert pr.usage["degraded_stage"] == "survey"
    assert "OAuth session expired" in pr.usage["degraded_because"]


def test_fallback_reads_are_counted_separately_from_real_ones():
    """Folding them into `reads` would make the desk look busiest exactly while
    its analyst was dead."""
    d = _Desk()
    d._fallback_read(_brief(), "read", EXPIRED)
    d._fallback_read(_brief(), "read", EXPIRED)
    assert d.stats.fallback_reads == 2
    assert d.stats.reads == 0


# --------------------------------------------------------------------------
# It cannot hide the outage. The larger half.

def _row(kind="SIGNAL", degraded=False, ts="2026-08-28T11:00:00+00:00"):
    dec = {"provider": "claudecode", "model": "claude-opus-5"}
    if degraded:
        dec = {"provider": "deterministic", "model": "rules-v1",
               "usage": {"degraded": True}}
    return {"t0": ts, "kind": kind, "decision": dec}


def test_degraded_signals_do_not_read_as_a_healthy_analyst():
    """THE DEFECT THIS PREVENTS. Without it the fallback masks the very outage
    it exists to survive, and answer rate reports 100% while nothing has
    reached a model for hours."""
    rows = [_row(degraded=True) for _ in range(MIN_WAKES + 5)]
    f = check_answer_rate(rows, NOW)
    assert not f.ok
    assert "RULE-BASED FALLBACK" in f.detail


def test_the_finding_says_why_nothing_else_noticed():
    rows = [_row(degraded=True) for _ in range(MIN_WAKES + 5)]
    detail = check_answer_rate(rows, NOW).detail
    assert "still producing signals" in detail


def test_a_healthy_desk_is_still_reported_healthy():
    """The check must not fire on ordinary reads, or it is noise."""
    rows = [_row() for _ in range(MIN_WAKES + 5)]
    assert check_answer_rate(rows, NOW).ok


def test_blind_and_degraded_are_counted_together_as_not_from_the_analyst():
    rows = ([_row(degraded=True) for _ in range(10)]
            + [_row(kind="BLIND") for _ in range(10)]
            + [_row() for _ in range(5)])
    f = check_answer_rate(rows, NOW)
    assert not f.ok
    assert "80%" in f.detail, f.detail


# --------------------------------------------------------------------------
# It tells the operator, once.

def test_the_operator_is_told_the_desk_dropped_to_the_rules():
    d = _Desk()
    d._fallback_read(_brief(), "read", EXPIRED)
    assert len(d.sent) == 1
    assert "RULE-BASED ARM" in d.sent[0]


def test_it_says_the_reads_are_weaker_rather_than_implying_all_is_well():
    d = _Desk()
    d._fallback_read(_brief(), "read", EXPIRED)
    assert "WEAKER" in d.sent[0]
    assert "Better than silence" in d.sent[0]


def test_it_alarms_once_per_outage_not_once_per_bar():
    """On M15 that would be four messages an hour for as long as it lasted."""
    d = _Desk()
    for _ in range(20):
        d._fallback_read(_brief(), "read", EXPIRED)
    assert len(d.sent) == 1



# --------------------------------------------------------------------------
# End to end, through the REAL desk. The claim is "the operator gets signals
# instead of silence", and only a real drive can make it.

def test_a_real_desk_with_a_dead_analyst_produces_decisions_not_silence():
    """THE WHOLE POINT, asserted against the real LiveDesk over real bars.

    Before this, an expired login meant 59 of 59 wakes journalled BLIND and the
    operator got nothing. The comparison here is the same desk, same bars, same
    dead provider — fallback off versus on."""
    import json

    from test_blind_ledger import BlindProvider, _bars
    from golddesk.features import atr, classify, swings
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.analyst import Thresholds
    from golddesk.notify import NullSink
    import tempfile

    def drive(fallback_on):
        bars = _bars()
        atrs, sw = atr(bars), swings(bars)
        out = Path(tempfile.mkdtemp()) / "l.jsonl"
        desk = LiveDesk(BlindProvider(), Ledger(out), NullSink(),
                        shadow=True, vision=Vision.NUMERIC_ONLY,
                        thresholds=Thresholds(fallback_min_rr=1.0),
                        measure_position_constraint=False)
        desk.fallback_when_blind = fallback_on
        tl: list[str] = []
        for i in range(60, len(bars) - 61):
            if classify(bars, i, sw, atrs) is None:
                continue
            desk.on_bar(bars, i, sw, atrs, None,
                        (bars[i].close - 0.05, bars[i].close + 0.05, 1.0), tl)
        rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()
                if x.strip()] if out.exists() else []
        return desk, rows

    _, dark = drive(False)
    desk, lit = drive(True)
    dark_decisions = [r for r in dark if r.get("kind") != "SPECIALIST_VERDICT"]
    lit_decisions = [r for r in lit if r.get("kind") != "SPECIALIST_VERDICT"]

    assert dark_decisions, "sanity: the dead-analyst desk should journal something"
    assert all(r["kind"] == "BLIND" for r in dark_decisions), \
        "sanity: every decision is blind without it"

    assert desk.stats.fallback_reads > 0, "the fallback never ran"
    assert not any(r["kind"] == "BLIND" for r in lit_decisions), \
        "still going dark with a working fallback available"


def test_the_end_to_end_rows_are_labelled_in_the_ledger():
    """Not just in memory. Six weeks later the only thing left is the file."""
    import json
    import tempfile

    from test_blind_ledger import BlindProvider, _bars
    from golddesk.features import atr, classify, swings
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.analyst import Thresholds
    from golddesk.notify import NullSink

    bars = _bars()
    atrs, sw = atr(bars), swings(bars)
    out = Path(tempfile.mkdtemp()) / "l.jsonl"
    desk = LiveDesk(BlindProvider(), Ledger(out), NullSink(),
                    shadow=True, vision=Vision.NUMERIC_ONLY,
                    thresholds=Thresholds(fallback_min_rr=1.0),
                    measure_position_constraint=False)
    tl: list[str] = []
    for i in range(60, len(bars) - 61):
        if classify(bars, i, sw, atrs) is None:
            continue
        desk.on_bar(bars, i, sw, atrs, None,
                    (bars[i].close - 0.05, bars[i].close + 0.05, 1.0), tl)
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    assert rows
    stamped = [r for r in rows
               if ((r.get("decision") or {}).get("usage") or {}).get("degraded")]
    assert stamped, "no row records that it came from the fallback"
    assert all(r["decision"]["model"] == "rules-v1" for r in stamped)


# --------------------------------------------------------------------------
# A check that cannot clear is not a check. Learned on excursion, repeated here.

def _r(kind, ts, degraded=False, login=False):
    dec = {"provider": "claudecode", "model": "claude-opus-5"}
    if degraded:
        dec = {"usage": {"degraded": True}}
    if login:
        dec = {"cli": {"result": "Failed to authenticate: OAuth session expired"}}
    return {"t0": ts, "kind": kind, "decision": dec}


def test_the_login_check_clears_once_the_analyst_answers_again():
    """OBSERVED 2026-08-28: Telegram said ANALYST BACK while this said THE LOGIN
    HAS EXPIRED, at the same moment, from the same ledger. A contradiction like
    that does not get investigated — it gets the whole report distrusted."""
    from golddesk.analyst_health import check_login
    rows = ([_r("BLIND", f"2026-08-28T0{h}:00:00+00:00", login=True) for h in range(1, 9)]
            + [_r("SIGNAL", "2026-08-28T11:30:00+00:00")])
    f = check_login(rows, NOW.replace(hour=12))
    assert f.ok, f.detail
    assert "RECOVERED" in f.detail
    assert "credential works now" in f.detail


def test_it_still_fires_while_the_login_is_actually_dead():
    """The clearing must depend on a real answer, not on time passing."""
    from golddesk.analyst_health import check_login
    rows = [_r("BLIND", f"2026-08-28T0{h}:00:00+00:00", login=True) for h in range(1, 9)]
    f = check_login(rows, NOW.replace(hour=12))
    assert not f.ok
    assert "NOTHING has answered since" in f.detail


def test_a_fallback_read_does_not_clear_the_login_check():
    """The rule-based arm answering proves nothing about the credential — that
    is the entire reason degraded reads are stamped."""
    from golddesk.analyst_health import check_login
    rows = ([_r("BLIND", "2026-08-28T01:00:00+00:00", login=True)]
            + [_r("SIGNAL", "2026-08-28T11:30:00+00:00", degraded=True)])
    assert not check_login(rows, NOW.replace(hour=12)).ok


def test_the_answer_rate_says_when_an_outage_is_RESOLVED():
    rows = ([_r("BLIND", "2026-08-28T0%d:00:00+00:00" % h) for h in range(1, 10)]
            + [_r("SIGNAL", "2026-08-28T1%d:00:00+00:00" % h) for h in range(0, 2)]
            + [_r("SIGNAL", "2026-08-28T11:30:00+00:00")]
            + [_r("REFUSAL_MODEL", "2026-08-28T11:45:00+00:00")] * 9)
    f = check_answer_rate(rows, NOW.replace(hour=12))
    assert f.ok
    assert "RESOLVED outage" in f.detail


def test_the_answer_rate_still_fails_while_the_desk_is_blind_now():
    rows = [_r("BLIND", "2026-08-28T0%d:00:00+00:00" % (h % 10)) for h in range(MIN_WAKES + 5)]
    f = check_answer_rate(rows, NOW.replace(hour=12))
    assert not f.ok
    assert "BLIND on those bars" in f.detail

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
