"""Export broker-matched XAUUSD tick and M1 history from a running MT5 terminal.

RUN THIS ON THE WINDOWS MACHINE WHERE MT5 IS INSTALLED AND LOGGED IN.
It cannot run anywhere else: MetaTrader5's Python package talks to the local
terminal over IPC, so there is no host and no credential that substitutes for
the terminal being open on the same machine.

    pip install MetaTrader5 pandas pyarrow
    python export_mt5.py --years 6 --out .

Produces, next to itself:

    XAUUSD_TICKS.parquet   every tick MT5 will give up, bid/ask/time_msc
    XAUUSD_M1.parquet      M1 OHLCV + real spread
    XAUUSD_M15.parquet     M15, the entry timeframe
    XAUUSD_EXPORT.json     coverage, gaps, and the symbol's contract details

WHY TICKS MATTER MORE THAN MORE YEARS

The desk's management engine — profit-lock, partials, runners, trailing — all
operate inside a single M15 bar. Without a finer series the backtester cannot
tell whether a trade reached +3R before it stopped out or after, so every
management number it produces is an assumption. Two years of ticks is worth
more than twenty years of daily bars for this purpose.

MT5's tick history is usually shorter than its bar history. That is expected.
Export whatever exists; the harness records which portion of the sample is
tick-resolved and which is assumed, and reports them separately rather than
averaging an observation together with a guess.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOL_CANDIDATES = ("XAUUSD", "XAUUSD.", "XAUUSDm", "XAUUSD_i", "GOLD", "GOLD.",
                     "XAUUSD.a", "XAUUSDpro", "XAUUSD-ECN")


def resolve_symbol(mt5, preferred: str | None) -> str:
    """Brokers rename gold. Find the real symbol rather than assuming one."""
    if preferred:
        if mt5.symbol_select(preferred, True):
            return preferred
        raise SystemExit(f"symbol {preferred!r} not selectable in this terminal")
    for name in SYMBOL_CANDIDATES:
        info = mt5.symbol_info(name)
        if info is not None and mt5.symbol_select(name, True):
            return name
    all_syms = [s.name for s in (mt5.symbols_get() or [])
                if "XAU" in s.name.upper() or "GOLD" in s.name.upper()]
    raise SystemExit(f"could not find gold. Candidates in this terminal: {all_syms}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=6.0)
    ap.add_argument("--tick-years", type=float, default=3.0,
                    help="ticks are huge; request fewer years than for bars")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 is not installed. Run:  pip install MetaTrader5 pandas pyarrow")
        return 2
    import pandas as pd

    if not mt5.initialize():
        print(f"mt5.initialize() failed: {mt5.last_error()}\n"
              f"Open the MT5 terminal and log in first — this script attaches to a "
              f"running terminal, it does not start one.")
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta: dict = {"exported_at": datetime.now(timezone.utc).isoformat()}

    try:
        sym = resolve_symbol(mt5, args.symbol)
        info = mt5.symbol_info(sym)
        acct = mt5.account_info()
        print(f"symbol   : {sym}")
        print(f"digits   : {info.digits}   point: {info.point}   "
              f"spread now: {info.spread} points")
        print(f"broker   : {getattr(acct, 'company', '?')}   server: "
              f"{getattr(acct, 'server', '?')}")
        meta["symbol"] = sym
        meta["digits"] = info.digits
        meta["point"] = info.point
        meta["trade_contract_size"] = getattr(info, "trade_contract_size", None)
        meta["broker"] = getattr(acct, "company", None)
        meta["server"] = getattr(acct, "server", None)

        now = datetime.now()
        # ---- bars -------------------------------------------------------
        for tf_name, tf in (("M1", mt5.TIMEFRAME_M1),
                            ("M15", mt5.TIMEFRAME_M15)):
            frm = now - timedelta(days=int(args.years * 365))
            rates = mt5.copy_rates_range(sym, tf, frm, now)
            if rates is None or not len(rates):
                print(f"{tf_name}: NO DATA ({mt5.last_error()})")
                meta[tf_name] = {"bars": 0, "error": str(mt5.last_error())}
                continue
            df = pd.DataFrame(rates)
            df["utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("utc").drop(columns=["time"])
            path = out / f"{sym}_{tf_name}.parquet"
            df.to_parquet(path)
            gaps = df.index.to_series().diff()
            meta[tf_name] = {
                "bars": int(len(df)),
                "start": str(df.index[0]), "end": str(df.index[-1]),
                "median_gap_s": float(gaps.median().total_seconds()),
                "max_gap_h": float(gaps.max().total_seconds() / 3600),
                "gaps_over_1d": int((gaps > pd.Timedelta("1D")).sum()),
                "zero_spread_rows": int((df.get("spread", pd.Series(dtype=int)) == 0).sum()),
                "file": str(path)}
            print(f"{tf_name:<4}: {len(df):>9,} bars  {df.index[0]} .. {df.index[-1]}"
                  f"   max gap {meta[tf_name]['max_gap_h']:.1f}h")

        # ---- ticks ------------------------------------------------------
        frm = now - timedelta(days=int(args.tick_years * 365))
        print(f"ticks: requesting from {frm:%Y-%m-%d} (this can take minutes and "
              f"several GB of RAM)…")
        ticks = mt5.copy_ticks_range(sym, frm, now, mt5.COPY_TICKS_ALL)
        if ticks is None or not len(ticks):
            print(f"ticks: NO DATA ({mt5.last_error()}). "
                  f"Try a shorter --tick-years, or in the terminal open "
                  f"View > Symbols > {sym} > Ticks to force a download first.")
            meta["ticks"] = {"ticks": 0, "error": str(mt5.last_error())}
        else:
            td = pd.DataFrame(ticks)
            td["utc"] = pd.to_datetime(td["time_msc"], unit="ms", utc=True)
            td = td.set_index("utc").drop(columns=["time", "time_msc"])
            path = out / f"{sym}_TICKS.parquet"
            td.to_parquet(path)
            spread = (td["ask"] - td["bid"])
            meta["ticks"] = {
                "ticks": int(len(td)),
                "start": str(td.index[0]), "end": str(td.index[-1]),
                "median_spread": float(spread.median()),
                "p95_spread": float(spread.quantile(0.95)),
                "crossed_quotes": int((spread < 0).sum()),
                "file": str(path)}
            print(f"ticks: {len(td):>9,} ticks  {td.index[0]} .. {td.index[-1]}"
                  f"   median spread {spread.median():.3f}")

        (out / f"{sym}_EXPORT.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {out / f'{sym}_EXPORT.json'}")
        print("Send the .parquet files and the EXPORT.json back to the desk.")
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
