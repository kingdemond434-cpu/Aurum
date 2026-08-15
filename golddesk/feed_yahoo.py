"""Gold prices with no account, no key, no signup. The zero-setup feed.

WHY THIS EXISTS

Every other backend needs you to go and get something first — an OANDA practice
account, an MT5 terminal, a vendor key. That is ten minutes of admin standing
between a working desk and a desk that has never run, and the binding
constraint on this project is forward evidence, which only accumulates once
something is actually running.

This backend needs nothing. It reads a public endpoint and starts.

WHAT IT COSTS YOU, STATED PLAINLY, BECAUSE IT IS NOT NOTHING

  1. NO REAL BID/ASK. This is the big one. The endpoint publishes OHLC, not a
     two-sided quote. The desk's tick path evaluates a long's exits on the BID
     and a short's on the ASK precisely so that half a spread is not silently
     credited to every trade — and here there is no bid or ask to use.

     So the quote is SYNTHESISED as mid +/- half of YOUR declared spread
     (--declared-spread, see venue.py). That is honest arithmetic on a number
     you supplied, not an invented market. But it means the spread is CONSTANT
     by construction: it cannot widen into a release, it cannot gap at the
     rollover, and the moments when spread matters most are exactly the moments
     it will be wrong. Every synthesised tick is stamped so nothing downstream
     mistakes it for an observed one.

  2. AN UNOFFICIAL ENDPOINT. It is not a documented, supported API. It can
     change shape, rate-limit, or stop without notice. The desk degrades to a
     stale-feed halt rather than bad data, but a feed that can vanish is not a
     feed you want under money.

  3. NOT YOUR EXECUTION VENUE. Neither is OANDA — that is what venue.py is for
     — but this is further away: a futures contract or a spot aggregate rather
     than a CFD book.

WHEN TO USE IT

  Now, to start. Forward evidence you are collecting beats forward evidence you
  are about to start collecting, and this removes the last excuse.

WHEN TO STOP USING IT

  The moment you care about the tick path — profit-lock, trailing, giveback,
  intrabar exit resolution. All of that is measured against a synthetic spread
  here. Ten minutes of OANDA signup buys a real two-sided quote, and the switch
  is one flag because both sides implement the same Protocol.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# Gold instruments available without an account.
#   GC=F      COMEX gold futures — deepest, but a FUTURES price: carries basis
#             and rolls, so it is not spot and the roll shows up as a gap.
#   XAUUSD=X  spot gold aggregate — closer to what a CFD tracks, thinner data.
# Default is spot, because the desk's structure logic is about spot levels and a
# futures roll would appear as a structural break that never happened.
SYMBOLS = {"XAUUSD": "XAUUSD=X", "GOLD": "XAUUSD=X", "GC": "GC=F"}

DIGITS = 2
POINT = 10 ** -DIGITS

# Yahoo interval strings for the timeframes the desk asks for.
INTERVAL = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
            "H1": "60m", "H4": "1h", "D1": "1d"}
MT5_TF = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 16385: "H1", 16388: "H4",
          16408: "D1"}
# How much history to request per timeframe. Yahoo caps intraday range.
RANGE = {"1m": "5d", "5m": "1mo", "15m": "1mo", "30m": "1mo",
         "60m": "3mo", "1h": "3mo", "1d": "2y"}


class _Row:
    __slots__ = ("time", "open", "high", "low", "close", "tick_volume", "spread",
                 "real_volume")

    def __init__(self, t, o, h, l, c, v, sp):
        self.time, self.open, self.high, self.low, self.close = t, o, h, l, c
        self.tick_volume = v
        self.spread = sp
        self.real_volume = v

    def __getitem__(self, k):
        return getattr(self, k)


@dataclass
class _Tick:
    bid: float
    ask: float
    time: float
    synthetic: bool = True          # NEVER silently False. See module docstring.


@dataclass
class _SymbolInfo:
    name: str
    digits: int = DIGITS
    point: float = POINT
    spread: int = 0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    volume_min: float = 0.01
    volume_step: float = 0.01


class YahooClient:
    """Mt5Client Protocol over a public quote endpoint. No credentials at all.

    `half_spread` is HALF of the spread you declared for your own venue, in
    price units, and it is what turns a one-sided mid into the two-sided quote
    the desk's exit logic requires. It is supplied, never guessed: a client that
    invented a spread would be making up the single number that decides whether
    marginal trades are worth taking.
    """

    def __init__(self, symbol: str = "XAUUSD", half_spread: float = 0.0,
                 timeout: float = 15.0, min_interval_s: float = 2.0):
        self.symbol = symbol
        self.yahoo = SYMBOLS.get(symbol.upper(), "XAUUSD=X")
        self.half_spread = float(half_spread)
        self.timeout = timeout
        # Politeness AND self-preservation: an unofficial endpoint hit hard will
        # rate-limit, and a rate-limited feed looks exactly like a dead one.
        self.min_interval_s = min_interval_s
        self._last_call = 0.0
        self._err: tuple = (0, "ok")
        self._cache: dict = {}

    # -- Protocol ---------------------------------------------------------
    def initialize(self, path=None, login=None, password=None, server=None) -> bool:
        if self.half_spread <= 0:
            self._err = (1, "no spread supplied — pass --declared-spread. This "
                            "feed publishes no bid/ask, so without your venue's "
                            "spread there is no honest way to build a quote")
            return False
        rows = self.copy_rates_from_pos(self.symbol, 15, 0, 5)
        if not rows:
            return False
        self._err = (0, "ok")
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> tuple:
        return self._err

    def _get(self, interval: str, rng: str) -> Optional[dict]:
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        url = (BASE + urllib.parse.quote(self.yahoo)
               + f"?interval={interval}&range={rng}")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (aurum/1.0)"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            self._err = (e.code, f"http {e.code} — this endpoint is unofficial "
                                 f"and rate-limits; back off or switch feed")
        except Exception as e:
            self._err = (1, f"{type(e).__name__}: {e}")
        return None

    def copy_rates_from_pos(self, symbol: str, timeframe, start: int, count: int):
        tf = MT5_TF.get(timeframe, timeframe if isinstance(timeframe, str) else "M15")
        interval = INTERVAL.get(tf, "15m")
        doc = self._get(interval, RANGE.get(interval, "1mo"))
        if not doc:
            return None
        try:
            res = doc["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError):
            self._err = (1, "unexpected response shape — the endpoint changed")
            return None

        rows = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            # A null bar is a GAP, not a zero. Interpolating it would invent
            # structure — a swing high that never traded — and structure is what
            # this desk reasons over.
            if None in (o, h, l, c):
                continue
            v = (q.get("volume") or [0] * len(ts))[i] or 0
            # Spread in POINTS, matching the Protocol contract that feed.bars()
            # converts with 10**-digits. Same unit trap the OANDA client fell
            # into; here the value is the declared spread rather than a measured
            # one, and it is constant by construction.
            sp = int(round(self.half_spread * 2 / POINT))
            rows.append(_Row(float(t), float(o), float(h), float(l), float(c),
                             int(v), sp))
        if not rows:
            self._err = (1, "no usable bars in the response")
            return None
        # Oldest-first with the forming bar last, which is what feed.bars()
        # expects to drop.
        return rows[-(count + 1):]

    def symbol_info_tick(self, symbol: str):
        """A two-sided quote SYNTHESISED from the last close and your spread.

        There is no real bid/ask behind this. It is `mid +/- half_spread`, and
        the constancy is the honest limitation: the spread cannot widen into a
        release here, which is exactly when a real one does.
        """
        rows = self.copy_rates_from_pos(symbol, 1, 0, 2)      # M1 for freshness
        if not rows:
            rows = self.copy_rates_from_pos(symbol, 15, 0, 2)
        if not rows:
            return None
        last = rows[-1]
        mid = float(last.close)
        # ROUND THE SPREAD OUTWARD, NEVER INWARD.
        #
        # Naive round() on both sides turns a declared $0.45 into $0.44: the bid
        # rounds up and the ask rounds down, each by half a tick, and the desk
        # is handed a cost cheaper than the one you told it you pay. That is the
        # error direction that makes marginal trades look positive, which is the
        # whole reason venue.py exists. Floor the bid, ceil the ask, so the
        # synthesised spread is never narrower than declared.
        import math
        bid = math.floor((mid - self.half_spread) / POINT) * POINT
        ask = math.ceil((mid + self.half_spread) / POINT) * POINT
        return _Tick(bid=round(bid, DIGITS), ask=round(ask, DIGITS),
                     time=float(last.time), synthetic=True)

    def symbol_info(self, symbol: str):
        """Contract facts. The VENUE limits are deliberately zero and not guessed.

        trade_stops_level is 0 for the same reason the OANDA client returns 0:
        it is a fact about the broker you execute at, and this feed knows
        nothing about that broker. Pass --min-stop from your own terminal.
        """
        return _SymbolInfo(name=symbol,
                           spread=int(round(self.half_spread * 2 / POINT)))

    def account_info(self):
        return None                 # no account exists; saying so beats a stub
