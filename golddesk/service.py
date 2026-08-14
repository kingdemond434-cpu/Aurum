"""The 24/5 desk process. Components existing is not a deployed desk.

This is the thing that actually runs: a supervised loop that holds a live MT5
connection, drives LiveDesk.on_tick() continuously and LiveDesk.on_bar() on each
close, survives disconnects and restarts, and never trades on data it cannot
vouch for.

WHAT A LONG-RUNNING TRADING PROCESS HAS TO GET RIGHT, AND WHY EACH IS HERE

  Reconnect.        Terminals restart, brokers drop sessions, laptops sleep. A
                    loop that dies on the first FeedError is a demo. Backoff is
                    bounded and every attempt is logged.

  Staleness.        The dangerous failure is not a dead feed — that is obvious —
                    it is a feed that keeps returning the LAST tick forever. The
                    desk then manages a position against a fossil price and
                    believes it is fine. `LiveFeed.tick_is_stale()` is checked
                    on every pass and management is SUSPENDED, loudly, rather
                    than run on stale input.

  Restart recovery. An open position must survive a process restart. State is
                    checkpointed after every event that changes it, and on
                    start-up the desk is rehydrated from that checkpoint before
                    the first tick is consumed. Without this a crash mid-trade
                    leaves a live position nobody is managing.

  Bar boundaries.   A bar is processed exactly ONCE, on its close, and never
                    while forming. Reprocessing a bar re-runs the analyst on a
                    state that already produced a decision.

  Weekend/halt.     The desk stops looking for entries when the venue is shut,
                    and says so, instead of accumulating refusals against a
                    frozen tape.

SHADOW REMAINS THE DEFAULT. Promotion to live is a flag on the desk, not a
different program.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .features import Bar, atr, classify, swings, visible_swings
from .feed import FeedConfig, FeedError, LiveFeed, RealMt5Client
from .ledger import Ledger
from .live import ENTRY_TF, HTF, LiveDesk, Vision
from .management import BrokerLimits
from .notify import build_sink
from .runner import build_brief

log = logging.getLogger(__name__)

SERVICE_VERSION = "service-2026-08-14-a"


@dataclass
class ServiceConfig:
    symbol: str = "XAUUSD"
    entry_tf: str = ENTRY_TF
    htf: str = HTF
    history_bars: int = 600
    poll_seconds: float = 1.0
    # Reconnect backoff, bounded so a long outage does not become a long sleep.
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 120.0
    # A tick older than this means the feed is not advancing. Management is
    # suspended rather than run against a fossil price.
    max_tick_age_s: float = 30.0
    # Nothing is looked at while the venue is shut; measured from tick advance,
    # not from a hardcoded calendar, so holidays need no maintenance.
    halt_after_silence_s: float = 900.0
    state_path: Path = Path("state/service_state.json")
    ledger_path: Path = Path("state/ledger.jsonl")
    heartbeat_every_s: float = 900.0


@dataclass
class ServiceState:
    """Everything needed to resume mid-trade after a restart."""
    version: str = SERVICE_VERSION
    last_bar_ts: Optional[str] = None
    open_trade: Optional[dict] = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    restarts: int = 0
    ticks_seen: int = 0
    bars_processed: int = 0
    reconnects: int = 0
    stale_suspensions: int = 0


class DeskService:
    """Supervised 24/5 loop. One symbol, one desk, one process."""

    def __init__(self, desk: LiveDesk, feed: LiveFeed, cfg: ServiceConfig = ServiceConfig()):
        self.desk, self.feed, self.cfg = desk, feed, cfg
        self.state = ServiceState()
        self._stop = False
        self._last_tick_advance = time.monotonic()
        self._last_heartbeat = 0.0
        self._bars: list[Bar] = []
        self._sw: list = []
        self._atrs: list = []
        self._rehydrated = False
        self._venue_shut = False
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)

    # -- lifecycle -------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            log.warning("signal %s received — finishing the current pass and "
                        "checkpointing before exit", signum)
            self._stop = True
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, handle)
            except (ValueError, OSError):
                pass                     # not the main thread; supervisor handles it

    def load_state(self) -> ServiceState:
        p = self.cfg.state_path
        if not p.exists():
            return self.state
        try:
            raw = json.loads(p.read_text())
            self.state = ServiceState(**{k: v for k, v in raw.items()
                                         if k in ServiceState.__dataclass_fields__})
            self.state.restarts += 1
            log.warning("resumed from checkpoint: restart #%d, last bar %s, "
                        "open trade %s", self.state.restarts, self.state.last_bar_ts,
                        "YES" if self.state.open_trade else "no")
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.error("checkpoint unreadable (%s) — starting fresh. An open "
                      "position may be UNMANAGED; check the terminal.", e)
        return self.state

    def checkpoint(self) -> None:
        """Written after every state-changing event, not on a timer.

        A timer-based checkpoint loses exactly the events that matter most —
        the ones immediately before a crash.
        """
        t = self.desk.open
        # The COMPILED SIGNAL is checkpointed in full, not just its tp2. Without
        # it a restart cannot rebuild an OpenTrade at all, and the previous
        # version silently didn't — it reconstructed a Position and an observer,
        # never assigned desk.open, and then appended to the risk ledger anyway.
        # The result was the worst possible state: the risk engine believed a
        # trade existed, LiveDesk believed none did, and nothing managed the
        # position that was actually live at the broker.
        self.state.open_trade = None if t is None else {
            "position": {
                "direction": t.position.direction, "entry": t.position.entry,
                "initial_stop": t.position.initial_stop,
                "current_stop": t.position.current_stop,
                "risk_price": t.position.risk_price,
                "remaining_fraction": t.position.remaining_fraction,
                "banked_r": t.position.banked_r,
                "opened_utc": t.position.opened_utc.isoformat(),
                "setup": t.position.setup},
            "signal": {k: (v.value if hasattr(v, "value") else
                           (v.isoformat() if hasattr(v, "isoformat") else v))
                       for k, v in t.signal.__dict__.items()},
            "opened_idx": t.opened_idx,
            "mechanism_name": t.mechanism_name,
            "entry_context": t.entry_context,
            "observer": {"mfe_r": t.observer.mfe_r, "mae_r": t.observer.mae_r,
                         "ticks": t.observer.ticks},
            "mgmt_log": t.mgmt_log}
        tmp = self.cfg.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2, default=str))
        os.replace(tmp, self.cfg.state_path)

    def rehydrate(self) -> bool:
        """Restore the open position INTO LiveDesk, exactly once.

        "Exactly once" is load-bearing. rehydrate() is called after every
        successful reconnect, and the previous version appended to the risk
        ledger on each call, so a flapping connection inflated open risk without
        opening a single trade.

        Excursion history before the restart is restored from the checkpoint;
        the tick-by-tick path is genuinely lost and is not fabricated.
        """
        if self._rehydrated:
            return self.desk.open is not None
        raw = self.state.open_trade
        if not raw:
            self._rehydrated = True
            return False

        from .analyst import CompiledSignal, Setup
        from .live import OpenTrade
        from .management import Position
        from .observer import TradeObserver

        try:
            pr = raw["position"]
            pos = Position(
                direction=pr["direction"], entry=pr["entry"],
                initial_stop=pr["initial_stop"], current_stop=pr["current_stop"],
                risk_price=pr["risk_price"],
                remaining_fraction=pr["remaining_fraction"],
                banked_r=pr["banked_r"],
                opened_utc=datetime.fromisoformat(pr["opened_utc"]),
                setup=pr.get("setup", "UNKNOWN"))
            sg = dict(raw["signal"])
            sg["setup"] = Setup(sg["setup"])
            sg["brief_as_of"] = datetime.fromisoformat(sg["brief_as_of"])
            sig = CompiledSignal(**sg)
            obs = TradeObserver(pos.direction, pos.entry, pos.current_stop,
                                sig.tp2, pos.risk_price, pos.opened_utc)
            ob = raw.get("observer") or {}
            obs.mfe_r = ob.get("mfe_r", 0.0)
            obs.mae_r = ob.get("mae_r", 0.0)
            self.desk.open = OpenTrade(
                pos, sig, raw.get("opened_idx", 0), obs,
                entry_context=raw.get("entry_context") or {},
                mechanism_name=raw.get("mechanism_name", "unnamed"),
                mgmt_log=list(raw.get("mgmt_log") or []))
        except (KeyError, TypeError, ValueError) as e:
            # A position that cannot be rebuilt must NOT leave a phantom risk
            # entry behind. Say so loudly — there may be a live trade at the
            # broker that this process can no longer manage.
            log.error("checkpoint present but unrestorable (%s). If a position is "
                      "open at the broker, THIS PROCESS IS NOT MANAGING IT.", e)
            self._rehydrated = True
            return False

        self.desk.risk.open_risks.append(1.0)
        self.desk.risk.open_directions.append(pos.direction)
        self._rehydrated = True
        log.warning("REHYDRATED %s from %.2f, stop %.2f, tp2 %.2f, %.0f%% remaining, "
                    "banked %+.2fR", pos.direction, pos.entry, pos.current_stop,
                    sig.tp2, pos.remaining_fraction * 100, pos.banked_r)
        self._notify(f"*RESUMED* {pos.direction} from {pos.entry:.2f}, "
                     f"SL {pos.current_stop:.2f}, {pos.remaining_fraction:.0%} left")
        return True

    # -- the loop --------------------------------------------------------
    def run(self, max_seconds: Optional[float] = None) -> ServiceState:
        self.install_signal_handlers()
        self.load_state()
        started = time.monotonic()
        backoff = self.cfg.backoff_initial_s
        self._notify(f"*DESK STARTING* {self.cfg.symbol} · {SERVICE_VERSION} · "
                     f"{'SHADOW' if self.desk.shadow else 'LIVE'} · "
                     f"restart #{self.state.restarts}")

        while not self._stop:
            if max_seconds and (time.monotonic() - started) > max_seconds:
                log.info("max_seconds reached — exiting cleanly")
                break
            try:
                if not self.feed.connect():
                    raise FeedError("connect() returned falsy")
                backoff = self.cfg.backoff_initial_s
                self._warm()
                self.rehydrate()
                self._inner_loop(started, max_seconds)
            except FeedError as e:
                self.state.reconnects += 1
                self.checkpoint()
                log.error("feed error (%s) — reconnecting in %.0fs "
                          "[reconnect #%d]", e, backoff, self.state.reconnects)
                self._notify(f"*FEED LOST* {e} — retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.backoff_max_s)
            except Exception as e:                    # never die silently
                self.checkpoint()
                log.exception("unexpected error in service loop: %s", e)
                self._notify(f"*DESK ERROR* {type(e).__name__}: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.backoff_max_s)

        self.checkpoint()
        self._notify(f"*DESK STOPPED* {self.state.bars_processed} bars, "
                     f"{self.state.ticks_seen} ticks, "
                     f"{self.state.reconnects} reconnect(s)")
        return self.state

    def _inner_loop(self, started: float, max_seconds: Optional[float]) -> None:
        last_price = None
        while not self._stop:
            if max_seconds and (time.monotonic() - started) > max_seconds:
                return
            # ---- staleness: the failure that looks like success ----------
            stale, tick_age = self.feed.tick_is_stale()
            if stale:
                # A CLOSED MARKET AND A BROKEN FEED LOOK IDENTICAL from here:
                # both are "the tick stopped advancing". They demand opposite
                # responses — one is a Sunday evening, the other is an incident —
                # so they are separated by HOW long the silence has run.
                #
                # This matters more than it sounds. Over a weekend a desk that
                # logs "FEED STALE — management suspended" every minute is
                # indistinguishable from the tuple-truthiness bug that made it do
                # exactly that on a healthy feed, and the natural reaction is to
                # go hunting for a bug that is not there.
                if tick_age >= self.cfg.halt_after_silence_s:
                    if not self._venue_shut:
                        self._venue_shut = True
                        log.info("no tick for %.0fs — VENUE APPEARS CLOSED. This is "
                                 "normal outside market hours and is not an error; "
                                 "the desk will resume by itself when quotes return.",
                                 tick_age)
                else:
                    self.state.stale_suspensions += 1
                    if self.state.stale_suspensions % 60 == 1:
                        log.warning("tick is stale (%.1fs) DURING MARKET HOURS — "
                                    "management SUSPENDED. The desk will not act on "
                                    "a price that stopped advancing.", tick_age)
                        self._notify("*FEED STALE* management suspended until the "
                                     "quote advances")
                time.sleep(self.cfg.poll_seconds)
                continue
            if self._venue_shut:
                self._venue_shut = False
                log.info("quotes resumed after %.0fs — desk active", tick_age)
                self._notify("*MARKET OPEN* quotes resumed, desk active")

            bid, ask, age = self.feed.quote()
            price = (bid + ask) / 2.0
            self.desk.last_bid, self.desk.last_ask = bid, ask
            self.desk.last_spread = max(0.0, ask - bid)
            if price != last_price:
                self._last_tick_advance = time.monotonic()
                last_price = price
            self.state.ticks_seen += 1

            # ---- venue halt: measured, not from a calendar ---------------
            silent = time.monotonic() - self._last_tick_advance
            if silent > self.cfg.halt_after_silence_s:
                if self.state.ticks_seen % 300 == 0:
                    log.info("no tick advance for %.0fs — venue appears shut", silent)
                time.sleep(self.cfg.poll_seconds)
                continue

            # ---- TICK PATH: continuous observation of an open position ---
            if self.desk.open is not None:
                # bid/ask, not mid: the desk evaluates a long's exits on the bid
                # and a short's on the ask.
                out = self.desk.on_tick(price, datetime.now(timezone.utc),
                                        bid=bid, ask=ask)
                if out:
                    self.checkpoint()

            # ---- BAR PATH: exactly once, on close ------------------------
            self._maybe_close_bar()

            if time.monotonic() - self._last_heartbeat > self.cfg.heartbeat_every_s:
                self._last_heartbeat = time.monotonic()
                log.info("alive: %d ticks, %d bars, open=%s, spread %.2f",
                         self.state.ticks_seen, self.state.bars_processed,
                         bool(self.desk.open), self.desk.last_spread or 0.0)
            time.sleep(self.cfg.poll_seconds)

    def _warm(self) -> None:
        self._bars = list(self.feed.bars(self.cfg.entry_tf, self.cfg.history_bars))
        self._sw = swings(self._bars)
        self._atrs = atr(self._bars)
        log.info("warmed with %d %s bars, last close %s", len(self._bars),
                 self.cfg.entry_tf, self._bars[-1].ts if self._bars else "-")

    def _maybe_close_bar(self) -> None:
        """Process a bar exactly once, on its close, never while forming."""
        bars = self.feed.bars(self.cfg.entry_tf, self.cfg.history_bars)
        if len(bars) < 2:
            return
        # LiveFeed.bars() drops the forming bar itself and documents that it
        # returns CLOSED bars only, so bars[-1] IS the most recent closed bar.
        # Taking bars[-2] dropped it a second time and analysed the one before,
        # leaving the desk a full entry-timeframe candle behind the market while
        # every timestamp still looked correct.
        closed = bars[-1]
        if self.state.last_bar_ts == closed.ts.isoformat():
            return
        self._bars = list(bars)
        self._sw = swings(self._bars)
        self._atrs = atr(self._bars)
        i = len(self._bars) - 1
        htf_state = self._htf_state()
        bid, ask, age = self.feed.quote()
        try:
            self.desk.on_bar(self._bars, i, visible_swings(self._sw, i), self._atrs,
                             htf_state, (bid, ask, age),
                             (f"{self.cfg.entry_tf} close {closed.ts:%Y-%m-%d %H:%M}",))
        except Exception as e:
            log.exception("on_bar failed at %s: %s", closed.ts, e)
        self.state.last_bar_ts = closed.ts.isoformat()
        self.state.bars_processed += 1
        self.checkpoint()

    def _htf_state(self):
        """Higher-timeframe structure from TRUE aggregation, never sampling."""
        try:
            htf_bars = self.feed.bars(self.cfg.htf, 200)
            if len(htf_bars) < 30:
                return None
            # Same contract, same fix. Dropping one more here made the higher
            # timeframe a whole H4 candle stale — worse than supplying no HTF
            # state at all, because it is convincingly formatted and wrong.
            closed = list(htf_bars)
            hsw, hatrs = swings(closed), atr(closed)
            return classify(closed, len(closed) - 1, visible_swings(hsw, len(closed) - 1),
                            hatrs)
        except Exception as e:
            log.debug("htf state unavailable: %s", e)
            return None

    def _notify(self, text: str) -> None:
        try:
            self.desk._notify(text)
        except Exception:
            pass


# --------------------------------------------------------------------------

def build_service(*, symbol: str = "XAUUSD", shadow: bool = True,
                  provider_spec: str = "anthropic:claude-opus-5",
                  vision: Vision = Vision.NUMERIC_PLUS_CHARTS,
                  cfg: Optional[ServiceConfig] = None,
                  secrets_dir: str = "secrets") -> DeskService:
    """Wire the real client, feed, desk and sink. One call, one deployed desk."""
    from .providers import build_provider
    cfg = cfg or ServiceConfig(symbol=symbol)
    client = RealMt5Client()
    feed = LiveFeed(client, FeedConfig(symbol=symbol,
                                       max_tick_age_s=cfg.max_tick_age_s))
    feed.connect()
    broker = BrokerLimits()
    try:
        info = client.symbol_info(symbol)
        if info is not None:
            broker = BrokerLimits.from_symbol_info(info)
            log.info("broker limits: min stop %.2f, freeze %.2f",
                     broker.min_stop_distance, broker.freeze_distance)
    except Exception as e:
        log.warning("could not read symbol limits (%s) — stop legality will use "
                    "the through-the-market test only", e)
    # A REAL sink. build_sink(None) returns a null sink, so the one-call
    # constructor produced a desk that could never notify anything — including
    # with shadow=False, where silence is the last thing you want. build_sink
    # reads secrets/telegram_token and secrets/telegram_chat_id and degrades to
    # null on its own if they are absent, so passing the path is safe either way.
    sink = build_sink(secrets_dir)
    desk = LiveDesk(build_provider(provider_spec), Ledger(cfg.ledger_path),
                    sink, shadow=shadow, vision=vision, broker=broker)
    log.info("notification sink: %s", type(sink).__name__)
    return DeskService(desk, feed, cfg)
