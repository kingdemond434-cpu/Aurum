"""A Linux-native XAUUSD feed. Same Protocol, no MT5, no Wine, no Windows.

WHY THIS EXISTS

`Mt5Client` is deliberately tiny — seven methods, of which only three carry
data: bars, a tick, and the symbol's contract facts. Everything above it in the
desk depends on that Protocol and not on MetaTrader, which was the point of
writing it as a Protocol. So a feed that runs on an ordinary Linux VPS is a
contained job rather than a rewrite.

That matters because the MT5 Python package needs the MT5 *terminal*, and the
terminal is Windows. On Linux it imports fine and then fails to initialize, with
nothing to attach to. The usual answers are a Windows VM or Wine, and both are
real work for a desk that only ever needs to READ prices.

THE HONEST TRADE-OFF, STATED PLAINLY

OANDA's XAU_USD is OANDA's pricing, not your MT5 broker's. For an advisory desk
that you execute by hand this is defensible: structure, swings, levels and
displacement are properties of gold, not of a venue, and they agree across
brokers to well inside a tick. Two things do NOT agree and must be handled:

  1. SPREAD. Cost accounting must use YOUR broker's spread, not OANDA's, or
     every net-R number is wrong in whichever direction the venues differ.
     Pass the measured value through CostModel; do not take it from this feed.

  2. STOPS LEVEL. Venue minimum stop distance is a broker fact. Read it once
     from your MT5 terminal and pass it in BrokerLimits.

Use this feed for PERCEPTION. Use your broker's numbers for COST and LEGALITY.

STATUS: written against the documented v20 REST API and NOT exercised against
the live service from here — this container cannot reach OANDA. `--selftest`
runs the whole surface in one command so you find out in ten seconds rather
than at 3am.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

PRACTICE = "https://api-fxpractice.oanda.com"
LIVE = "https://api-fxtrade.oanda.com"

# OANDA granularity strings for the timeframes the desk asks for.
GRAN = {"M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30",
        "H1": "H1", "H4": "H4", "D1": "D"}
# The desk passes MT5 integer timeframes through the Protocol, so map both ways.
MT5_TF = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 16385: "H1", 16388: "H4",
          16408: "D1"}


class _Row:
    """Mimics an MT5 rates row closely enough for feed.py's reader."""
    __slots__ = ("time", "open", "high", "low", "close", "tick_volume", "spread",
                 "real_volume")

    def __init__(self, t, o, h, l, c, v, sp):
        self.time, self.open, self.high, self.low, self.close = t, o, h, l, c
        self.tick_volume = self.real_volume = v
        self.spread = sp

    def __getitem__(self, k):
        return getattr(self, k)

    def keys(self):
        return self.__slots__


@dataclass
class _Tick:
    time: int
    bid: float
    ask: float
    last: float = 0.0
    volume: int = 0
    time_msc: int = 0
    flags: int = 0


@dataclass
class _SymbolInfo:
    name: str
    digits: int = 2
    point: float = 0.01
    spread: int = 0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    trade_contract_size: float = 100.0
    visible: bool = True


@dataclass
class _AccountInfo:
    company: str = "OANDA"
    server: str = "v20"
    login: int = 0
    balance: float = 0.0
    currency: str = "USD"


class OandaClient:
    """Implements the Mt5Client Protocol against OANDA's v20 REST API.

    READ ONLY. It requests pricing and candles and nothing else; there is no
    order endpoint anywhere in this file, which keeps the advisory-only property
    that run_desk.py verifies by scanning for trading calls.
    """

    def __init__(self, token: Optional[str] = None, account: Optional[str] = None,
                 practice: bool = True, instrument: str = "XAU_USD",
                 timeout: float = 10.0):
        self.token = token or os.environ.get("OANDA_TOKEN")
        self.account = account or os.environ.get("OANDA_ACCOUNT")
        self.base = PRACTICE if practice else LIVE
        self.instrument = instrument
        self.timeout = timeout
        self._err: tuple = (0, "ok")
        self._ready = False

    # -- plumbing ---------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept-Datetime-Format": "RFC3339"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _ts(s: str) -> int:
        # RFC3339 with nanoseconds; datetime only takes microseconds.
        s = s.replace("Z", "+00:00")
        if "." in s:
            head, _, tail = s.partition(".")
            frac = tail.split("+")[0][:6].ljust(6, "0")
            off = tail[tail.find("+"):] if "+" in tail else "+00:00"
            s = f"{head}.{frac}{off}"
        return int(datetime.fromisoformat(s).timestamp())

    # -- Mt5Client Protocol ----------------------------------------------
    def initialize(self, path=None, login=None, password=None, server=None) -> bool:
        if not self.token:
            self._err = (1, "OANDA_TOKEN not set")
            return False
        try:
            # cheapest authenticated call that proves the token works
            self._get("/v3/accounts")
            self._ready = True
            self._err = (0, "ok")
            return True
        except urllib.error.HTTPError as e:
            self._err = (e.code, f"HTTP {e.code}: {e.reason}")
        except Exception as e:                       # network, DNS, timeout
            self._err = (2, f"{type(e).__name__}: {e}")
        return False

    def shutdown(self) -> None:
        self._ready = False

    def last_error(self) -> tuple:
        return self._err

    def symbol_info_tick(self, symbol: str):
        if not self.account:
            self._err = (3, "OANDA_ACCOUNT not set — pricing needs an account id")
            return None
        try:
            d = self._get(f"/v3/accounts/{self.account}/pricing",
                          {"instruments": self.instrument})
            p = (d.get("prices") or [None])[0]
            if not p:
                self._err = (4, "no price in response")
                return None
            bid = float(p["bids"][0]["price"])
            ask = float(p["asks"][0]["price"])
            t = self._ts(p["time"])
            return _Tick(time=t, bid=bid, ask=ask, last=(bid + ask) / 2,
                         time_msc=t * 1000)
        except Exception as e:
            self._err = (5, f"{type(e).__name__}: {e}")
            return None

    def copy_rates_from_pos(self, symbol: str, timeframe, start: int, count: int):
        tf = MT5_TF.get(timeframe, timeframe if isinstance(timeframe, str) else "M15")
        gran = GRAN.get(tf, "M15")
        try:
            # BA gives bid and ask candles, so the spread per bar is REAL rather
            # than assumed — the desk's cost accounting is only as good as this.
            d = self._get(f"/v3/instruments/{self.instrument}/candles",
                          {"granularity": gran, "count": min(count, 5000),
                           "price": "BA"})
            rows = []
            for c in d.get("candles", []):
                b, a = c.get("bid"), c.get("ask")
                if not b or not a:
                    continue
                mid_o = (float(b["o"]) + float(a["o"])) / 2
                mid_h = (float(b["h"]) + float(a["h"])) / 2
                mid_l = (float(b["l"]) + float(a["l"])) / 2
                mid_c = (float(b["c"]) + float(a["c"])) / 2
                spread = float(a["c"]) - float(b["c"])
                rows.append(_Row(self._ts(c["time"]), mid_o, mid_h, mid_l, mid_c,
                                 int(c.get("volume", 0)), spread))
            # OANDA returns oldest-first and includes the forming candle last,
            # which is exactly what feed.bars() expects to drop.
            return rows or None
        except Exception as e:
            self._err = (6, f"{type(e).__name__}: {e}")
            return None

    def symbol_info(self, symbol: str):
        """Contract facts. digits/point are gold's; the venue limits are NOT.

        trade_stops_level and trade_freeze_level are returned as ZERO on
        purpose. They are facts about the broker you will actually execute on,
        this feed is not that broker, and inventing them here would silently
        authorise stop placements your venue rejects. Read them once from your
        MT5 terminal and pass BrokerLimits explicitly.
        """
        tick = self.symbol_info_tick(symbol)
        sp = int(round((tick.ask - tick.bid) / 0.01)) if tick else 0
        return _SymbolInfo(name=symbol, digits=2, point=0.01, spread=sp,
                           trade_stops_level=0, trade_freeze_level=0)

    def account_info(self):
        return _AccountInfo(company="OANDA (read-only price feed)",
                            server="v20-practice" if "practice" in self.base else "v20")


def selftest(practice: bool = True) -> int:
    """Exercise the whole surface in one command."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    c = OandaClient(practice=practice)
    print(f"endpoint   : {c.base}")
    print(f"token      : {'set' if c.token else 'NOT SET — export OANDA_TOKEN=...'}")
    print(f"account    : {c.account or 'NOT SET — export OANDA_ACCOUNT=...'}")
    if not c.initialize():
        print(f"initialize : FAILED {c.last_error()}")
        return 1
    print("initialize : ok")
    t = c.symbol_info_tick(c.instrument)
    if t is None:
        print(f"tick       : FAILED {c.last_error()}")
        return 1
    age = datetime.now(timezone.utc).timestamp() - t.time
    print(f"tick       : bid={t.bid} ask={t.ask} spread=${t.ask-t.bid:.2f} "
          f"age={age:.0f}s")
    for tf in ("M15", "H4"):
        rows = c.copy_rates_from_pos(c.instrument, tf, 0, 10)
        if not rows:
            print(f"{tf:<11}: FAILED {c.last_error()}")
            return 1
        last = rows[-1]
        print(f"{tf:<11}: {len(rows)} candles, last close {last.close:.2f} "
              f"at {datetime.fromtimestamp(last.time, timezone.utc):%Y-%m-%d %H:%M} "
              f"spread ${last.spread:.2f}")
    print("\nSELFTEST PASSED — this client can drive the desk.")
    print("Remember: cost and stop-legality must come from YOUR broker, not this feed.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(selftest(practice="--live" not in sys.argv))
