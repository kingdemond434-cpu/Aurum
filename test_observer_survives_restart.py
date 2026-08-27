"""A restart must not erase what the open trade has been through.

THE DEFECT. An exit message read "MFE +0.00R · MAE +0.00R · 0 observations" on a
trade that had been open for hours.

`DeskService.checkpoint()` wrote three observer fields — mfe_r, mae_r, ticks —
and `rehydrate()` read back only the first two. So `ticks` reset to zero on
every restart, which is literally the "0 observations". Worse, `path`, `t_mfe`
and `t_mae` were never written at all, so the full excursion path and BOTH
time-to-extreme stamps were destroyed by any restart — and this desk restarts on
every logon, every watchdog relaunch and every deploy.

That is not cosmetic. The path IS the forward evidence: time-to-MFE,
time-to-MAE, whether +0.5R came before −1R, how much of MFE was captured. A desk
cannot learn whether a wide structural stop beats a tight one from a record that
resets to zero every few hours — and it cannot tell "this mechanism never moved"
from "nobody was watching when it did".

TELEMETRY ONLY. Nothing here gates a trade, changes a threshold, or alters
firing rate. It restores measurement that was being silently thrown away.

    python3 -m pytest test_observer_survives_restart.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 11, 15, tzinfo=UTC)


def _service(tmp_path):
    from golddesk.analyst import Thresholds
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.notify import Sink
    from golddesk.providers import AnalystProvider
    from golddesk.service import DeskService, ServiceConfig

    class _P(AnalystProvider):
        name, model = "p", "p"
        def read(self, brief, charts=()):
            raise NotImplementedError

    class _S(Sink):
        def send(self, text):
            return True

    cfg = ServiceConfig(state_path=tmp_path / "svc.json",
                        ledger_path=tmp_path / "l.jsonl",
                        archive_ticks=False)
    desk = LiveDesk(_P(), Ledger(cfg.ledger_path), _S(), shadow=True,
                    vision=Vision.NUMERIC_ONLY, thresholds=Thresholds())
    return DeskService(desk, feed=None, cfg=cfg)


def _open_a_trade(svc):
    """A short with a real excursion history behind it."""
    from golddesk.analyst import CompiledSignal, Setup
    from golddesk.live import OpenTrade
    from golddesk.management import Position
    from golddesk.observer import TradeObserver

    entry, stop, tp2 = 4596.52, 4609.00, 4561.21
    risk = abs(entry - stop)
    pos = Position("SHORT", entry, stop, stop, risk, 1.0, 0.0, T0, "NOVEL")
    sig = CompiledSignal(direction="SHORT", setup=Setup.NOVEL, entry=entry,
                         stop=stop, tp1=4569.08, tp2=tp2, risk=risk,
                         rr_tp1=1.5, rr_tp2=2.33, cost_r=0.0035,
                         breakeven_win_rate=0.4, ttl_minutes=30, confidence=3,
                         trigger_price=entry, stop_anchor_ref="L7",
                         router_advisories=[], read="r", why="w", why_not="n",
                         invalidation="i", brief_as_of=T0)
    obs = TradeObserver("SHORT", entry, stop, tp2, risk, T0)
    # A trade that HAS been through something: 40 observations, both extremes
    # stamped, a real path.
    for k in range(40):
        px = entry - 6.0 + (k % 11) * 1.3
        obs.observe(px, T0 + timedelta(minutes=k))
    t = OpenTrade(pos, sig, 0, obs, mechanism_name="broken-shelf-retest-supply")
    svc.desk.open_trades.append(t)
    svc.desk.risk.open_risks.append(1.0)
    svc.desk.risk.open_directions.append("SHORT")
    return t


def _round_trip(svc, tmp_path):
    """Checkpoint, then rehydrate into a brand-new service — a real restart."""
    svc.checkpoint()
    fresh = _service(tmp_path)
    fresh.cfg.state_path = svc.cfg.state_path
    fresh.load_state()          # rehydrate() reads self.state, not the file
    assert fresh.rehydrate(), "the position did not survive the restart at all"
    return fresh.desk.open_trades[0]


def test_the_fixture_actually_accumulates_something(tmp_path):
    """Otherwise every assertion below passes against an empty observer."""
    t = _open_a_trade(_service(tmp_path))
    assert t.observer.ticks == 40
    assert t.observer.mfe_r > 0 and t.observer.mae_r < 0
    assert t.observer.t_mfe and t.observer.t_mae
    assert len(t.observer.path) == 40


def test_observation_count_survives_a_restart(tmp_path):
    """The '0 observations' on the exit message, exactly."""
    svc = _service(tmp_path)
    before = _open_a_trade(svc).observer.ticks
    assert _round_trip(svc, tmp_path).observer.ticks == before


def test_both_extremes_survive_a_restart(tmp_path):
    svc = _service(tmp_path)
    b = _open_a_trade(svc).observer
    a = _round_trip(svc, tmp_path).observer
    assert a.mfe_r == pytest.approx(b.mfe_r)
    assert a.mae_r == pytest.approx(b.mae_r)


def test_time_to_each_extreme_survives_a_restart(tmp_path):
    """Never persisted at all before. time-to-MFE and time-to-MAE are the two
    fields that say whether a move was fast or ground out."""
    svc = _service(tmp_path)
    b = _open_a_trade(svc).observer
    a = _round_trip(svc, tmp_path).observer
    assert a.t_mfe == b.t_mfe
    assert a.t_mae == b.t_mae


def test_the_excursion_path_survives_a_restart(tmp_path):
    """The path is the forward evidence — whether +0.5R came before −1R can only
    be recomputed from it."""
    svc = _service(tmp_path)
    b = _open_a_trade(svc).observer
    a = _round_trip(svc, tmp_path).observer
    assert a.path, "the whole excursion path was lost"
    assert len(a.path) == len(b.path)
    assert a.path[0][1] == pytest.approx(b.path[0][1])
    assert a.path[-1][1] == pytest.approx(b.path[-1][1])


def test_a_long_path_is_bounded_but_keeps_its_extremes(tmp_path):
    """A tick-driven trade can run to tens of thousands of points; the
    checkpoint must not grow without limit. Both turning points are pinned."""
    svc = _service(tmp_path)
    t = _open_a_trade(svc)
    for k in range(5000):
        t.observer.observe(4596.52 - 20.0 + (k % 37) * 0.9,
                           T0 + timedelta(seconds=k))
    peak, trough = t.observer.mfe_r, t.observer.mae_r
    a = _round_trip(svc, tmp_path).observer
    assert len(a.path) <= 500, len(a.path)
    rs = [r for _, r in a.path]
    assert max(rs) == pytest.approx(peak, abs=1e-3)
    assert min(rs) == pytest.approx(trough, abs=1e-3)


def test_an_old_checkpoint_without_the_new_fields_still_restores(tmp_path):
    """Backward compatibility is not optional here: the checkpoint on the box
    right now was written by the old build. A missing field must degrade to the
    old behaviour, never raise and lose a live position."""
    svc = _service(tmp_path)
    _open_a_trade(svc)
    svc.checkpoint()
    raw = json.loads(svc.cfg.state_path.read_text(encoding="utf-8"))
    raw["open_trade"]["observer"] = {"mfe_r": 1.25, "mae_r": -0.4}
    svc.cfg.state_path.write_text(json.dumps(raw), encoding="utf-8")
    fresh = _service(tmp_path)
    fresh.cfg.state_path = svc.cfg.state_path
    fresh.load_state()
    assert fresh.rehydrate()
    o = fresh.desk.open_trades[0].observer
    assert o.mfe_r == 1.25 and o.mae_r == -0.4
    assert o.ticks == 0 and o.path == []


def test_a_corrupt_path_point_is_dropped_not_coerced(tmp_path):
    """A fabricated point in an excursion path is worse than a shorter path."""
    svc = _service(tmp_path)
    _open_a_trade(svc)
    svc.checkpoint()
    raw = json.loads(svc.cfg.state_path.read_text(encoding="utf-8"))
    good = len(raw["open_trade"]["observer"]["path"])
    raw["open_trade"]["observer"]["path"] += [["not-a-timestamp", 1.0],
                                              ["2026-08-27T12:00:00+00:00", "x"],
                                              ["oops"]]
    svc.cfg.state_path.write_text(json.dumps(raw), encoding="utf-8")
    fresh = _service(tmp_path)
    fresh.cfg.state_path = svc.cfg.state_path
    fresh.load_state()
    assert fresh.rehydrate()
    assert len(fresh.desk.open_trades[0].observer.path) == good


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
