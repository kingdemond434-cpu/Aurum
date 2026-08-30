"""The continuously supervised desk process (24/7 process, 24/5 gold venue).

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
from .live import ENTRY_TF, HTF, LiveDesk, Vision, _downsample_path
from .management import BrokerLimits
from .notify import build_sink
from .runner import build_brief

log = logging.getLogger(__name__)

SERVICE_VERSION = "service-2026-08-14-a"

# Entry-timeframe lengths, for clock-gating the bar request. Only used to decide
# WHETHER to ask; the answer still has to pass the last_bar_ts guard.
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240,
              "D1": 1440}


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
    # Cadence once the venue is judged shut. Nothing can change until quotes
    # return, so polling every second all weekend is ~200k requests that cannot
    # produce information. 60s still notices Sunday open within a minute.
    closed_poll_seconds: float = 60.0
    # Cadence with NO position open. The tick path exists to observe an open
    # position continuously — giveback, profit-lock, trailing, protective moves.
    # With nothing open there is nothing to observe: entries are decided on bar
    # close, so a one-second quote poll while flat produces 900 requests per M15
    # bar to answer a question asked once. Fast polling resumes the instant a
    # position opens.
    idle_poll_seconds: float = 15.0
    # How early to start asking for the next closed bar. Venues publish a candle
    # a moment after its boundary; asking slightly early is one wasted request,
    # asking late delays every entry by that much.
    bar_poll_lead_s: float = 5.0
    # Higher-timeframe structure is refreshed on its OWN cadence, not on every
    # entry bar. This must be a meaningful fraction of the HTF PERIOD or it does
    # nothing: at 300s it expired between every M15 close and saved zero
    # requests. An hour still refreshes H4 context four times per H4 candle,
    # which is ample, while cutting the request rate sixteen-fold.
    htf_cache_seconds: float = 3600.0
    # Reject implausible prints before they can trip a stop, and keep every
    # accepted tick. See tickguard.py — one bad quote writes a fabricated loss
    # into the ledger, and the ledger is the only evidence this desk has.
    guard_ticks: bool = True
    archive_ticks: bool = True
    tick_archive_dir: Path = Path("data/ticks")
    state_path: Path = Path("state/service_state.json")
    ledger_path: Path = Path("state/ledger.jsonl")
    heartbeat_every_s: float = 900.0
    # Presence of this file stands the desk down. A FILE rather than a signal or
    # an in-process flag, so it survives a restart, can be set from the Telegram
    # bot or by hand with `touch`, and is legible during an incident with `ls`.
    # See golddesk/bot.py — a halt command nothing reads is theatre.
    halt_path: Path = Path("state/HALTED")


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
        self._halted = False
        self._notify_errors = 0
        self._htf_cache = None
        self._htf_cached_at = 0.0
        from .tickguard import TickArchive, TickGuard
        self.guard = TickGuard() if self.cfg.guard_ticks else None
        self.archive = (TickArchive(self.cfg.tick_archive_dir, self.cfg.symbol)
                        if self.cfg.archive_ticks else None)
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
            raw = json.loads(p.read_text(encoding='utf-8'))
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
            # THE WHOLE OBSERVER, not two of its fields.
            #
            # This wrote mfe_r/mae_r/ticks and rehydrate() read back only the
            # first two, so `ticks` reset to 0 on every restart -- which is
            # literally the "0 observations" printed on an exit message. Worse,
            # `path`, `t_mfe` and `t_mae` were never written at all: the full
            # excursion path and both time-to-extreme stamps were destroyed by
            # any restart, and this desk restarts on every logon, every
            # watchdog relaunch and every deploy.
            #
            # That is not a cosmetic loss. The path IS the forward evidence --
            # time-to-MFE, time-to-MAE, whether +0.5R came before -1R, how much
            # of MFE was captured. A desk cannot learn whether a 16-point
            # structural stop beats a tight one from a record that resets to
            # zero every few hours. Telemetry only: nothing here gates a trade.
            "observer": {"mfe_r": t.observer.mfe_r, "mae_r": t.observer.mae_r,
                         "ticks": t.observer.ticks,
                         "t_mfe": (t.observer.t_mfe.isoformat()
                                   if t.observer.t_mfe else None),
                         "t_mae": (t.observer.t_mae.isoformat()
                                   if t.observer.t_mae else None),
                         "last_price": t.observer.last_price,
                         "last_ts": (t.observer.last_ts.isoformat()
                                     if t.observer.last_ts else None),
                         "velocity_r_per_min": t.observer.velocity_r_per_min,
                         # Bounded the same way the ledger bounds it, so a
                         # long tick-driven trade cannot grow the checkpoint
                         # without limit. Both extremes are pinned by
                         # _downsample_path, so the turning points survive.
                         "path": _downsample_path(t.observer.path)},
            "mgmt_log": t.mgmt_log}
        # DELIVERY HEALTH GOES IN THE CHECKPOINT. The message is this desk's
        # only product, so "is the channel working" belongs beside "is there a
        # position" rather than in a log line nobody greps. The bot reads it.
        payload = asdict(self.state)
        payload["notification_health"] = self.notification_health()
        tmp = self.cfg.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.cfg.state_path)

    def rehydrate(self) -> bool:
        """Restore the open position INTO LiveDesk, exactly once.

        "Exactly once" is load-bearing. rehydrate() is called after every
        successful reconnect, and the previous version appended to the risk
        ledger on each call, so a flapping connection inflated open risk without
        opening a single trade.

        The FULL excursion record is restored: both extremes, both time-to-
        extreme stamps, the observation count and the (bounded) tick-by-tick
        path. This docstring used to say "the tick-by-tick path is genuinely
        lost and is not fabricated" -- accurate when written, and the reason
        the loss went unquestioned for so long. It described a deliberate
        choice, but checkpoint() was not even persisting `ticks` back, so an
        exit could report "0 observations" on a trade that had run for hours,
        and time-to-MFE/MAE vanished on every logon, watchdog relaunch and
        deploy. Nothing is fabricated now either: an unparseable point is
        DROPPED rather than coerced, and a checkpoint from an older build
        restores exactly as it used to.
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
            # RESTORE THE REST. `ticks` was being WRITTEN by checkpoint() and
            # never read here, so it came back 0 on every restart -- the "0
            # observations" on the exit message. Each field below is restored
            # defensively: a checkpoint written by an older build has none of
            # them, and a missing field must degrade to the old behaviour
            # rather than raise and lose the whole position.
            obs.ticks = int(ob.get("ticks") or 0)
            obs.velocity_r_per_min = float(ob.get("velocity_r_per_min") or 0.0)
            lp = ob.get("last_price")
            obs.last_price = float(lp) if lp is not None else None
            for attr in ("t_mfe", "t_mae", "last_ts"):
                raw_ts = ob.get(attr)
                if raw_ts:
                    try:
                        setattr(obs, attr, datetime.fromisoformat(raw_ts))
                    except ValueError:
                        log.warning("checkpoint %s unparseable (%r) — left unset "
                                    "rather than guessed", attr, raw_ts)
            # The path is stored as [iso, r] pairs. Anything unparseable is
            # DROPPED rather than coerced: a fabricated point in an excursion
            # path is worse than a shorter one.
            restored = []
            for pt in (ob.get("path") or []):
                try:
                    ts_s, r = pt
                    restored.append((datetime.fromisoformat(ts_s), float(r)))
                except (TypeError, ValueError):
                    continue
            obs.path = restored
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
                # connect() signals failure by RAISING FeedError (after its own
                # internal retry loop) -- it has no meaningful truthy return on
                # success, so this must never gate on its return value. It used
                # to: `if not self.feed.connect(): raise FeedError(...)`, which
                # fired on every successful connect (an implicit `None` return
                # is falsy) and made the live loop unable to ever get past this
                # line. Invisible to tests because the fake feed's connect()
                # returned True, a contract the real one never had.
                self.feed.connect()
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
        # Flush and close the archive on the way out. A day's ticks sitting in a
        # buffer that was never flushed is the same as not having collected them.
        if self.archive is not None:
            self.archive.close()
            log.info(self.archive.render())
        if self.guard is not None and self.guard.stats.seen:
            log.info("tick guard:\n%s", self.guard.stats.render())
        self._notify(f"*DESK STOPPED* {self.state.bars_processed} bars, "
                     f"{self.state.ticks_seen} ticks, "
                     f"{self.state.reconnects} reconnect(s)")
        return self.state

    def _inner_loop(self, started: float, max_seconds: Optional[float]) -> None:
        last_price = None
        while not self._stop:
            if max_seconds and (time.monotonic() - started) > max_seconds:
                return

            # ---- STAND DOWN IF ASKED -----------------------------------
            #
            # Checked here, above the quote, because everything below this line
            # either acts on price or spends a request to fetch it. A halt that
            # only took effect at the next bar close would leave up to fifteen
            # minutes between the operator asking the desk to stop and it
            # stopping, which is the entire duration of the incident they are
            # halting for.
            #
            # The desk keeps RUNNING — connection alive, state intact — it just
            # decides nothing. Exiting the process instead would mean the way
            # back is an SSH session rather than a chat message, and would drop
            # the rehydration state that makes a mid-trade restart safe.
            halted = self.cfg.halt_path.exists()
            if halted != self._halted:
                self._halted = halted
                if halted:
                    log.warning("HALT FLAG SET (%s) — the desk will decide nothing "
                                "until it is cleared. Any position you hold is "
                                "untouched; this desk has never placed an order.",
                                self.cfg.halt_path)
                    self._notify("*DESK HALTED* standing down — no new signals. "
                                 "Your open trades are untouched. /resume to clear.")
                else:
                    log.info("halt flag cleared — desk active")
                    self._notify("*DESK RESUMED* watching again.")
            if halted:
                time.sleep(self.cfg.idle_poll_seconds)
                continue

            # ---- ONE quote per iteration -------------------------------
            #
            # This used to be `tick_is_stale()` immediately followed by
            # `quote()` — and tick_is_stale() is implemented AS a quote() call,
            # so every pass fetched the same tick twice. Against a local MT5
            # terminal that is a wasted memcpy. Against a remote REST API it is
            # double the request rate, double the latency exposure and double
            # the rate-limit budget, forever, for no information.
            try:
                bid, ask, tick_age = self.feed.quote()
            except FeedError:
                raise
            stale = tick_age > self.cfg.max_tick_age_s
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
                # A CLOSED VENUE IS NOT A BUSY LOOP. Polling a REST API once a
                # second all weekend is ~200k pointless requests between Friday
                # close and Sunday open. Nothing can change until quotes return,
                # and quotes returning is not something a faster poll detects
                # sooner in any way that matters on an M15 desk.
                time.sleep(self.cfg.closed_poll_seconds if self._venue_shut
                           else self.cfg.poll_seconds)
                continue
            if self._venue_shut:
                self._venue_shut = False
                log.info("quotes resumed after %.0fs — desk active", tick_age)
                self._notify("*MARKET OPEN* quotes resumed, desk active")

            # ---- REJECT BAD PRINTS BEFORE THEY REACH THE POSITION --------
            #
            # This sits above everything that acts on price. The tick path
            # evaluates stops and targets, so a single bad quote closes a trade
            # that was never closed and writes a fabricated loss the ledger
            # cannot distinguish from a real one. Rejecting a good tick costs
            # one poll; accepting a bad one corrupts the evidence permanently.
            now_utc = datetime.now(timezone.utc)
            if self.guard is not None:
                ok, why = self.guard.check(bid, ask, now_utc)
                if not ok:
                    if self.archive is not None:
                        self.archive.write_reject(bid, ask, now_utc, why)
                    # Log the first few and then every hundredth: a feed that
                    # starts rejecting steadily is a problem worth seeing, and
                    # one that rejects a print an hour is noise.
                    n = self.guard.stats.rejected
                    if n <= 5 or n % 100 == 0:
                        log.warning("REJECTED TICK (%d total, %.3f%% of stream): %s",
                                    n, self.guard.stats.reject_rate * 100, why)
                    time.sleep(self.cfg.poll_seconds)
                    continue
            if self.archive is not None:
                self.archive.write(bid, ask, now_utc)

            age = tick_age
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
                time.sleep(self.cfg.closed_poll_seconds)
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
            # Gated on the CLOCK before it is gated on the data. A new M15 bar
            # can only close at :00/:15/:30/:45, so asking the venue for several
            # hundred candles at 14:03:07 cannot possibly return one the desk
            # has not seen. The old loop asked anyway, once a second — the single
            # largest and least useful request the service made.
            if self._bar_boundary_passed():
                self._maybe_close_bar(quote=(bid, ask, age))

            if time.monotonic() - self._last_heartbeat > self.cfg.heartbeat_every_s:
                self._last_heartbeat = time.monotonic()
                log.info("alive: %d ticks, %d bars, open=%s, spread %.2f",
                         self.state.ticks_seen, self.state.bars_processed,
                         bool(self.desk.open), self.desk.last_spread or 0.0)
            # Fast while managing, slow while flat. The one thing that must not
            # be slowed is observation of an open position, and that is exactly
            # what this keeps at full rate.
            time.sleep(self.cfg.poll_seconds if self.desk.open_trades
                       else self.cfg.idle_poll_seconds)

    def _bar_boundary_passed(self) -> bool:
        """Could a new entry-timeframe bar plausibly have closed since the last?

        Pure clock arithmetic, no network. Returns True in a short window after
        each boundary so the venue has time to publish the closed candle, and
        True whenever the desk has not processed a bar yet.

        Deliberately CONSERVATIVE: it is far better to ask once too often than
        to miss a close, so the window is generous and the data-level guard in
        _maybe_close_bar (last_bar_ts) remains the thing that guarantees a bar
        is processed exactly once. This only removes requests that could not
        possibly have returned new information.
        """
        mins = TF_MINUTES.get(self.cfg.entry_tf)
        if not mins:
            return True                       # unknown timeframe: never gate
        if not self.state.last_bar_ts:
            return True
        now = datetime.now(timezone.utc)
        try:
            last = datetime.fromisoformat(self.state.last_bar_ts)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        # The close of the bar AFTER the one we last processed.
        due = last + timedelta(minutes=2 * mins)
        return now >= due - timedelta(seconds=self.cfg.bar_poll_lead_s)

    def _warm(self) -> None:
        self._bars = list(self.feed.bars(self.cfg.entry_tf, self.cfg.history_bars))
        self._sw = swings(self._bars)
        self._atrs = atr(self._bars)
        log.info("warmed with %d %s bars, last close %s", len(self._bars),
                 self.cfg.entry_tf, self._bars[-1].ts if self._bars else "-")

    def _maybe_close_bar(self, quote: Optional[tuple] = None) -> None:
        """Process a bar exactly once, on its close, never while forming.

        `quote` is the one the loop already fetched this iteration. Passing it
        avoids a third round trip for a tick that is at most one poll old — on
        an M15 desk that difference cannot change a decision, and the request
        cost is paid on every close.
        """
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
        bid, ask, age = quote if quote is not None else self.feed.quote()
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
        """Higher-timeframe structure from TRUE aggregation, never sampling.

        CACHED. At M15 entry and H4 context, the H4 structure is identical
        across sixteen consecutive M15 closes, so re-requesting 200 H4 candles
        each time bought nothing and cost a large request. The cache expires on
        time rather than on count so an unusual timeframe pairing cannot make it
        stale by accident.
        """
        now = time.monotonic()
        if (self._htf_cache is not None
                and now - self._htf_cached_at < self.cfg.htf_cache_seconds):
            return self._htf_cache
        try:
            htf_bars = self.feed.bars(self.cfg.htf, 200)
            if len(htf_bars) < 30:
                return None
            # Same contract, same fix. Dropping one more here made the higher
            # timeframe a whole H4 candle stale — worse than supplying no HTF
            # state at all, because it is convincingly formatted and wrong.
            closed = list(htf_bars)
            hsw, hatrs = swings(closed), atr(closed)
            st = classify(closed, len(closed) - 1,
                          visible_swings(hsw, len(closed) - 1), hatrs)
            self._htf_cache, self._htf_cached_at = st, now
            return st
        except Exception as e:
            log.debug("htf state unavailable: %s", e)
            # Do NOT cache a failure as "no HTF context" — a transient error
            # would then suppress higher-timeframe state for the whole window.
            return None

    def _notify(self, text: str) -> None:
        """Never propagates — but no longer discards.

        This was `except Exception: pass`, which meant a revoked bot or a wrong
        chat id produced a desk that ran perfectly and delivered nothing, with
        no trace anywhere. Aurum places no orders; the message IS the product,
        so a silently dead channel is not a degraded mode, it is total failure
        wearing a healthy process.

        The exception still cannot reach the loop. It is now COUNTED, and
        `notification_health()` surfaces the counters to the checkpoint and to
        the bot's /status.
        """
        try:
            self.desk._notify(text)
        except Exception as e:                        # noqa: BLE001
            self._notify_errors += 1
            log.warning("notification failed (%d total): %s", self._notify_errors, e)

    def notification_health(self) -> dict:
        """What the channel has actually delivered. Read by the checkpoint.

        Walks to the wrapped sink rather than asking the desk, because the desk
        holds whatever `build_sink` returned and only the wrapper counts.
        """
        sink = getattr(self.desk, "sink", None) or getattr(self.desk, "_sink", None)
        stats = sink.stats() if hasattr(sink, "stats") else {
            "sink": type(sink).__name__,
            "note": "this sink does not track delivery; health is UNKNOWN, "
                    "which is not the same as healthy"}
        stats["notify_exceptions"] = self._notify_errors
        return stats


# --------------------------------------------------------------------------

def build_service(*, symbol: str = "XAUUSD", shadow: bool = True,
                  provider_spec: str = "anthropic:claude-opus-5",
                  provider_effort: Optional[str] = None,
                  vision: Vision = Vision.NUMERIC_PLUS_CHARTS,
                  cfg: Optional[ServiceConfig] = None,
                  secrets_dir: str = "secrets",
                  feed_backend: str = "mt5",
                  management: str = "heuristic",
                  shadow_management: bool = True,
                  shadow_contextual: bool = False,
                  universe_mode: bool = False,
                  calendar=None,
                  spread_profile_path: str = "config/spread_profile.json",
                  declared_spread: Optional[float] = None,
                  broker_limits: Optional[BrokerLimits] = None,
                  enable_macro: bool = True,
                  wake_on_bar_close: bool = False,
                  fallback_provider_specs: Sequence[str] = ("codex:gpt-5.6-sol",),
                  specialists: Optional[dict] = None) -> DeskService:
    """Wire the real client, feed, desk and sink. One call, one deployed desk.

    `feed_backend` selects where PERCEPTION comes from. It does not select where
    cost and stop-legality come from: those are facts about the venue you
    execute on, and `broker_limits` carries them explicitly precisely so a
    non-MT5 feed cannot quietly supply its own.
    """
    from .providers import build_provider_chain
    from .specialists import build_desk_council
    cfg = cfg or ServiceConfig(symbol=symbol)
    if feed_backend == "oanda":
        from .feed_oanda import OandaClient
        client = OandaClient(instrument=os.environ.get("OANDA_INSTRUMENT", "XAU_USD"))
    elif feed_backend == "yahoo":
        # ZERO-SETUP feed. No account, no key. It publishes no bid/ask, so the
        # quote is synthesised from YOUR declared spread — which is why this
        # backend REQUIRES --declared-spread and refuses without one.
        from .feed_yahoo import YahooClient
        if not declared_spread:
            raise SystemExit(
                "--feed yahoo requires --declared-spread. That feed publishes "
                "OHLC only, so the desk has to build the bid/ask from a number "
                "you supply. Inventing one would be inventing the single figure "
                "that decides whether marginal trades are worth taking.")
        client = YahooClient(symbol=symbol, half_spread=declared_spread / 2.0)
    else:
        client = RealMt5Client()
    feed = LiveFeed(client, FeedConfig(symbol=symbol,
                                       max_tick_age_s=cfg.max_tick_age_s))
    feed.connect()
    broker = broker_limits
    if broker is None:
        try:
            info = client.symbol_info(symbol)
            if info is not None:
                broker = BrokerLimits.from_symbol_info(info)
        except Exception as e:
            log.warning("could not read symbol limits (%s)", e)
        broker = broker or BrokerLimits()
    if feed_backend != "mt5" and not broker.min_stop_distance:
        log.warning("feed=%s supplies no venue stop limits and none were passed. "
                    "Stop legality falls back to the through-the-market test only. "
                    "Read trade_stops_level from YOUR MT5 terminal and pass "
                    "broker_limits=BrokerLimits(min_stop_distance=...).", feed_backend)
    log.info("broker limits: min stop %.2f, freeze %.2f",
             broker.min_stop_distance, broker.freeze_distance)
    # A REAL sink. build_sink(None) returns a null sink, so the one-call
    # constructor produced a desk that could never notify anything — including
    # with shadow=False, where silence is the last thing you want. build_sink
    # reads secrets/telegram_token and secrets/telegram_chat_id and degrades to
    # null on its own if they are absent, so passing the path is safe either way.
    # YOUR VENUE'S COSTS. Perception is OANDA/MT5; you execute elsewhere by
    # hand. Without this the compiler prices every trade against the feed's
    # spread — a cost you will not pay, and usually a smaller one.
    from .costs import CostModel
    from .venue import SpreadProfile
    if declared_spread:
        profile = SpreadProfile.declared("declared", declared_spread)
    else:
        profile = SpreadProfile.load(Path(spread_profile_path))
    cost_model = CostModel(spread_profile=profile)
    if not profile.calibrated:
        log.warning("NO SPREAD PROFILE — costs will be taken from the FEED, "
                    "which is not your execution venue. Every expectancy figure "
                    "is priced against a spread you will not pay. Set "
                    "--declared-spread or calibrate one.")
    else:
        log.info("spread profile: %s (%s)", profile.venue, profile.statistic)

    sink = build_sink(secrets_dir)

    # Event proximity, computed rather than fetched. Wired here so it is on by
    # default: uncertainty.event_risk() reported UNKNOWN on every decision the
    # desk ever made purely because nobody passed it a calendar.
    if calendar is None:
        from .calendar import Calendar
        calendar = Calendar()

    # Novelty needs the desk's own resolved history to compare against. Loaded
    # once at boot from the ledger; None until there is enough of it, which the
    # decomposition reports as UNKNOWN rather than as "familiar".
    history = None
    cohorts = None
    rows: list = []          # bound BEFORE the try: the self-audit below reads
                             # it, and an exception here must leave it empty
                             # rather than unbound.
    try:
        from .regime import load_history
        rows = Ledger(cfg.ledger_path).read_all()
        history = load_history(rows) or None
        log.info("regime history: %d resolved trades to compare against",
                 len(history or []))

        # MEASURED COHORTS, FROM THE DESK'S OWN RESOLVED TRADES.
        #
        # THIS IS WHAT MADE THE DESK UNABLE TO LEARN. `build_cohorts` existed,
        # was correct, and was called by adapt.py and acceptance.py -- never by
        # the thing that builds the LIVE desk. So `LiveDesk.cohorts` was None
        # forever, and every consumer silently degraded to its no-history path:
        #
        #   ev_gate      took the COLD_START_PRIOR branch on EVERY decision, no
        #                matter how many trades had resolved -- so a mechanism
        #                with eighty wins was priced exactly like one never
        #                traded
        #   _size        adaptive sizing saw cohort_n=0 and could not size to
        #                measured edge
        #   _edge_r      no measured edge, so execution advice stayed silent
        #   evidence_tier could never reach T1 MEASURED, by construction
        #
        # Every part worked. Nothing joined them, so the desk re-derived
        # ignorance at every boot. The same `rows` was already being read one
        # line above for regime history and then thrown away.
        #
        # Refreshed at BOOT rather than continuously: outcomes resolve over
        # hours, the desk restarts on every logon, watchdog relaunch and deploy,
        # and a cohort that moves mid-session would make two decisions in the
        # same hour incomparable. Boot is frequent enough and is a clean seam.
        from .opportunity import build_cohorts
        cohorts = build_cohorts(rows) or None
        if cohorts:
            top = sorted(cohorts.values(), key=lambda c: -c.n)[:3]
            log.info("cohorts: %d mechanism(s) with resolved history — %s",
                     len(cohorts),
                     ", ".join(f"{c.key} n={c.n} hit {c.hit_rate_shrunk:.0%}"
                               for c in top))
        else:
            log.info("cohorts: NONE resolved yet — every mechanism prices off "
                     "the cold-start prior until trades resolve")
    except Exception as e:
        log.info("no regime history or cohorts yet (%s) — novelty will read "
                 "UNKNOWN and every mechanism stays cold-start", e)

    # MACRO. Built here rather than inside LiveDesk so the desk keeps taking a
    # plain callable and stays testable without a network. None means no feed,
    # and every brief then renders MACRO CONTEXT: UNMEASURED -- the honest
    # state, not a silent omission.
    #
    # This closes the last unwired link on the macro path: macro_context could
    # build a block and MarketBrief could carry one, but nothing ever
    # CONSTRUCTED a provider, so the analyst would have read UNMEASURED forever
    # while every individual part looked correctly wired.
    macro_fn = None
    if enable_macro:
        def macro_fn():
            from .drivers_free import build_drivers
            from .drivers_mt5 import build_from
            from .macro_context import from_drivers
            try:
                points = build_drivers(os.environ.get("FRED_API_KEY"))
            except Exception as e:                     # noqa: BLE001
                # A DEAD WEB FEED MUST NOT MEAN NO MACRO. yfinance broke twice
                # in two days -- "possibly delisted" for DX-Y.NYB/^GSPC/^VIX on
                # 2026-08-27, not importable at all on 2026-08-28 -- and each
                # time the analyst silently lost every macro variable while
                # every component reported healthy.
                log.warning("web driver feed failed (%s) — falling back to the "
                            "execution terminal", e)
                points = {}
            # FILLS GAPS ONLY. A driver the web feed really observed is the
            # actual series and is never overridden by the terminal's proxy.
            client = getattr(feed, "client", None)
            if client is not None:
                from .crossmarket_mt5 import collect
                points, note = build_from(points, collect(client))
                if note:
                    log.info("macro: %s", note)
            return from_drivers(points)
        log.info("macro feed: drivers_free (dxy, spx, vix, real_yield_10y, "
                 "breakeven_10y) on the desk's own refresh cadence")
    else:
        log.info("macro feed DISABLED -- briefs render MACRO CONTEXT: UNMEASURED, "
                 "which the analyst is told to treat as absent, not neutral")

    # CROSS-MARKET FROM THE EXECUTION TERMINAL. The macro leg above goes to
    # Yahoo, and on 2026-08-27 Yahoo returned "possibly delisted" for DX-Y.NYB,
    # ^GSPC and ^VIX at the same moment -- three of the most quoted series in
    # the world do not delist on one afternoon, so that was the API. Every brief
    # that day read MACRO CONTEXT: UNMEASURED while this process held an
    # authenticated connection to a broker quoting silver, the dollar and
    # indices on the SAME CLOCK as its own bars.
    #
    # This does not replace drivers_free: real yields and breakevens need a rate
    # curve no broker quotes. It means the analyst is not left with NOTHING when
    # the web feed fails.
    def crossmarket_fn():
        from .crossmarket_mt5 import collect
        client = getattr(feed, "client", None)
        if client is None:
            return None
        return collect(client, gold_price=getattr(desk, "last_bid", None)).render()

    provider_kw = {"effort": provider_effort} if provider_effort is not None else {}
    primary_name = provider_spec.partition(":")[0]
    fallbacks = (() if primary_name in {"deterministic", "replay", "codex"}
                 else tuple(fallback_provider_specs))
    provider = build_provider_chain(provider_spec, fallbacks,
                                    fallback_kw={"effort": "high"},
                                    **provider_kw)
    desk = LiveDesk(provider,
                    Ledger(cfg.ledger_path),
                    sink, shadow=shadow, vision=vision, broker=broker,
                    cost_model=cost_model,
                    shadow_management=shadow_management,
                    shadow_contextual=shadow_contextual,
                    universe_mode=universe_mode,
                    cohorts=cohorts,
                    crossmarket_provider=crossmarket_fn,
                    calendar=calendar, regime_history=history,
                    macro_provider=macro_fn,
                    wake_on_bar_close=wake_on_bar_close,
                    specialist_council=build_desk_council(specialists))

    # WHO HAS AUTHORITY over the open position. An explicit production decision:
    # the desk ships with Claude forming the entry judgement and a deterministic
    # heuristic running the lifecycle, and that asymmetry should be chosen out
    # loud rather than inherited from a default nobody revisited.
    # WIRING SELF-AUDIT. run_desk.py's preflight checks the WORLD -- MT5, the
    # broker, Telegram -- and every one of those passed all day on 2026-08-27
    # while the desk was broken in five places. None was a world problem: each
    # was a JOIN between two components that both worked and both passed their
    # own tests. A join is invisible to any check that looks at one side of it.
    #
    # Reports, never blocks. A desk that refuses to start because an audit is
    # unhappy is worse than one that starts and says so loudly.
    try:
        from .self_audit import audit, render
        _findings = audit(rows, cohorts)
        for _line in render(_findings).splitlines():
            log.warning(_line) if any(not f.ok for f in _findings) else log.info(_line)
    except Exception as e:                            # noqa: BLE001
        log.warning("self-audit skipped (%s) — wiring is UNVERIFIED, which is "
                    "not the same as verified-good", e)

    desk.set_management(management)
    log.info("management authority: %s (shadow=%s, contextual shadowed=%s)",
             management, shadow_management, shadow_contextual)
    log.info("notification sink: %s", type(sink).__name__)
    return DeskService(desk, feed, cfg)
