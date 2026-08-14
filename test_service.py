"""Integration tests for DeskService — the daemon, not LiveDesk.

The existing suite exercises LiveDesk thoroughly and the service layer not at
all, which is how four P0s survived into a package whose own audit called it
production wiring. Every test here corresponds to one of them and fails if it
returns.

    python3 test_service.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import AnalystRead, CompiledSignal, Setup, Thresholds
from golddesk.feed import FeedError
from golddesk.features import Bar
from golddesk.ledger import Ledger
from golddesk.live import LiveDesk, OpenTrade, Resolution, Vision
from golddesk.management import Position
from golddesk.notify import Sink
from golddesk.observer import TradeObserver
from golddesk.providers import AnalystProvider, ProviderRead
from golddesk.service import DeskService, ServiceConfig

UTC = timezone.utc
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


class RecordingSink(Sink):
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(text); return True


class StubProvider(AnalystProvider):
    name, model = "stub", "stub-v1"
    def read(self, brief, charts=()):
        raise RuntimeError("no analysis in these tests")


def bars(n=300, start=2000.0):
    out, px = [], start
    t = datetime(2025, 6, 2, tzinfo=UTC)
    for k in range(n):
        px += 0.7 if (k // 30) % 2 == 0 else -0.6
        out.append(Bar(t + timedelta(minutes=15 * k), px - 0.3, px + 2.0,
                       px - 2.0, px, 100.0, 0.30))
    return out


class FakeFeed:
    """Matches the LiveFeed contract that DeskService actually uses.

    `bars()` returns CLOSED bars only, exactly as LiveFeed documents, because
    the whole point of one of these tests is that the service must not drop a
    second one on top of that.
    """

    def __init__(self, series, bid=2000.0, ask=2000.3, stale=False,
                 fail_first_connect=False, max_tick_age_s=30.0):
        self._bars = series
        self.bid, self.ask = bid, ask
        self._stale = stale
        # AGE AND STALENESS ARE THE SAME FACT. LiveFeed.tick_is_stale() is
        # literally implemented as `quote()` and compares the age it returns, so
        # a feed reporting "stale" while handing back a 1ms-old quote is a state
        # the real class cannot reach. The fixture previously did exactly that,
        # which meant it passed against a loop that called tick_is_stale() and
        # silently stopped testing anything once the loop derived staleness from
        # the quote it already had. One source of truth here too.
        self.age = (max_tick_age_s * 4) if stale else 0.001
        self.connects = 0
        self.fail_first_connect = fail_first_connect

    def connect(self):
        self.connects += 1
        if self.fail_first_connect and self.connects == 1:
            raise FeedError("simulated connect failure")
        return True

    def quote(self):
        return self.bid, self.ask, self.age

    def tick_is_stale(self, max_age_s=30.0):
        # Derived from age, exactly as LiveFeed does — not stored separately.
        return self.age > max_age_s, self.age  # the tuple that started it all

    def bars(self, timeframe, count=500):
        return list(self._bars[-count:])


def make_desk(out: Path, sink=None):
    return LiveDesk(StubProvider(), Ledger(out / "l.jsonl"), sink or RecordingSink(),
                    shadow=True, vision=Vision.NUMERIC_ONLY,
                    thresholds=Thresholds(fallback_min_rr=1.0))


def open_trade(desk, entry=2000.0, stop=1990.0, tp2=2030.0, direction="LONG",
               banked=0.0, remaining=1.0, cost_r=0.05):
    pos = Position(direction, entry, stop, stop, abs(entry - stop), remaining,
                   banked, datetime(2025, 6, 2, tzinfo=UTC), "SWING_REVERSAL")
    sig = CompiledSignal(direction=direction, setup=Setup.SWING_REVERSAL, entry=entry,
                         stop=stop, tp1=(entry + tp2) / 2, tp2=tp2,
                         risk=abs(entry - stop), rr_tp1=1.0, rr_tp2=3.0,
                         cost_r=cost_r, breakeven_win_rate=0.3, ttl_minutes=30,
                         confidence=3, trigger_price=entry, stop_anchor_ref="L1",
                         router_advisories=[], read="r", why="w", why_not="n",
                         invalidation="i", brief_as_of=datetime(2025, 6, 2, tzinfo=UTC))
    obs = TradeObserver(direction, entry, stop, tp2, abs(entry - stop),
                        pos.opened_utc)
    # APPEND, never `desk.open = ...`. The compat setter deliberately REPLACES
    # the list — that is what "the one open trade" means for rehydrate — so
    # using it here would silently make every multi-thesis test a single-thesis
    # test that passes for the wrong reason.
    t = OpenTrade(pos, sig, 0, obs, mechanism_name="test-mech")
    desk.open_trades.append(t)
    desk.risk.open_risks.append(1.0)
    desk.risk.open_directions.append(direction)
    return t


# --------------------------------------------------------------------------

def test_p0_1_stale_tuple():
    print("\nP0-1  tick_is_stale returns a TUPLE — a truthy one")
    f = FakeFeed(bars())
    stale, age = f.tick_is_stale()
    check("feed reports not-stale", stale is False, f"returns {(stale, age)}")
    check("the raw tuple is truthy (the trap)", bool((stale, age)) is True,
          f"bool({(stale, age)}) = {bool((stale, age))}")

    out = Path(tempfile.mkdtemp())
    svc = DeskService(make_desk(out), f,
                      ServiceConfig(state_path=out / "s.json",
                                    ledger_path=out / "l.jsonl",
                                    poll_seconds=0.001))
    svc.run(max_seconds=0.6)
    check("service PROCESSES ticks when the feed says not-stale",
          svc.state.ticks_seen > 0,
          f"ticks_seen={svc.state.ticks_seen} stale_suspensions={svc.state.stale_suspensions}")
    check("service does NOT log stale suspensions on a healthy feed",
          svc.state.stale_suspensions == 0,
          f"stale_suspensions={svc.state.stale_suspensions}")

    f2 = FakeFeed(bars(), stale=True)
    svc2 = DeskService(make_desk(out), f2,
                       ServiceConfig(state_path=out / "s2.json",
                                     ledger_path=out / "l2.jsonl", poll_seconds=0.001))
    svc2.run(max_seconds=0.4)
    check("a genuinely stale feed DOES suspend", svc2.state.stale_suspensions > 0,
          f"stale_suspensions={svc2.state.stale_suspensions}")
    shutil.rmtree(out, ignore_errors=True)


def test_p0_3_closed_bar_contract():
    print("\nP0-3  the newest CLOSED bar must be the one analysed")
    series = bars()
    f = FakeFeed(series)
    out = Path(tempfile.mkdtemp())
    svc = DeskService(make_desk(out), f,
                      ServiceConfig(state_path=out / "s.json",
                                    ledger_path=out / "l.jsonl", poll_seconds=0.001))
    svc.run(max_seconds=0.6)
    newest = series[-1].ts.isoformat()
    check("service processed a bar at all", svc.state.bars_processed > 0,
          f"bars_processed={svc.state.bars_processed}")
    check("it used the NEWEST closed bar, not the one before it",
          svc.state.last_bar_ts == newest,
          f"used {svc.state.last_bar_ts}, newest closed is {newest} "
          f"(previous was {series[-2].ts.isoformat()})")
    check("the desk's bar window ends at the newest closed bar",
          svc._bars and svc._bars[-1].ts == series[-1].ts)
    shutil.rmtree(out, ignore_errors=True)


def test_p0_2_rehydrate():
    print("\nP0-2  restart recovery must restore the trade, exactly once")
    out = Path(tempfile.mkdtemp())
    cfg = ServiceConfig(state_path=out / "s.json", ledger_path=out / "l.jsonl",
                        poll_seconds=0.001)
    d1 = make_desk(out)
    open_trade(d1, banked=0.4, remaining=0.6)
    s1 = DeskService(d1, FakeFeed(bars()), cfg)
    s1.checkpoint()
    check("checkpoint stores the compiled signal, not just tp2",
          "signal" in (json.loads(cfg.state_path.read_text())["open_trade"] or {}),
          f"keys={sorted((json.loads(cfg.state_path.read_text())['open_trade'] or {}))}")

    d2 = make_desk(out)
    s2 = DeskService(d2, FakeFeed(bars()), cfg)
    s2.load_state()
    restored = s2.rehydrate()
    check("rehydrate() returns True", restored)
    check("desk.open is ACTUALLY restored", d2.open is not None,
          "this is the seam that was missing entirely")
    if d2.open:
        check("position survives intact",
              abs(d2.open.position.banked_r - 0.4) < 1e-9
              and abs(d2.open.position.remaining_fraction - 0.6) < 1e-9,
              f"banked={d2.open.position.banked_r} remaining={d2.open.position.remaining_fraction}")
        check("the signal survives, so tp2/cost are available",
              abs(d2.open.signal.tp2 - 2030.0) < 1e-9)
    check("risk ledger has exactly ONE entry", len(d2.risk.open_risks) == 1,
          f"open_risks={d2.risk.open_risks}")

    for _ in range(4):
        s2.rehydrate()                       # simulate repeated reconnects
    check("repeated rehydrate does NOT accumulate risk entries",
          len(d2.risk.open_risks) == 1,
          f"after 5 calls: open_risks={d2.risk.open_risks}")

    d3 = make_desk(out)
    s3 = DeskService(d3, FakeFeed(bars()), cfg)
    s3.state.open_trade = {"position": {"direction": "LONG"}}   # corrupt
    check("an unrestorable checkpoint leaves NO phantom risk",
          s3.rehydrate() is False and not d3.risk.open_risks,
          f"open={d3.open} risks={d3.risk.open_risks}")
    shutil.rmtree(out, ignore_errors=True)


def test_p0_4_execution_side_and_cost():
    print("\nP0-4  exits use bid/ask; realised R is net of cost")
    out = Path(tempfile.mkdtemp())
    d = make_desk(out)
    t = open_trade(d, entry=2000.0, stop=1990.0, tp2=2030.0, cost_r=0.05)
    d._last_state = None
    # mid is above the stop, bid is not: a long exits on the bid
    r = d.on_tick(1990.15, datetime(2025, 6, 2, 1, tzinfo=UTC),
                  bid=1989.95, ask=1990.35)
    check("LONG stop triggers on the BID, not the mid", r == "EXIT_STOP",
          f"mid 1990.15 > stop, bid 1989.95 <= stop -> {r}")

    d2 = make_desk(out)
    open_trade(d2, entry=2000.0, stop=2010.0, tp2=1970.0, direction="SHORT", cost_r=0.05)
    d2._last_state = None
    r2 = d2.on_tick(2009.9, datetime(2025, 6, 2, 1, tzinfo=UTC),
                    bid=2009.7, ask=2010.1)
    check("SHORT stop triggers on the ASK", r2 == "EXIT_STOP",
          f"mid 2009.9 < stop, ask 2010.1 >= stop -> {r2}")

    rows = [json.loads(l) for l in (out / "l.jsonl").read_text().splitlines() if l.strip()]
    closed = [x for x in rows if x.get("kind") == "TRADE_CLOSED"]
    check("close row records gross AND net separately", bool(closed)
          and "gross_r" in closed[0] and "cost_r" in closed[0],
          f"keys={sorted(closed[0])[:8]}" if closed else "no close row")
    if closed:
        c = closed[0]
        check("realised_r is gross minus cost",
              abs(c["realised_r"] - (c["gross_r"] - c["cost_r"])) < 1e-9,
              f"{c['realised_r']} == {c['gross_r']} - {c['cost_r']}")
        check("cost was actually charged", c["cost_r"] > 0,
              f"cost_r={c['cost_r']}")
    shutil.rmtree(out, ignore_errors=True)


def test_reconnect_and_sink():
    print("\nSUPERVISION  reconnect after a feed failure; sink is real")
    out = Path(tempfile.mkdtemp())
    sink = RecordingSink()
    f = FakeFeed(bars(), fail_first_connect=True)
    svc = DeskService(make_desk(out, sink), f,
                      ServiceConfig(state_path=out / "s.json",
                                    ledger_path=out / "l.jsonl",
                                    poll_seconds=0.001, backoff_initial_s=0.05))
    svc.run(max_seconds=0.8)
    check("survives a connect failure and retries", f.connects >= 2,
          f"connects={f.connects} reconnects={svc.state.reconnects}")
    check("notifications actually reach the sink", len(sink.sent) > 0,
          f"{len(sink.sent)} message(s); first: {sink.sent[0][:60] if sink.sent else '-'}")
    check("shadow mode is tagged", all(s.startswith("[SHADOW]") for s in sink.sent))
    shutil.rmtree(out, ignore_errors=True)


def test_multi_thesis():
    print("\nMULTI-THESIS  concurrency is decided by the constitution")
    from golddesk.constitution import BY_ID, Status
    out = Path(tempfile.mkdtemp())
    d = make_desk(out)

    BY_ID["risk.one_position"].status = Status.ENFORCING
    check("enforcing -> one thesis only", d.max_concurrent() == 1)
    BY_ID["risk.one_position"].status = Status.ADVISORY
    check("demoted -> concurrency allowed", d.max_concurrent() > 1,
          f"max_concurrent={d.max_concurrent()} (heat still bounds it)")

    a = open_trade(d, entry=2000.0, stop=1990.0, tp2=2030.0)
    b = open_trade(d, entry=2005.0, stop=1995.0, tp2=2040.0)
    check("two theses held at once", len(d.open_trades) == 2)
    check("`open` still returns the first (compat surface)", d.open is a)
    check("risk ledger tracks both", len(d.risk.open_risks) == 2,
          f"open_risks={d.risk.open_risks}")

    # close the FIRST one; the second must survive intact with its own risk
    d._last_state = None
    d._close(datetime(2025, 6, 2, 1, tzinfo=UTC), -1.0, "STOP", None,
             Resolution.TICK_OBSERVED, t=a)
    check("closing one leaves the other open", len(d.open_trades) == 1
          and d.open_trades[0] is b)
    check("the RIGHT risk entry was released", len(d.risk.open_risks) == 1,
          f"open_risks={d.risk.open_risks}")

    # a tick must reach every open thesis
    # The two ranges must OVERLAP, or the tick exits one of them before the
    # observer ever runs — exits are checked first, by design.
    d2 = make_desk(out)
    x = open_trade(d2, entry=2000.0, stop=1980.0, tp2=2060.0)
    y = open_trade(d2, entry=2010.0, stop=1985.0, tp2=2070.0)
    d2._last_state = None
    d2.on_tick(2020.0, datetime(2025, 6, 2, 2, tzinfo=UTC), bid=2019.9, ask=2020.1)
    check("on_tick feeds EVERY open thesis",
          x.observer.ticks > 0 and y.observer.ticks > 0,
          f"observer ticks: {x.observer.ticks} and {y.observer.ticks}")

    BY_ID["risk.one_position"].status = Status.ENFORCING   # restore
    shutil.rmtree(out, ignore_errors=True)


def main():
    print("=" * 78)
    print("DESKSERVICE INTEGRATION TESTS — the daemon, not LiveDesk")
    print("=" * 78)
    for fn in (test_p0_1_stale_tuple, test_p0_3_closed_bar_contract,
               test_p0_2_rehydrate, test_p0_4_execution_side_and_cost,
               test_reconnect_and_sink, test_multi_thesis):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{fn.__name__}: {type(e).__name__}: {e}")
    print("\n" + "=" * 78)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
