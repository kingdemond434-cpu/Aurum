r"""TP1 was printed on the message and compared to nothing.

THE DEFECT. `grep -rn "\.tp1" golddesk/` found four sites: the compiler computes
it (under the comment "partial bank"), the ledger records it, the Telegram
message renders it, and universe.py mirrors it into a journal dict. ZERO
comparisons against price. TP1 was decoration.

THE COST, measured. 2026-08-27: a short reached +1.88R with TP1 at +1.78R.
Price traded THROUGH the target, nothing banked, a trail then locked +0.29R, and
the pullback took that. The exit message read "capture 15% of MFE" on a call
that was directionally right.

THE MECHANISM. management.options() offers its risk-free partial ONLY while
`guaranteed_now < 0`. A trail that reaches risk-free permanently removes the
partial from the option set, after which the whole position rides one stop.
Nothing was broken; the partial simply stopped being offered at the moment it
became most valuable.

WHY IT IS DETERMINISTIC AND NOT AN OPTION. The tp2 exit is checked directly in
_tick_one; TP1 is the same kind of thing — reaching a NAMED OBJECTIVE is a price
event, not a policy preference. An option the chooser may decline is exactly how
this got lost.

NOT A GATE. It adds no refusal and changes no threshold. It increases realised R
on winners, which is the opposite of trading less.

    python3 -m pytest test_tp1_bank.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import CompiledSignal, Setup, Thresholds
from golddesk.ledger import Ledger
from golddesk.live import LiveDesk, OpenTrade, Vision
from golddesk.partial_policy import MAX, MIN
from golddesk.management import Position
from golddesk.notify import Sink
from golddesk.observer import TradeObserver
from golddesk.providers import AnalystProvider

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 11, 15, tzinfo=UTC)


class _Rec(Sink):
    def __init__(self):
        self.sent = []
    def send(self, text):
        self.sent.append(text)
        return True


class _P(AnalystProvider):
    name, model = "p", "p"
    def read(self, brief, charts=()):
        raise NotImplementedError


def _desk(tmp_path, sink=None):
    return LiveDesk(_P(), Ledger(tmp_path / "l.jsonl"), sink or _Rec(),
                    shadow=True, vision=Vision.NUMERIC_ONLY,
                    thresholds=Thresholds())


def _short(desk):
    """The 2026-08-27 trade, to the cent."""
    entry, stop, tp1, tp2 = 4594.56, 4608.87, 4569.08, 4561.21
    risk = abs(entry - stop)
    pos = Position("SHORT", entry, stop, stop, risk, 1.0, 0.0, T0, "TREND_CONTINUATION")
    sig = CompiledSignal(direction="SHORT", setup=Setup.TREND_CONTINUATION,
                         entry=entry, stop=stop, tp1=tp1, tp2=tp2, risk=risk,
                         rr_tp1=1.78, rr_tp2=2.33, cost_r=0.004,
                         breakeven_win_rate=0.4, ttl_minutes=30, confidence=3,
                         trigger_price=entry, stop_anchor_ref="L7",
                         router_advisories=[], read="r", why="w", why_not="n",
                         invalidation="i", brief_as_of=T0)
    obs = TradeObserver("SHORT", entry, stop, tp2, risk, T0)
    t = OpenTrade(pos, sig, 0, obs, mechanism_name="broken-shelf-retest-supply")
    desk.open_trades.append(t)
    desk.risk.open_risks.append(1.0)
    desk.risk.open_directions.append("SHORT")
    return t


def _tick(desk, px, mins=1):
    desk.on_tick(px, T0 + timedelta(minutes=mins), bid=px - 0.05, ask=px + 0.05)


def test_tp1_is_now_compared_to_price_at_all():
    """The whole defect in one assertion: the package must contain a comparison
    of tp1 against price, not merely a render of it."""
    src = (Path(__file__).parent / "golddesk" / "live.py").read_text(encoding="utf-8")
    assert "t.signal.tp1" in src
    assert "tp1_banked" in src


def test_reaching_tp1_banks_a_partial(tmp_path):
    d = _desk(tmp_path)
    t = _short(d)
    _tick(d, 4580.00)                       # above TP1, nothing yet
    assert not t.tp1_banked and not t.partials
    _tick(d, 4568.50, mins=2)               # through TP1 at 4569.08
    assert t.tp1_banked
    assert t.partials, "TP1 was reached and nothing was banked"
    assert t.position.banked_r > 0


def test_the_runner_survives_for_tp2(tmp_path):
    """Banking everything at TP1 would cap the trade at its first objective."""
    d = _desk(tmp_path)
    t = _short(d)
    _tick(d, 4568.50)
    assert 0 < t.position.remaining_fraction < 1.0
    # The fraction is decided from live conditions, so this asserts the BAND
    # rather than a constant — a bank outside it is either theatre or a runner
    # that cannot pay for what was given up.
    assert 1 - MAX <= t.position.remaining_fraction <= 1 - MIN


def test_it_banks_once_and_not_on_every_subsequent_tick(tmp_path):
    """Re-banking each tick would suffocate the runner to nothing."""
    d = _desk(tmp_path)
    t = _short(d)
    for k in range(25):
        _tick(d, 4568.0 - k * 0.05, mins=k + 1)
    assert len(t.partials) == 1
    assert 1 - MAX <= t.position.remaining_fraction <= 1 - MIN


def test_the_operator_is_told(tmp_path):
    sink = _Rec()
    d = _desk(tmp_path, sink)
    _short(d)
    _tick(d, 4568.50)
    msg = [m for m in sink.sent if "TP1 BANK" in m]
    assert msg, sink.sent
    assert "4569.08" in msg[0] and "runner" in msg[0]


def test_it_is_journalled_on_the_trade(tmp_path):
    """A bank the ledger cannot see is one no capture analysis can credit."""
    d = _desk(tmp_path)
    t = _short(d)
    _tick(d, 4568.50)
    entry = [m for m in t.mgmt_log if m.get("source") == "tp1"]
    assert entry and entry[0]["banked_r"] > 0


def test_price_stopping_short_of_tp1_banks_nothing(tmp_path):
    """The trigger is the objective, not "in profit"."""
    d = _desk(tmp_path)
    t = _short(d)
    _tick(d, 4570.00)                       # 0.92 short of TP1
    assert not t.tp1_banked and not t.partials


def test_a_long_banks_on_the_other_side(tmp_path):
    """Direction must not be hardcoded to the trade that exposed the bug."""
    d = _desk(tmp_path)
    entry, stop, tp1, tp2 = 4580.0, 4570.0, 4595.0, 4605.0
    pos = Position("LONG", entry, stop, stop, 10.0, 1.0, 0.0, T0, "NOVEL")
    sig = CompiledSignal(direction="LONG", setup=Setup.NOVEL, entry=entry,
                         stop=stop, tp1=tp1, tp2=tp2, risk=10.0, rr_tp1=1.5,
                         rr_tp2=2.5, cost_r=0.004, breakeven_win_rate=0.4,
                         ttl_minutes=30, confidence=3, trigger_price=entry,
                         stop_anchor_ref="L1", router_advisories=[], read="r",
                         why="w", why_not="n", invalidation="i", brief_as_of=T0)
    t = OpenTrade(pos, sig, 0, TradeObserver("LONG", entry, stop, tp2, 10.0, T0))
    d.open_trades.append(t)
    d.risk.open_risks.append(1.0)
    d.risk.open_directions.append("LONG")
    _tick(d, 4570.5)                        # still below TP1
    assert not t.tp1_banked
    _tick(d, 4596.0, mins=2)
    assert t.tp1_banked and t.partials


def test_a_stop_touch_still_wins_over_tp1(tmp_path):
    """Order matters: the exit checks run first, so a tick that would do both
    exits rather than banking into a position that is already closed."""
    d = _desk(tmp_path)
    t = _short(d)
    _tick(d, 4620.0)                        # straight through the stop
    assert not d.open_trades
    assert not t.tp1_banked


def test_the_invariants_still_bind(tmp_path):
    """The bank goes through apply_option, so a runner below the minimum is
    REJECTED rather than forced — and a rejection must not retry every tick."""
    d = _desk(tmp_path)
    t = _short(d)
    t.position = Position(**{**t.position.__dict__, "remaining_fraction": 0.12})
    _tick(d, 4568.50)
    assert t.tp1_banked, "a declined bank must not re-attempt on every tick"
    assert not t.partials
    assert t.position.remaining_fraction == pytest.approx(0.12)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
