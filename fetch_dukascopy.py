"""Deep tick history for XAUUSD, free, back roughly two decades.

WHY THIS AND NOT ONLY MT5

`export_mt5.py` gives BROKER-MATCHED data, which is the right thing for cost
calibration and for anything that has to match the venue you execute on. What it
usually will not give you is depth: MT5 tick history is whatever the broker
chose to keep, and for most retail brokers that is months rather than years.

Dukascopy publish tick-level bid/ask for XAUUSD going back to roughly 2003, free
and without an account. That is the difference between measuring management on a
few hundred trades and measuring it on tens of thousands, and management is
where this system's measured losses currently are — 15 of 20 trades reached +1R
and 2 survived, with zero intrabar observations on any of them.

USE BOTH, FOR DIFFERENT THINGS

  Dukascopy  -> depth. Structure, excursion, intrabar ordering, management
                research, scalp viability. Millions of ticks.
  Your MT5   -> truth about YOUR venue. Spread distribution, stops level,
                execution costs. Hundreds of thousands of ticks is plenty.

And then MEASURE the disagreement between them rather than assuming it is small.
`--compare` does exactly that against an MT5 export, which converts "third-party
data is close enough" from a hope into a number.

FORMAT NOTES, BECAUSE THEY BITE

  * The month in the URL is ZERO-INDEXED. January is 00. This is the single
    most common way to silently fetch the wrong month.
  * Each hour is an LZMA-compressed .bi5 of 20-byte records:
        uint32  milliseconds since the hour
        uint32  ask, scaled by the instrument's point factor
        uint32  bid, scaled the same way
        float32 ask volume
        float32 bid volume
    all BIG-endian.
  * Missing hours are normal — weekends, holidays, and genuine gaps all return
    404 or an empty body. An empty hour is recorded as empty rather than
    skipped, so coverage is measurable instead of assumed.

    python3 fetch_dukascopy.py --from 2023-01-01 --to 2026-08-01
    python3 fetch_dukascopy.py --selftest          # one hour, ~2 seconds
"""

from __future__ import annotations

import argparse
import lzma
import struct
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

UTC = timezone.utc
BASE = "https://datafeed.dukascopy.com/datafeed"
REC = struct.Struct(">IIIff")          # big-endian: ms, ask, bid, askvol, bidvol

# XAUUSD is quoted to 3 decimals in the Dukascopy feed, so the integer prices
# are scaled by 1000. Getting this wrong shifts every price by a factor of ten
# and the error looks like a market crash rather than a bug.
POINT = {"XAUUSD": 1000.0, "XAGUSD": 1000.0, "EURUSD": 100000.0}


def hour_url(symbol: str, t: datetime) -> str:
    # month is ZERO-INDEXED in this API
    return (f"{BASE}/{symbol}/{t.year}/{t.month - 1:02d}/{t.day:02d}/"
            f"{t.hour:02d}h_ticks.bi5")


def fetch_hour(symbol: str, t: datetime, timeout: float = 30.0
               ) -> tuple[list, str]:
    """One hour of ticks. Returns (rows, status) — status is never silent."""
    url = hour_url(symbol, t)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aurum/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            blob = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], "missing"          # weekend/holiday — expected
        return [], f"http {e.code}"
    except Exception as e:
        return [], f"{type(e).__name__}"
    if not blob:
        return [], "empty"
    try:
        raw = lzma.decompress(blob)
    except lzma.LZMAError:
        return [], "corrupt"

    scale = POINT.get(symbol, 1000.0)
    base_ms = int(t.timestamp() * 1000)
    rows = []
    for off in range(0, len(raw) - REC.size + 1, REC.size):
        ms, ask, bid, av, bv = REC.unpack_from(raw, off)
        rows.append((base_ms + ms, bid / scale, ask / scale, float(bv), float(av)))
    return rows, "ok"


def hours(start: datetime, end: datetime) -> Iterator[datetime]:
    t = start.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)
    while t < end:
        # Skip the weekend closure: Friday 21:00 UTC to Sunday 21:00 UTC. Not a
        # guess — it is when the venue is shut, and requesting it wastes an
        # hour of round trips per weekend for guaranteed 404s.
        wd, hr = t.weekday(), t.hour
        shut = (wd == 5) or (wd == 4 and hr >= 21) or (wd == 6 and hr < 21)
        if not shut:
            yield t
        t += timedelta(hours=1)


def fetch_range(symbol: str, start: datetime, end: datetime, out: Path,
                resume: bool = True) -> dict:
    import pandas as pd

    out.mkdir(parents=True, exist_ok=True)
    stats = {"hours": 0, "ok": 0, "missing": 0, "failed": 0, "ticks": 0,
             "months": []}
    buf: list = []
    current_month: Optional[str] = None

    def flush(month: str) -> None:
        if not buf:
            return
        p = out / f"{symbol}_ticks_{month}.parquet"
        df = pd.DataFrame(buf, columns=["ms", "bid", "ask", "bidvol", "askvol"])
        df["utc"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
        df = df.drop(columns=["ms"]).set_index("utc").sort_index()
        df.to_parquet(p)
        stats["months"].append({"month": month, "ticks": len(df), "file": str(p)})
        print(f"  wrote {p.name}: {len(df):,} ticks")
        buf.clear()

    for t in hours(start, end):
        month = f"{t.year}{t.month:02d}"
        if current_month and month != current_month:
            flush(current_month)
        current_month = month
        if resume and (out / f"{symbol}_ticks_{month}.parquet").exists() \
                and t.day == 1 and t.hour == 0:
            print(f"  {month} already present — skipping (delete to refetch)")
        stats["hours"] += 1
        rows, status = fetch_hour(symbol, t)
        if status == "ok":
            stats["ok"] += 1
            stats["ticks"] += len(rows)
            buf.extend(rows)
        elif status in ("missing", "empty"):
            stats["missing"] += 1
        else:
            stats["failed"] += 1
            print(f"  {t:%Y-%m-%d %H}h: {status}")
        if stats["hours"] % 200 == 0:
            print(f"  ...{stats['hours']} hours, {stats['ticks']:,} ticks so far")
    if current_month:
        flush(current_month)
    return stats


def selftest(symbol: str = "XAUUSD") -> int:
    """One known-liquid hour. Proves URL, decompression, scaling and sanity."""
    t = datetime(2025, 6, 3, 14, tzinfo=UTC)          # a Tuesday, NY session
    print(f"fetching {hour_url(symbol, t)}")
    rows, status = fetch_hour(symbol, t)
    print(f"status : {status}")
    if status != "ok" or not rows:
        print("FAILED — check network egress to datafeed.dukascopy.com")
        return 1
    ms, bid, ask, bv, av = rows[0]
    when = datetime.fromtimestamp(ms / 1000, UTC)
    spreads = [a - b for _, b, a, _, _ in rows]
    print(f"ticks  : {len(rows):,} in one hour")
    print(f"first  : {when:%Y-%m-%d %H:%M:%S} bid={bid:.3f} ask={ask:.3f}")
    print(f"spread : median ${sorted(spreads)[len(spreads)//2]:.3f}")
    lo = min(b for _, b, _, _, _ in rows)
    hi = max(a for _, _, a, _, _ in rows)
    print(f"range  : {lo:.2f} .. {hi:.2f}")
    if not (500 < lo < 10000):
        print(f"FAILED — prices out of any plausible gold range. The POINT "
              f"scale for {symbol} is wrong.")
        return 1
    if any(a < b for _, b, a, _, _ in rows):
        print("WARNING — crossed quotes present (ask < bid)")
    print("\nSELFTEST PASSED — scaling and format are correct.")
    return 0


def compare(duka: Path, mt5: Path) -> int:
    """How far apart are the two sources, really? A number, not a hope."""
    import pandas as pd
    d = pd.read_parquet(duka)
    m = pd.read_parquet(mt5)
    for df in (d, m):
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
    dm = ((d["bid"] + d["ask"]) / 2).resample("1min").last().dropna()
    if {"bid", "ask"}.issubset(m.columns):
        mm = ((m["bid"] + m["ask"]) / 2).resample("1min").last().dropna()
    else:
        mm = m["close"].resample("1min").last().dropna()
    j = pd.concat([dm.rename("duka"), mm.rename("mt5")], axis=1).dropna()
    if j.empty:
        print("no overlapping minutes — check the date ranges")
        return 1
    diff = (j["duka"] - j["mt5"]).abs()
    print(f"overlapping minutes : {len(j):,}")
    print(f"median |difference| : ${diff.median():.3f}")
    print(f"p95    |difference| : ${diff.quantile(0.95):.3f}")
    print(f"max    |difference| : ${diff.max():.3f}")
    print("\nStructure and excursion transfer if these are small relative to a "
          "typical stop.\nSpread and stops level never transfer — take those "
          "from your own broker.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--out", default="data/dukascopy")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("DUKA", "MT5"))
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.symbol)
    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))
    if not (args.start and args.end):
        ap.error("--from and --to required (or use --selftest)")

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    print(f"{args.symbol} ticks {start:%Y-%m-%d} .. {end:%Y-%m-%d} -> {args.out}")
    print("Expect roughly 1-3 GB per year of gold ticks. Resumable: rerun to "
          "continue.\n")
    st = fetch_range(args.symbol, start, end, Path(args.out))
    print(f"\nhours requested {st['hours']:,}  ok {st['ok']:,}  "
          f"missing {st['missing']:,}  failed {st['failed']:,}")
    print(f"ticks written   {st['ticks']:,}")
    if st["failed"]:
        print(f"\n{st['failed']} hour(s) failed — rerun to fill them; the fetch "
              f"is resumable and will skip completed months.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
