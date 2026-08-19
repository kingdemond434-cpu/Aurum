"""Build M1/M5/M15 bars from tick history. Runs on the VPS, not in the sandbox.

WHY THIS EXISTS AS A SEPARATE SCRIPT

The research container has no route to any market data host — dukascopy, Yahoo,
Stooq, HistData and Binance all return 403 at the proxy, because the environment
only allows package registries and GitHub. Nothing about the data problem can be
solved from there, so the fetch lives here and runs where the terminal already
runs.

TWO SOURCES, FOR DIFFERENT THINGS, AND THE DIFFERENCE MATTERS

    MT5      YOUR broker's ticks. Months to a couple of years deep, but it is
             the venue you actually fill on, so its spread is the one that
             belongs in a cost model. Requires the terminal running and logged
             in; needs no account credentials in this script.

    Dukascopy  Tick bid/ask back to about 2003, free, no account. Twenty years
             of depth against MT5's months. Not your venue, so its spread is
             indicative rather than authoritative.

Use MT5 to calibrate costs and Dukascopy for depth, and MEASURE the disagreement
rather than assuming it is small. The 33x gold spread error is exactly what an
unmeasured cost assumption looks like, and it survived years of backtests.

MEMORY IS THE WHOLE DESIGN, AND IT IS golddesk.bars' JOB

An hour of ticks is fetched, folded into bar accumulators, and dropped before
the next hour is requested. Nothing tick-level is ever written or held. Peak
memory is one hour of one symbol; the output is measured in tens of megabytes
where the input would have been a hundred gigabytes.

    python3 fetch_bars.py --source mt5 --symbols XAUUSD --years 2
    python3 fetch_bars.py --source dukascopy --symbols XAUUSD --years 20 --plan
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from golddesk.bars import (DEFAULT_BUDGET_MB, TIMEFRAMES, DiskBudget,  # noqa: E402
                           build, plan)

OUT = Path("data/bars")

#: Point value per symbol, for the int32 scaling. Read from the terminal when
#: MT5 is the source; these are the fallbacks for the offline path.
TICK_SIZE = {"XAUUSD": 0.01, "XAGUSD": 0.001, "BTCUSD": 0.01, "ETHUSD": 0.01}
DEFAULT_TICK_SIZE = 0.00001
JPY_TICK_SIZE = 0.001


def tick_size_for(symbol: str) -> float:
    if symbol in TICK_SIZE:
        return TICK_SIZE[symbol]
    return JPY_TICK_SIZE if symbol.endswith("JPY") else DEFAULT_TICK_SIZE


# --------------------------------------------------------------------- MT5

def mt5_source(symbol: str):
    """Hour-at-a-time ticks from the running terminal.

    BROKER-MATCHED, which is the point. This is the spread you actually pay, so
    it is the one a cost model should be calibrated against — not a median from
    a third party and certainly not a hardcoded literal.
    """
    import MetaTrader5 as mt5                     # noqa: PLC0415
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}. "
                           f"The terminal must be running and logged in.")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol {symbol} not available in Market Watch")

    def fetch(hour: datetime):
        end = hour + timedelta(hours=1)
        ticks = mt5.copy_ticks_range(symbol, hour, end, mt5.COPY_TICKS_INFO)
        if ticks is None:
            return
        for t in ticks:
            bid, ask = float(t["bid"]), float(t["ask"])
            if bid > 0 and ask > 0:
                yield int(t["time_msc"]), bid, ask

    return fetch


# --------------------------------------------------------------- Dukascopy

def dukascopy_source(symbol: str, point: float):
    """Hour-at-a-time ticks from Dukascopy's public feed.

    THE MONTH IN THE URL IS ZERO-INDEXED. January is 00. This is the single most
    common way to silently fetch the wrong month, and a wrong month does not
    error — it returns perfectly good ticks from somewhere else.
    """
    import lzma                                    # noqa: PLC0415
    import struct                                  # noqa: PLC0415
    import urllib.request                          # noqa: PLC0415

    def fetch(hour: datetime):
        url = (f"https://datafeed.dukascopy.com/datafeed/{symbol}/"
               f"{hour.year:04d}/{hour.month - 1:02d}/{hour.day:02d}/"
               f"{hour.hour:02d}h_ticks.bi5")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                blob = r.read()
        except Exception:                          # noqa: BLE001
            return                                 # missing hour: weekend, holiday
        if not blob:
            return
        try:
            raw = lzma.LZMADecompressor().decompress(blob)
        except lzma.LZMAError:
            return
        base = int(hour.replace(tzinfo=timezone.utc).timestamp()) * 1000
        for i in range(0, len(raw) - 19, 20):
            ms, a, b, _av, _bv = struct.unpack(">IIIff", raw[i:i + 20])
            yield base + ms, b * point, a * point

    return fetch


# ------------------------------------------------------------------- driver

def main() -> int:
    ap = argparse.ArgumentParser(description="tick history -> M1/M5/M15 bars")
    ap.add_argument("--source", choices=("mt5", "dukascopy"), default="mt5")
    ap.add_argument("--symbols", default="XAUUSD",
                    help="comma-separated, e.g. XAUUSD,USDJPY")
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--timeframes", default="M1,M5,M15")
    ap.add_argument("--budget-mb", type=float, default=DEFAULT_BUDGET_MB)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--plan", action="store_true",
                    help="print the disk cost and exit, spending no bandwidth")
    a = ap.parse_args()

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    tfs = {k: TIMEFRAMES[k] for k in a.timeframes.split(",") if k in TIMEFRAMES}
    if not tfs:
        print(f"no valid timeframes in {a.timeframes!r}; "
              f"choose from {list(TIMEFRAMES)}")
        return 2

    # ALWAYS PLAN FIRST. "Will it fit" is answerable in milliseconds and the
    # fetch is not; discovering the answer four hours in is the expensive way.
    print(plan(symbols, a.years, list(tfs), a.budget_mb))
    if a.plan:
        return 0
    print()

    out = Path(a.out)
    budget = DiskBudget(out, limit_mb=a.budget_mb)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=a.years * 365.25)

    total = {}
    for sym in symbols:
        ts = tick_size_for(sym)
        if a.source == "mt5":
            try:
                src = mt5_source(sym)
            except Exception as exc:               # noqa: BLE001
                print(f"{sym}: {exc}")
                continue
        else:
            src = dukascopy_source(sym, ts)
        print(f"{sym}: {start:%Y-%m-%d} -> {end:%Y-%m-%d} via {a.source}")
        stats = build(sym, src, start, end, out, ts, budget, tfs)
        print(f"  {stats['hours']:,} hours, {stats['ticks']:,} ticks, "
              f"wrote {stats['written']}, skipped {stats['skipped']} "
              f"already-present month-files")
        if stats["refused"]:
            for r in stats["refused"][:3]:
                print(f"  {r}")
        total[sym] = stats

    print(f"\nstore now {budget.used_mb():.1f} MB of {budget.limit_mb:.0f} MB")
    print("\nNothing tick-level was written. The bars are in "
          f"{out}/ as SYMBOL_TF_YYYY-MM.parquet,\nint32-scaled — read them with "
          "golddesk.bars.read_frame(path, tick_size).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
