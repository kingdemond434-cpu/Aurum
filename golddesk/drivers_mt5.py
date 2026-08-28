"""Macro drivers from the execution terminal, when the web feed has nothing.

WHY THIS EXISTS

Every brief on the live desk read `MACRO CONTEXT: UNMEASURED` for days. The
analyst was reading gold — an instrument whose entire bid is macro — with no
dollar, no risk state and no rate context at all.

The cause was not the desk. `drivers_free` fetches through yfinance, and on
2026-08-27 Yahoo answered "possibly delisted" for DX-Y.NYB, ^GSPC and ^VIX at
the same moment. Three of the most quoted series on earth do not delist on one
afternoon, so that was the API. On 2026-08-28 the box could not even import
yfinance. A single unofficial web endpoint was the only path to the macro block,
and when it broke the analyst simply lost a whole category of input while
everything reported healthy.

Meanwhile this process holds an authenticated MT5 connection quoting the dollar
and equities on the SAME CLOCK as its own bars. It cannot delist, needs no API
key, no library, and no network the desk is not already using.

WHAT IT CAN AND CANNOT SUPPLY, said plainly because the gap is the point

    dxy               PROXY   EURUSD inverted. EUR is ~57% of the DXY basket,
                              so the correlation is high and the SIGN is exact,
                              but the magnitude is not the index.
    spx               EXACT   US500 is the S&P 500. A CFD on the index is the
                              index, not a correlate.
    vix               ABSENT  Most retail FX brokers do not quote it, and every
                              cheap stand-in (range, ATR) is derived from price
                              the desk already sends. That is not a second
                              observation, it is the first one rearranged.
    real_yield_10y    ABSENT  Needs a rate curve. No broker quotes it.
    breakeven_10y     ABSENT  Same.

ABSENT IS AN ANSWER AND IS RETURNED AS ONE. This never invents a driver to fill
a slot. Two of five stay missing and `macro_context` renders them as missing —
which is the honest state and leaves the FRED key as the only way to get them.

IT IS A FALLBACK, NOT A REPLACEMENT. `build_from` fills only the drivers the web
feed failed to observe, so a working Yahoo leg is never overridden by a proxy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .drivers_free import DriverPoint

log = logging.getLogger(__name__)

DRIVERS_MT5_VERSION = "drivmt5-2026-08-28-a"

#: EUR's weight in the ICE dollar index. Not used to SCALE anything — carried so
#: the proxy's note can state how close a stand-in it is rather than implying
#: the two move one-for-one.
EUR_WEIGHT_IN_DXY = 0.576


def from_crossmarket(cm: Any, now: Optional[datetime] = None) -> dict:
    """Turn a CrossMarket reading into {key: DriverPoint}.

    Takes the already-collected object rather than a client, so this is pure and
    testable with no MetaTrader5 anywhere — the same reason crossmarket_mt5
    takes a RateClient Protocol instead of importing the vendor package.

    Only drivers the terminal can actually support are returned. A key absent
    from this dict means "this source cannot answer that", which is different
    from a DriverPoint carrying None, and `build_from` relies on the difference.
    """
    now = now or datetime.now(tz=timezone.utc)
    series = getattr(cm, "series", None) or {}
    out: dict[str, DriverPoint] = {}

    eur = series.get("eurusd")
    if eur is not None and getattr(eur, "observed", False):
        # SIGN IS THE PART THAT MUST BE RIGHT. attribution.py raises a SIGN
        # VIOLATION when a fitted beta contradicts the declared one, so a proxy
        # fed in with the wrong sense would fire that alarm forever while the
        # market did nothing unusual. EURUSD up means the dollar is WEAKER, so
        # the dollar-index change is the negation.
        out["dxy"] = DriverPoint(
            key="dxy", change_pct=-float(eur.change_pct), level=None, as_of=now,
            source=f"mt5:{eur.symbol} inverted", exact=False,
            why=(f"EURUSD inverted. EUR is ~{EUR_WEIGHT_IN_DXY:.0%} of the DXY "
                 f"basket, so the direction is right and the magnitude is not "
                 f"the index. Used because the web feed had no dollar at all."))

    eq = series.get("equities")
    if eq is not None and getattr(eq, "observed", False):
        out["spx"] = DriverPoint(
            key="spx", change_pct=float(eq.change_pct),
            level=getattr(eq, "last", None), as_of=now,
            source=f"mt5:{eq.symbol}", exact=True,
            why="US500 is the S&P 500; a CFD on the index is the index.")

    # vix, real_yield_10y and breakeven_10y are deliberately NOT here. See the
    # module docstring: a stand-in derived from price the desk already sends is
    # not a second observation, and inventing one would corrupt every
    # attribution that treats these as independent drivers.
    return out


def build_from(web_points: Optional[dict], cm: Any,
               now: Optional[datetime] = None) -> tuple[dict, str]:
    """Merge a terminal reading UNDER a web reading. Returns (points, note).

    FILLS GAPS ONLY. A driver the web feed observed is kept exactly as it came,
    because it is the real series and this module's dollar is a proxy. The MT5
    reading is consulted only where the web leg returned nothing — which, when
    yfinance is broken or uninstalled, is everything.

    The note names which drivers were rescued and which are still missing, so
    "the analyst had macro today" never gets confused with "the analyst had the
    macro it should have had".
    """
    points = dict(web_points or {})
    try:
        fallback = from_crossmarket(cm, now)
    except Exception as e:                             # noqa: BLE001
        # NEVER the reason a wake fails. This is a rescue path; a rescue that
        # can take down the thing it rescues is worse than no rescue.
        log.warning("mt5 driver fallback failed: %s", e)
        return points, ""

    rescued = []
    for key, point in fallback.items():
        existing = points.get(key)
        if existing is not None and getattr(existing, "observed", False):
            continue
        points[key] = point
        rescued.append(key)

    if not rescued:
        return points, ""
    still = [k for k in ("dxy", "spx", "vix", "real_yield_10y", "breakeven_10y")
             if not getattr(points.get(k), "observed", False)]
    note = (f"{', '.join(rescued)} came from the EXECUTION TERMINAL because the "
            f"web feed returned nothing for them"
            + (f"; {', '.join(still)} still ABSENT — no broker quotes them"
               if still else ""))
    log.info("macro rescued from mt5: %s", note)
    return points, note
