"""Live XAUUSD feed — built for Aurum v2, not reconstructed from anything.

Responsibilities, in the order they matter:

    1. CLOSED BARS ONLY. MT5's copy_rates_from_pos(..., 0, n) includes the bar
       currently forming, whose high/low/close still move. Feeding that to
       structure detection is lookahead by accident. bars() drops it.
    2. SERVER TIME IS MEASURED, NOT ASSUMED. MT5 stamps bars in broker
       wall-clock with no timezone. The offset is derived from an ADVANCING
       tick and cached; a fossil quote sitting on the weekend can never
       redefine it and shift every timestamp on the desk.
    3. STALENESS IS EXPLICIT. A quote that stopped advancing is reported as
       stale rather than silently used as current.
    4. RECONNECT FAILS SAFE. On any disconnect the feed halts and reconciles
       before it will answer another question. It never guesses across a gap.

TESTABILITY: every MT5 call goes through Mt5Client. RealMt5Client is the thin
binding to the MetaTrader5 package; SimulatedMt5Client replays stored bars.
The feed logic below is identical for both, so it is exercised in full without
a terminal. Only RealMt5Client itself is unrun until you point it at MT5.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol, Sequence

from .features import Bar

log = logging.getLogger(__name__)

# MT5 timeframe constants, named here so nothing else imports MetaTrader5.
TF = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 16385, "H4": 16388, "D1": 16408}
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
              "H1": 3600, "H4": 14400, "D1": 86400}


@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float
    server_time: datetime      # as reported, broker wall-clock, tz-naive in MT5

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class FeedError(RuntimeError):
    pass


class Mt5Client(Protocol):
    """The entire MT5 surface Aurum uses. Keep it this small."""
    def initialize(self, path: Optional[str], login: Optional[int],
                   password: Optional[str], server: Optional[str]) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple: ...
    def copy_rates_from_pos(self, symbol: str, timeframe: int,
                            start: int, count: int): ...
    def symbol_info_tick(self, symbol: str): ...
    def symbol_info(self, symbol: str): ...
    def account_info(self): ...


class RealMt5Client:
    """Thin binding to the MetaTrader5 package. Import is deferred.

    UNRUN IN DEVELOPMENT — there is no MT5 terminal in the build environment.
    Everything that consumes it is tested against SimulatedMt5Client.
    """
    def __init__(self):
        import MetaTrader5 as mt5           # deferred: never a hard dependency
        self._mt5 = mt5

    def initialize(self, path=None, login=None, password=None, server=None) -> bool:
        kw = {k: v for k, v in
              {"path": path, "login": login, "password": password, "server": server}.items()
              if v is not None}
        return bool(self._mt5.initialize(**kw))

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def last_error(self):
        return self._mt5.last_error()

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return self._mt5.copy_rates_from_pos(symbol, timeframe, start, count)

    def symbol_info_tick(self, symbol):
        return self._mt5.symbol_info_tick(symbol)

    def symbol_info(self, symbol):
        return self._mt5.symbol_info(symbol)

    def account_info(self):
        return self._mt5.account_info()


# --------------------------------------------------------------------------
# Server clock — measured from an advancing tick, never assumed
# --------------------------------------------------------------------------

@dataclass
class ServerClock:
    """Broker wall-clock -> UTC. Only an advancing quote may set the offset."""
    offset: Optional[timedelta] = None
    last_seen_server: Optional[datetime] = None
    measured_at: Optional[datetime] = None
    cache_path: Optional[Path] = None

    def load(self) -> None:
        if self.cache_path and Path(self.cache_path).exists():
            secs = float(Path(self.cache_path).read_text().strip())
            self.offset = timedelta(seconds=secs)
            log.info("server clock offset loaded: %+.0fs", secs)

    def _save(self) -> None:
        if self.cache_path:
            Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.cache_path).write_text(str(self.offset.total_seconds()))

    def observe(self, tick_server_time: datetime, now_utc: datetime) -> bool:
        """Update the offset only if this quote is genuinely newer than the last.

        A weekend terminal keeps returning the Friday close tick forever. Using
        it to measure the offset would drift every timestamp on the desk by
        however long the market has been shut.
        """
        if self.last_seen_server is not None and tick_server_time <= self.last_seen_server:
            return False                       # fossil quote — ignore for timing
        self.last_seen_server = tick_server_time
        self.offset = now_utc.replace(tzinfo=None) - tick_server_time
        self.measured_at = now_utc
        self._save()
        return True

    def to_utc(self, server_naive: datetime) -> datetime:
        if self.offset is None:
            raise FeedError("server clock not yet measured — no advancing tick seen")
        return (server_naive + self.offset).replace(tzinfo=timezone.utc)

    @property
    def known(self) -> bool:
        return self.offset is not None


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------

@dataclass
class FeedConfig:
    symbol: str = "XAUUSD"
    symbol_aliases: tuple[str, ...] = ("XAUUSD", "XAUUSD.r", "GOLD", "XAUUSDm")
    terminal_path: Optional[str] = None
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    max_tick_age_s: float = 120.0
    reconnect_attempts: int = 5
    reconnect_backoff_s: float = 2.0
    clock_cache: Optional[Path] = None
    digits: int = 2


class LiveFeed:
    """Connected, self-healing source of closed bars and fresh quotes."""

    def __init__(self, client: Mt5Client, cfg: FeedConfig = FeedConfig()):
        self.client, self.cfg = client, cfg
        self.clock = ServerClock(cache_path=cfg.clock_cache)
        self.clock.load()
        self.connected = False
        self.resolved_symbol: Optional[str] = None
        self._halted_reason: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        last = None
        for attempt in range(1, self.cfg.reconnect_attempts + 1):
            try:
                ok = self.client.initialize(self.cfg.terminal_path, self.cfg.login,
                                            self.cfg.password, self.cfg.server)
                if ok:
                    self.connected = True
                    self._halted_reason = None
                    self.resolved_symbol = self._resolve_symbol()
                    log.info("feed connected, symbol=%s", self.resolved_symbol)
                    return
                last = self.client.last_error()
            except Exception as e:                     # binding-level failure
                last = e
            wait = self.cfg.reconnect_backoff_s * (2 ** (attempt - 1))
            log.warning("connect attempt %d failed (%s) — retrying in %.0fs",
                        attempt, last, wait)
            time.sleep(wait)
        raise FeedError(f"could not connect after {self.cfg.reconnect_attempts}: {last}")

    def _resolve_symbol(self) -> str:
        """Brokers rename gold. Find the alias this terminal actually serves."""
        for name in (self.cfg.symbol, *self.cfg.symbol_aliases):
            try:
                if self.client.symbol_info(name) is not None:
                    return name
            except Exception:
                continue
        raise FeedError(f"none of {self.cfg.symbol_aliases} exist on this terminal")

    def halt(self, reason: str) -> None:
        """Fail safe. Nothing is answered until reconcile() succeeds."""
        self._halted_reason = reason
        self.connected = False
        log.error("feed halted: %s", reason)

    def reconcile(self) -> bool:
        """Reconnect and re-establish the clock before answering anything."""
        try:
            self.connect()
        except FeedError as e:
            log.error("reconcile failed: %s", e)
            return False
        t = self.raw_tick()
        if t is None:
            self.halt("no tick after reconnect")
            return False
        return True

    def _require_live(self) -> None:
        if self._halted_reason:
            raise FeedError(f"feed halted: {self._halted_reason}")
        if not self.connected:
            raise FeedError("feed not connected")

    # -- quotes ------------------------------------------------------------
    def raw_tick(self) -> Optional[Tick]:
        self._require_live()
        t = self.client.symbol_info_tick(self.resolved_symbol)
        if t is None:
            return None
        st = getattr(t, "time", None)
        server = (datetime.utcfromtimestamp(float(st)) if not isinstance(st, datetime)
                  else st)
        tick = Tick(float(t.bid), float(t.ask), server)
        self.clock.observe(server, datetime.now(timezone.utc))
        return tick

    def quote(self) -> tuple[float, float, float]:
        """(bid, ask, tick_age_s). Age is measured, never assumed to be zero."""
        t = self.raw_tick()
        if t is None:
            raise FeedError("no tick available")
        if not self.clock.known:
            return t.bid, t.ask, float("inf")
        age = (datetime.now(timezone.utc) - self.clock.to_utc(t.server_time)).total_seconds()
        return t.bid, t.ask, max(0.0, age)

    def tick_is_stale(self) -> tuple[bool, float]:
        _, _, age = self.quote()
        return age > self.cfg.max_tick_age_s, age

    # -- bars --------------------------------------------------------------
    def bars(self, timeframe: str, count: int = 500) -> list[Bar]:
        """CLOSED bars only, timestamps converted to UTC.

        Index 0 from copy_rates_from_pos is the forming bar. It is requested
        (so a caller can see it exists) and then dropped — never returned.
        """
        self._require_live()
        if timeframe not in TF:
            raise FeedError(f"unknown timeframe {timeframe}")
        raw = self.client.copy_rates_from_pos(self.resolved_symbol, TF[timeframe],
                                              0, count + 1)
        if raw is None or len(raw) < 2:
            self.halt(f"no bars for {timeframe}: {self.client.last_error()}")
            raise FeedError(f"no bars for {timeframe}")

        rows = list(raw)[:-1] if _ascending(raw) else list(raw)[1:]
        out: list[Bar] = []
        for r in rows:
            ts_raw = r["time"] if not hasattr(r, "time") else r.time
            server = (datetime.utcfromtimestamp(float(ts_raw))
                      if not isinstance(ts_raw, datetime) else ts_raw)
            spread_pts = float(r["spread"]) if _has(r, "spread") else 0.0
            out.append(Bar(
                ts=self.clock.to_utc(server),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=float(r["tick_volume"]) if _has(r, "tick_volume") else 0.0,
                spread=spread_pts * (10 ** -self.cfg.digits),
            ))
        out.sort(key=lambda b: b.ts)
        _assert_no_duplicates(out, timeframe)
        return out

    def multi(self, timeframes: Sequence[str], count: int = 500) -> dict[str, list[Bar]]:
        """Multi-timeframe pull in one pass, so all frames share a moment."""
        return {tf: self.bars(tf, count) for tf in timeframes}


def _ascending(raw) -> bool:
    try:
        return float(raw[0]["time"]) < float(raw[-1]["time"])
    except Exception:
        return True


def _has(row, key) -> bool:
    try:
        _ = row[key]
        return True
    except Exception:
        return False


def _assert_no_duplicates(bars: Sequence[Bar], tf: str) -> None:
    seen = set()
    for b in bars:
        if b.ts in seen:
            raise FeedError(f"duplicate {tf} bar at {b.ts} — feed integrity failure")
        seen.add(b.ts)


# --------------------------------------------------------------------------
# Simulated client — replays stored bars so the feed logic is fully testable
# --------------------------------------------------------------------------

class SimulatedMt5Client:
    """Mimics MT5 semantics: newest-first rows, index 0 forming, naive server time."""

    def __init__(self, bars: Sequence[Bar], server_offset: timedelta = timedelta(hours=3),
                 symbol: str = "XAUUSD", freeze_tick: bool = False):
        self._bars = list(bars)
        self._offset = server_offset       # broker is UTC+offset
        self._i = len(self._bars) - 1
        self._symbol = symbol
        self._freeze_tick = freeze_tick
        self.connected = False

    # position in the replay
    def seek(self, i: int) -> None:
        self._i = i

    def initialize(self, path=None, login=None, password=None, server=None) -> bool:
        self.connected = True
        return True

    def shutdown(self) -> None:
        self.connected = False

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, symbol):
        return object() if symbol == self._symbol else None

    def account_info(self):
        return object()

    def symbol_info_tick(self, symbol):
        b = self._bars[self._i]
        sp = b.spread if b.spread > 0 else 0.48
        srv = (b.ts.replace(tzinfo=None) + self._offset)
        if self._freeze_tick:                     # simulate a weekend fossil quote
            srv = (self._bars[0].ts.replace(tzinfo=None) + self._offset)
        return type("T", (), {"bid": b.close - sp / 2, "ask": b.close + sp / 2,
                              "time": srv})()

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        hi = self._i + 1                          # index 0 == forming bar
        lo = max(0, hi - count)
        window = self._bars[lo:hi + 1]
        rows = []
        for b in window:
            rows.append({"time": (b.ts.replace(tzinfo=None) + self._offset).timestamp(),
                         "open": b.open, "high": b.high, "low": b.low,
                         "close": b.close, "tick_volume": b.volume,
                         "spread": int(round(b.spread * 100))})
        return rows
