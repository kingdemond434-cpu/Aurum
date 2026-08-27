"""Gold's macro context, read from the terminal that is already connected.

WHY THIS EXISTS

drivers_free.py fetches DXY, S&P and VIX from Yahoo. On 2026-08-27 Yahoo
returned "possibly delisted; no price data found" for DX-Y.NYB, ^GSPC and ^VIX
SIMULTANEOUSLY -- three of the most heavily quoted series in the world do not
delist on the same afternoon, so that was the API, not the market. Every brief
that day carried MACRO CONTEXT: UNMEASURED, and gold's entire bid is macro.

The desk was reading gold blind to the dollar while holding an authenticated
connection to a broker quoting the dollar, silver, indices and oil on the same
clock as its own bars.

WHAT THIS READS AND WHY EACH ONE

  XAGUSD    the gold/silver ratio. Silver is the higher-beta precious metal, so
            the ratio widening while gold rises is gold being bought for FEAR
            and narrowing is it being bought for REFLATION. Two different tapes
            that look identical on a gold chart alone.
  EURUSD    a dollar proxy, and the cleanest one a retail broker always quotes.
            NOT the DXY -- it is one pair, not a basket -- and it is labelled a
            PROXY everywhere it appears so nothing can quietly treat it as the
            index.
  US500     risk appetite. Gold catching a bid while equities fall is a hedge;
            both rising together is liquidity.
  USOIL     the inflation leg, and the one most often absent from a retail
            symbol list, so it is expected to be missing and says so.

WHAT IT IS NOT

It is not the DXY, not real yields, not breakevens. Those need a rate curve no
broker quotes and drivers_free remains the only route to them. This does not
replace that feed; it means the analyst is not left with NOTHING when it fails.

EVERY VALUE IS EVIDENCE, WITH NO VOTE. Same standing as every Context field:
the model reasons over it, it never overrides structure, and a symbol the broker
does not carry renders as ABSENT rather than being silently dropped -- the
analyst must be able to tell "the dollar did nothing" from "nobody looked".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

log = logging.getLogger(__name__)

CROSSMARKET_MT5_VERSION = "xmt5-2026-08-28-a"

#: symbol -> (label, why it is here). Aliases are tried in order because retail
#: brokers name the same instrument four different ways and a missing symbol
#: must be a MISS, not a crash.
SERIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "silver":   ("XAGUSD", ("XAGUSD", "XAGUSD.r", "SILVER", "XAGUSDm")),
    "eurusd":   ("EURUSD", ("EURUSD", "EURUSD.r", "EURUSDm")),
    "equities": ("US500",  ("US500", "SPX500", "SP500", "USTEC", "US500.cash")),
    "oil":      ("USOIL",  ("USOIL", "WTI", "XTIUSD", "CRUDOIL", "USOIL.cash")),
}

#: Bars of lookback for the change window. 24 on H1 is one full session cycle --
#: long enough that a single spike does not define the reading, short enough
#: that it still describes today.
LOOKBACK = 24


class RateClient(Protocol):
    def copy_rates_from_pos(self, symbol: str, timeframe: int,
                            start: int, count: int): ...
    def symbol_info(self, symbol: str): ...


@dataclass(frozen=True)
class Series:
    key: str
    symbol: Optional[str]        # None when the broker carries none of the aliases
    change_pct: Optional[float]
    last: Optional[float]

    @property
    def observed(self) -> bool:
        return self.change_pct is not None


@dataclass(frozen=True)
class CrossMarket:
    series: dict
    gold_silver_ratio: Optional[float] = None
    note: str = ""

    def render(self) -> str:
        """The block the analyst reads. ABSENT is printed, never omitted.

        Omitting a missing series would leave the model unable to tell "the
        dollar did nothing" from "nobody looked" -- the same defect as a brief
        that drops its macro section instead of saying UNMEASURED.
        """
        lines = ["CROSS-MARKET (from the execution terminal, not a web feed)"]
        for key, s in self.series.items():
            if not s.observed:
                lines.append(f"  {key.upper():<10} ABSENT — this broker quotes no "
                             f"symbol for it")
                continue
            lines.append(f"  {key.upper():<10} {s.change_pct:+.2f}%  "
                         f"last {s.last:.4f}  ({s.symbol})")
        if self.gold_silver_ratio is not None:
            lines.append(f"  GOLD/SILVER {self.gold_silver_ratio:.1f} — widening "
                         f"with gold up is fear, narrowing is reflation")
        lines.append("  EURUSD is a DOLLAR PROXY, not the DXY: one pair, not a "
                     "basket. Real yields and breakevens are not here at all.")
        lines.append("  EVIDENCE ONLY — no vote on direction, never overrides "
                     "structure.")
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)


def _resolve(client: RateClient, aliases: Sequence[str]) -> Optional[str]:
    for name in aliases:
        try:
            if client.symbol_info(name) is not None:
                return name
        except Exception:                              # noqa: BLE001
            continue
    return None


def _read(client: RateClient, key: str, aliases: Sequence[str],
          timeframe: int, lookback: int) -> Series:
    sym = _resolve(client, aliases)
    if sym is None:
        return Series(key, None, None, None)
    try:
        rates = client.copy_rates_from_pos(sym, timeframe, 0, lookback + 1)
    except Exception as e:                             # noqa: BLE001
        log.debug("cross-market %s failed: %s", sym, e)
        return Series(key, sym, None, None)
    if rates is None or len(rates) < lookback + 1:
        return Series(key, sym, None, None)
    try:
        first = float(rates[0]["close"])
        last = float(rates[-1]["close"])
    except Exception:                                  # noqa: BLE001
        return Series(key, sym, None, None)
    if first == 0:
        return Series(key, sym, None, last)
    return Series(key, sym, (last - first) / abs(first) * 100.0, last)


def collect(client: RateClient, *, timeframe: int = 16385,
            lookback: int = LOOKBACK,
            gold_price: Optional[float] = None) -> CrossMarket:
    """Read every series this broker carries. NEVER raises.

    `timeframe` defaults to MT5's H1 constant. It is passed as an int rather
    than imported so this module stays testable with no MetaTrader5 installed --
    the desk's own test box has none.
    """
    out: dict[str, Series] = {}
    for key, (_label, aliases) in SERIES.items():
        try:
            out[key] = _read(client, key, aliases, timeframe, lookback)
        except Exception as e:                         # noqa: BLE001
            log.debug("cross-market %s skipped: %s", key, e)
            out[key] = Series(key, None, None, None)

    ratio = None
    silver = out.get("silver")
    if gold_price and silver and silver.last:
        ratio = gold_price / silver.last

    missing = [k for k, s in out.items() if not s.observed]
    note = ("" if not missing else
            f"{len(missing)} series ABSENT ({', '.join(missing)}) — this broker "
            f"does not quote them. Absent is not neutral.")
    return CrossMarket(out, ratio, note)
