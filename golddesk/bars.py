"""Tick history to M1/M5/M15 bars without ever storing a tick.

THE CONSTRAINT IS THE DESIGN

XAUUSD tick history from 2003 is well over a hundred gigabytes. The box has four.
Storing ticks and aggregating later is therefore not an option, and neither is
fetching less history — the whole point of going below H1 is MORE observations,
and Sharpe scales with the square root of them.

So nothing is stored. One hour of ticks is fetched, folded into open bar
accumulators, and dropped before the next hour is requested. Peak memory is one
hour of one symbol, which is tens of megabytes at gold's tick rate. The tick data
is never a file, never a dataframe of the whole range, never anything that has to
fit anywhere.

WHAT THAT COSTS AND WHAT IT BUYS

    XAUUSD H1, 2018-2026     49,735 bars      1.6 MB   (what exists today)
    XAUUSD M15, 2003-2026   ~570,000 bars    ~10 MB
    XAUUSD M5,  2003-2026  ~1,700,000 bars   ~30 MB
    XAUUSD M1,  2003-2026  ~8,300,000 bars  ~110 MB

Every armed symbol at M1 back to 2003 is about half a gigabyte. Twenty-two
symbols at M5 is under a gigabyte. Both fit, comfortably, on a box where the raw
ticks would not have fit a hundred times over.

COMPACT DTYPES, BECAUSE 40% IS FREE

Prices are stored as int32 scaled by the instrument's tick size rather than
float64. Gold at 3000.00 with a 0.01 tick is 300,000, which int32 holds to
21,474.83 — comfortable for every instrument here and half the width. Volumes are
uint32, spread is uint16 in points. Roughly 18 bytes a bar against 33.

THE BUDGET IS ENFORCED, NOT SUGGESTED

`DiskBudget` refuses to write past its cap and says what the write would have
cost. A disk that fills silently takes the daily cycle down with it, and a
research job that dies at 90% through a fetch has spent the bandwidth for
nothing. The budget is checked BEFORE each month is written, so a refusal costs
one month rather than the run.

RESUMABLE, BECAUSE A FOUR-HOUR FETCH WILL BE INTERRUPTED

Each month is written as it completes and recorded in a manifest. A rerun skips
what is already on disk. Without this the first network blip costs the entire
history, and on a free tier there will be network blips.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

BARS_VERSION = "bars-2026-08-18-a"

#: Timeframes built in one pass over the ticks. Building M1 and resampling later
#: would be cheaper in code and wrong in fact: resampling M1 to M5 inherits M1's
#: rounding, and the high of five M1 bars is not always the high of the M5 bar
#: when a tick lands between them.
TIMEFRAMES = {"M1": 60, "M5": 300, "M15": 900}

#: Default cap. Deliberately well under the box so an unrelated process that
#: needs room is not competing with a research fetch.
DEFAULT_BUDGET_MB = 1500


@dataclass
class Bar:
    """One bar under construction. Prices stay float until the write."""
    ts: int
    open: float
    high: float
    low: float
    close: float
    ticks: int = 0
    spread_sum: float = 0.0

    def update(self, price: float, spread: float) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close = price
        self.ticks += 1
        self.spread_sum += spread

    @property
    def mean_spread(self) -> float:
        return self.spread_sum / self.ticks if self.ticks else 0.0


class Aggregator:
    """Folds a stream of ticks into bars for several timeframes at once.

    ONE PASS, ALL TIMEFRAMES. The alternative — build M1, resample upward — is
    less code and produces different numbers: resampling inherits M1's bucket
    boundaries, and the true M5 high can sit on a tick that M1 rounding put in a
    neighbouring bar. Since the whole exercise is about measuring an edge at
    higher frequency, an aggregation artefact at exactly that frequency would be
    the worst possible place to save effort.
    """

    def __init__(self, timeframes: dict = TIMEFRAMES):
        self.timeframes = dict(timeframes)
        self._open: dict = {tf: None for tf in self.timeframes}
        self.done: dict = {tf: [] for tf in self.timeframes}

    def add(self, ts_ms: int, bid: float, ask: float) -> None:
        """One tick. `ts_ms` is epoch milliseconds, UTC.

        Mid price, and the spread carried alongside. Using the mid rather than
        bid or ask keeps the bar series comparable to the H1 data already on
        disk; the spread is preserved as its own column so cost work uses a
        MEASURED spread rather than a broker's median.
        """
        if not (math.isfinite(bid) and math.isfinite(ask)):
            return
        if bid <= 0 or ask <= 0 or ask < bid:
            return                      # crossed or absent quote: not a tick
        mid, spread = (bid + ask) / 2.0, ask - bid
        sec = ts_ms // 1000
        for tf, width in self.timeframes.items():
            bucket = (sec // width) * width
            cur = self._open[tf]
            if cur is None:
                self._open[tf] = Bar(bucket, mid, mid, mid, mid, 1, spread)
            elif bucket == cur.ts:
                cur.update(mid, spread)
            elif bucket > cur.ts:
                self.done[tf].append(cur)
                self._open[tf] = Bar(bucket, mid, mid, mid, mid, 1, spread)
            # bucket < cur.ts means an out-of-order tick; dropped rather than
            # reopening a closed bar, because reopening would let a late tick
            # rewrite a bar a downstream consumer may already have read.

    def flush(self) -> None:
        """Close every open bar. Call once at the end of a contiguous range.

        NOT between hours: an hour boundary is not a bar boundary for M15, and
        flushing there would emit three truncated bars an hour.
        """
        for tf, cur in self._open.items():
            if cur is not None:
                self.done[tf].append(cur)
                self._open[tf] = None

    def take(self, tf: str) -> list:
        """Completed bars for one timeframe, and forget them.

        Handing them over and clearing is what keeps memory flat across a
        multi-year fetch — the caller writes and drops, and nothing accumulates
        here.
        """
        out = self.done[tf]
        self.done[tf] = []
        return out


@dataclass
class DiskBudget:
    """A cap that refuses, rather than a guideline that is exceeded.

    A disk that fills silently takes the daily cycle down with it, and a fetch
    that dies at 90% has spent the bandwidth for nothing. Checked before each
    write so a refusal costs one month rather than the run.
    """
    root: Path
    limit_mb: float = DEFAULT_BUDGET_MB
    spent_mb: float = 0.0
    refusals: list = field(default_factory=list)

    def used_mb(self) -> float:
        if not self.root.exists():
            return 0.0
        return sum(p.stat().st_size for p in self.root.rglob("*")
                   if p.is_file()) / 1e6

    def allows(self, estimate_mb: float) -> bool:
        return self.used_mb() + estimate_mb <= self.limit_mb

    def refuse(self, what: str, estimate_mb: float) -> str:
        msg = (f"REFUSED {what}: {estimate_mb:.1f} MB would take the store to "
               f"{self.used_mb() + estimate_mb:.0f} MB against a "
               f"{self.limit_mb:.0f} MB cap. Nothing was written. Drop a "
               f"timeframe or a symbol rather than raising the cap blindly — "
               f"the box has to run the daily cycle too.")
        self.refusals.append(msg)
        return msg


def to_frame(bars: Sequence[Bar], tick_size: float):
    """Bars -> a compact dataframe. int32 scaled prices, not float64.

    Gold at 3000.00 with a 0.01 tick scales to 300,000; int32 reaches
    2,147,483,647, so every instrument here fits with orders of magnitude to
    spare. Half the width of float64 for exactly the same information, because
    a price that is a whole number of ticks does not need a mantissa.
    """
    import numpy as np
    import pandas as pd
    if not bars:
        return None
    inv = 1.0 / tick_size
    return pd.DataFrame({
        "ts": np.array([b.ts for b in bars], dtype="int64"),
        "open": np.array([round(b.open * inv) for b in bars], dtype="int32"),
        "high": np.array([round(b.high * inv) for b in bars], dtype="int32"),
        "low": np.array([round(b.low * inv) for b in bars], dtype="int32"),
        "close": np.array([round(b.close * inv) for b in bars], dtype="int32"),
        "ticks": np.array([min(b.ticks, 4_294_967_295) for b in bars],
                          dtype="uint32"),
        "spread_pts": np.array([min(round(b.mean_spread * inv), 65535)
                                for b in bars], dtype="uint16"),
    })


def read_frame(path: Path, tick_size: float):
    """Compact parquet -> the float OHLC frame the rest of the desk expects.

    The storage format is an implementation detail. Every consumer sees the same
    UTC-indexed float frame it always did, so nothing downstream needs to know
    prices were kept as integers.
    """
    import pandas as pd
    df = pd.read_parquet(path)
    out = pd.DataFrame({
        "open": df["open"].astype("float64") * tick_size,
        "high": df["high"].astype("float64") * tick_size,
        "low": df["low"].astype("float64") * tick_size,
        "close": df["close"].astype("float64") * tick_size,
        "tick_volume": df["ticks"].astype("uint64"),
        "spread": df["spread_pts"].astype("int32"),
    })
    out.index = pd.to_datetime(df["ts"], unit="s", utc=True)
    out.index.name = "time"
    return out


def estimate_mb(n_bars: int) -> float:
    """Bytes on disk for n bars in this layout, after compression.

    18 bytes uncompressed (8+4*4+4+2 minus alignment), and parquet with zstd
    reaches roughly half that on OHLC series because adjacent bars differ in the
    low bits only. Deliberately pessimistic: a budget that under-estimates is
    the one that fills the disk.
    """
    return n_bars * 11.0 / 1e6


def build(symbol: str, tick_source: Callable[[datetime], Iterable],
          start: datetime, end: datetime, out_root: Path, tick_size: float,
          budget: Optional[DiskBudget] = None,
          timeframes: dict = TIMEFRAMES,
          log: Callable[[str], None] = print) -> dict:
    """Stream ticks hour by hour into bar files. Nothing tick-level is kept.

    `tick_source(hour)` yields (ts_ms, bid, ask). It is called once per hour and
    its result is consumed and dropped before the next call, which is what holds
    peak memory to one hour regardless of how many years are requested.

    RESUMABLE. Each month is written as it completes and recorded in a manifest;
    a rerun skips what is on disk. A four-hour fetch on a free tier WILL be
    interrupted, and without this the first blip costs the whole history.
    """
    import pandas as pd
    out_root.mkdir(parents=True, exist_ok=True)
    budget = budget or DiskBudget(out_root)
    manifest_path = out_root / f"{symbol}_manifest.json"
    manifest = (json.loads(manifest_path.read_text("utf-8"))
                if manifest_path.exists() else {})

    agg = Aggregator(timeframes)
    stats = {"hours": 0, "ticks": 0, "written": {}, "skipped": 0,
             "refused": [], "months": []}

    def write_month(month: str) -> None:
        for tf in timeframes:
            bars = agg.take(tf)
            if not bars:
                continue
            key = f"{symbol}_{tf}_{month}"
            path = out_root / f"{key}.parquet"
            if key in manifest and path.exists():
                stats["skipped"] += 1
                continue
            est = estimate_mb(len(bars))
            if not budget.allows(est):
                stats["refused"].append(budget.refuse(key, est))
                continue
            frame = to_frame(bars, tick_size)
            if frame is None:
                continue
            frame.to_parquet(path, compression="zstd", index=False)
            manifest[key] = {"bars": len(bars),
                             "mb": round(path.stat().st_size / 1e6, 3)}
            stats["written"][tf] = stats["written"].get(tf, 0) + len(bars)
            stats["months"].append(key)
        manifest_path.write_text(json.dumps(manifest, indent=1), "utf-8")

    cur_month = None
    t = start
    while t < end:
        month = f"{t.year:04d}-{t.month:02d}"
        if cur_month is None:
            cur_month = month
        elif month != cur_month:
            # FLUSH ONLY AT THE MONTH BOUNDARY, never between hours: an hour
            # edge is not an M15 edge, and flushing there would emit three
            # truncated bars every hour.
            agg.flush()
            write_month(cur_month)
            cur_month = month
        try:
            for ts_ms, bid, ask in tick_source(t):
                agg.add(ts_ms, bid, ask)
                stats["ticks"] += 1
        except Exception as exc:                            # noqa: BLE001
            log(f"  {t:%Y-%m-%d %H}h failed: {exc}")
        stats["hours"] += 1
        t += timedelta(hours=1)

    if cur_month is not None:
        agg.flush()
        write_month(cur_month)

    stats["store_mb"] = round(budget.used_mb(), 1)
    stats["budget_mb"] = budget.limit_mb
    return stats


def plan(symbols: Sequence[str], years: float,
         timeframes: Sequence[str] = ("M15",),
         budget_mb: float = DEFAULT_BUDGET_MB) -> str:
    """What a fetch would cost on disk, BEFORE spending hours of bandwidth.

    The point of running this first is that "will it fit" is answerable in
    milliseconds and the fetch is not.
    """
    lines = [f"BAR STORE PLAN  ({BARS_VERSION})",
             f"  budget {budget_mb:.0f} MB", ""]
    total = 0.0
    per_year = {"M1": 372_000, "M5": 74_400, "M15": 24_800}
    for tf in timeframes:
        n = per_year.get(tf, 24_800) * years * len(symbols)
        mb = estimate_mb(n)
        total += mb
        lines.append(f"  {tf:<5}{len(symbols):>3} symbols x {years:.0f}y = "
                     f"{n:>12,.0f} bars   {mb:>8.1f} MB")
    lines += ["", f"  TOTAL {total:>8.1f} MB  "
              + ("FITS" if total <= budget_mb
                 else f"OVER BUDGET by {total - budget_mb:.0f} MB")]
    if total > budget_mb:
        lines.append("  Drop the finest timeframe or narrow the symbol list. "
                     "Raising the cap\n  is the wrong fix on a box that also "
                     "has to run the daily cycle.")
    return "\n".join(lines)
