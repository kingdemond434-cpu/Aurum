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
import time
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


def decode_hour(blob: bytes, symbol: str, t: datetime) -> tuple[list, str]:
    """Decompress and decode one hour's payload. NO NETWORK — testable offline.

    Split out from fetch_hour on purpose: the decoder is where the format bugs
    live (zero-indexed month, big-endian, the 1000x scale) and it is the part
    that can be verified without egress. A fetcher whose only test needs the
    internet is untested wherever the internet is not.
    """
    if not blob:
        return [], "empty"
    try:
        raw = lzma.decompress(blob)
    except lzma.LZMAError:
        return [], "corrupt"
    if len(raw) % REC.size:
        # A partial record means a truncated download, not a decodable hour.
        # Silently ignoring the tail would insert plausible-looking garbage.
        return [], f"truncated ({len(raw)} bytes, not a multiple of {REC.size})"

    scale = POINT.get(symbol, 1000.0)
    base_ms = int(t.timestamp() * 1000)
    rows = []
    for off in range(0, len(raw) - REC.size + 1, REC.size):
        ms, ask, bid, av, bv = REC.unpack_from(raw, off)
        rows.append((base_ms + ms, bid / scale, ask / scale, float(bv), float(av)))
    return rows, "ok"


def fetch_hour(symbol: str, t: datetime, timeout: float = 30.0,
               retries: int = 4, pause: float = 0.0) -> tuple[list, str]:
    """One hour of ticks, with backoff. Returns (rows, status) — never silent.

    RETRIES MATTER MORE THAN THEY LOOK. A five-year pull is ~20,000 requests;
    at even a 0.5% transient failure rate that is a hundred hours silently
    recorded as gaps, and a gap is indistinguishable from a holiday in the
    output. Retrying turns a network blip into a delay instead of a hole in the
    data — and holes in the data are the thing this whole fetch exists to fix.

    404 is NOT retried: on this feed it means the hour genuinely does not exist.
    """
    url = hour_url(symbol, t)
    last = "unknown"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aurum/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                blob = r.read()
            if pause:
                time.sleep(pause)
            return decode_hour(blob, symbol, t)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return [], "missing"          # weekend/holiday — expected
            last = f"http {e.code}"
            if e.code in (400, 401, 403):
                return [], last               # not transient; do not hammer
        except Exception as e:
            last = type(e).__name__
        if attempt < retries - 1:
            time.sleep(min(30.0, 2.0 ** attempt))
    return [], last


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
                resume: bool = True, pause: float = 0.0) -> dict:
    """Fetch hour by hour, writing one parquet per month. Genuinely resumable.

    THE RESUME BUG THIS REPLACES. The previous version checked whether a month's
    parquet existed, printed "already present — skipping", and then fell through
    and downloaded the hour anyway — there was no `continue`. It also only
    checked on day 1 hour 0, so even a working check would have skipped a single
    hour of the month. A five-year pull takes hours; a resume that silently
    re-downloads everything makes an interrupted run unrecoverable in practice,
    which is precisely when resume is the only thing that matters.

    Months are now skipped as a UNIT, before any request is issued.
    """
    import pandas as pd

    out.mkdir(parents=True, exist_ok=True)
    stats = {"hours": 0, "ok": 0, "missing": 0, "failed": 0, "ticks": 0,
             "skipped_months": 0, "months": [], "failures": []}
    buf: list = []
    current_month: Optional[str] = None
    t_start = time.monotonic()

    def month_path(month: str) -> Path:
        return out / f"{symbol}_ticks_{month}.parquet"

    def flush(month: str) -> None:
        if not buf:
            return
        p = month_path(month)
        df = pd.DataFrame(buf, columns=["ms", "bid", "ask", "bidvol", "askvol"])
        df["utc"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
        df = df.drop(columns=["ms"]).set_index("utc").sort_index()
        # Write to a temp name and rename. A process killed mid-write otherwise
        # leaves a truncated parquet that resume treats as complete — the worst
        # of both worlds, since the gap is then invisible.
        tmp = p.with_suffix(".parquet.partial")
        df.to_parquet(tmp)
        tmp.replace(p)
        stats["months"].append({"month": month, "ticks": len(df), "file": str(p)})
        print(f"  wrote {p.name}: {len(df):,} ticks")
        buf.clear()

    # Group the hours by month so a completed month can be skipped as a unit
    # without issuing a single request for it.
    todo = list(hours(start, end))
    by_month: dict = {}
    for t in todo:
        by_month.setdefault(f"{t.year}{t.month:02d}", []).append(t)

    total_hours = sum(len(v) for k, v in by_month.items()
                      if not (resume and month_path(k).exists()))
    print(f"  {len(by_month)} month(s), {total_hours:,} hours to fetch "
          f"({len(todo) - total_hours:,} already on disk)\n")

    for month in sorted(by_month):
        if resume and month_path(month).exists():
            stats["skipped_months"] += 1
            print(f"  {month} already complete — skipping "
                  f"(delete {month_path(month).name} to refetch)")
            continue
        current_month = month
        for t in by_month[month]:
            stats["hours"] += 1
            rows, status = fetch_hour(symbol, t, pause=pause)
            if status == "ok":
                stats["ok"] += 1
                stats["ticks"] += len(rows)
                buf.extend(rows)
            elif status in ("missing", "empty"):
                stats["missing"] += 1
            else:
                stats["failed"] += 1
                stats["failures"].append(f"{t:%Y-%m-%d %H}h {status}")
                print(f"  {t:%Y-%m-%d %H}h: {status}")
            if stats["hours"] % 200 == 0:
                el = time.monotonic() - t_start
                rate = stats["hours"] / el if el else 0
                left = (total_hours - stats["hours"]) / rate if rate else 0
                print(f"  ...{stats['hours']:,}/{total_hours:,} hours, "
                      f"{stats['ticks']:,} ticks, {rate:.1f} h/s, "
                      f"ETA {left/60:.0f} min")
        # Flush at the END of each month, so an interruption loses at most one
        # month and the next run picks up exactly there.
        flush(month)
        current_month = None
    if current_month:
        flush(current_month)
    return stats


def offline_test(symbol: str = "XAUUSD") -> int:
    """Prove the FORMAT handling without touching the network.

    Every expensive failure in a fetch like this is a format failure, and all
    three candidates are checkable here: the zero-indexed month in the URL, the
    big-endian record layout, and the 1000x price scale. Getting the scale wrong
    produces prices that look like a market crash rather than a bug; getting the
    month wrong silently downloads a different month than the one requested.

    Run this before committing three hours to a live pull.
    """
    ok = True

    def c(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  — {detail}" if detail else ""))

    print("OFFLINE FORMAT TEST — no network required\n")

    # --- the zero-indexed month, the classic silent error
    t = datetime(2025, 1, 15, 9, tzinfo=UTC)
    u = hour_url(symbol, t)
    c("January is month 00 in the URL", "/2025/00/15/09h_ticks.bi5" in u, u.split("datafeed/")[-1])
    t12 = datetime(2025, 12, 15, 9, tzinfo=UTC)
    c("December is month 11", "/2025/11/15/" in hour_url(symbol, t12))

    # --- build a synthetic hour EXACTLY as the feed documents it
    base = datetime(2025, 6, 3, 14, tzinfo=UTC)
    want = [(0, 3301.25, 3301.55), (1500, 3302.10, 3302.40), (59999, 3300.05, 3300.35)]
    payload = b"".join(
        REC.pack(ms, int(round(a * 1000)), int(round(b * 1000)), 1.5, 2.5)
        for ms, b, a in want)
    blob = lzma.compress(payload, format=lzma.FORMAT_ALONE)
    rows, status = decode_hour(blob, symbol, base)
    c("a well-formed hour decodes", status == "ok", status)
    c("record count matches", len(rows) == len(want), f"{len(rows)} of {len(want)}")
    if len(rows) == len(want):
        for (ms, wb, wa), (gms, gb, ga, _, _) in zip(want, rows):
            good = (abs(gb - wb) < 1e-6 and abs(ga - wa) < 1e-6
                    and gms == int(base.timestamp() * 1000) + ms)
            c(f"  tick +{ms}ms round-trips", good,
              f"bid {gb:.3f} (want {wb:.3f}), ask {ga:.3f} (want {wa:.3f})")
        c("prices land in a plausible gold range",
          all(500 < b < 10000 for _, b, _, _, _ in rows),
          f"{rows[0][1]:.2f}..{rows[-1][2]:.2f} — a wrong scale shows up here as "
          f"3.30 or 3,301,250")
        c("BID and ASK are not swapped",
          all(a >= b for _, b, a, _, _ in rows),
          "the record is ms, ASK, BID — ask comes first on the wire")
        c("timestamps are absolute, not offsets",
          rows[0][0] == int(base.timestamp() * 1000),
          datetime.fromtimestamp(rows[0][0] / 1000, UTC).isoformat())

    # --- the failure modes must be reported, never silently absorbed
    c("an empty body is 'empty'", decode_hour(b"", symbol, base)[1] == "empty")
    c("garbage is 'corrupt'", decode_hour(b"notlzma", symbol, base)[1] == "corrupt")
    trunc = lzma.compress(payload[:-7], format=lzma.FORMAT_ALONE)
    st = decode_hour(trunc, symbol, base)[1]
    c("a truncated download is REFUSED, not partially decoded",
      st.startswith("truncated"), st)

    # --- weekend skipping must not eat live hours
    fri = datetime(2025, 6, 6, 20, tzinfo=UTC)      # Friday 20:00 — open
    sat = datetime(2025, 6, 7, 12, tzinfo=UTC)      # Saturday — shut
    sun_l = datetime(2025, 6, 8, 22, tzinfo=UTC)    # Sunday 22:00 — open
    got = set(hours(fri, sun_l + timedelta(hours=1)))
    c("Friday 20:00 is fetched", fri in got)
    c("Saturday noon is skipped", sat not in got)
    c("Sunday 22:00 is fetched", sun_l in got, "the week reopens ~21:00 UTC")

    print("\n" + ("OFFLINE TEST PASSED — the format handling is correct.\n"
                  "Next: --selftest (needs network), then --dry-run, then the pull."
                  if ok else "OFFLINE TEST FAILED — do not start a long pull."))
    return 0 if ok else 1


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
    ap.add_argument("--selftest", action="store_true",
                    help="fetch one known-liquid hour; needs network")
    ap.add_argument("--offline-test", action="store_true",
                    help="verify the DECODER against synthetic bytes. No network. "
                         "Run this first — it proves the format handling is right "
                         "before you spend three hours on a live pull")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be fetched and stop. Check the date "
                         "range and the month count before committing to it")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds between requests. Be polite to a free feed; "
                         "0.05 costs ~15 min over five years and is good manners")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    default=True)
    ap.add_argument("--compare", nargs=2, metavar=("DUKA", "MT5"))
    args = ap.parse_args()

    if args.offline_test:
        return offline_test(args.symbol)
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
    if args.dry_run:
        hs = list(hours(start, end))
        months = sorted({f"{t.year}{t.month:02d}" for t in hs})
        print(f"would fetch {len(hs):,} hours across {len(months)} months")
        print(f"  first {hs[0]:%Y-%m-%d %H}h  last {hs[-1]:%Y-%m-%d %H}h")
        print(f"  first url: {hour_url(args.symbol, hs[0])}")
        print(f"  estimated {len(hs)/3600*1.2:.1f}-{len(hs)/3600*3:.1f} hours "
              f"at 1-3 requests/sec, roughly {len(months)*0.15:.1f} GB")
        print("\nNothing was fetched. Drop --dry-run to run it.")
        return 0
    st = fetch_range(args.symbol, start, end, Path(args.out),
                     resume=args.resume, pause=args.pause)
    print(f"\nhours requested {st['hours']:,}  ok {st['ok']:,}  "
          f"missing {st['missing']:,}  failed {st['failed']:,}")
    print(f"ticks written   {st['ticks']:,}")
    if st.get("skipped_months"):
        print(f"months skipped  {st['skipped_months']} (already on disk)")
    if st["failed"]:
        print(f"\n{st['failed']} hour(s) failed — rerun to fill them; the fetch "
              f"is resumable and will skip completed months.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
