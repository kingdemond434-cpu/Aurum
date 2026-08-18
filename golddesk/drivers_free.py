"""Free driver feeds, and an honest account of which are the thing itself.

`crossmarket.py` defines the causal state and takes a fetcher. `attribution.py`
decomposes gold's move across it. Neither could run, because nothing supplied
the drivers — the cycle has been reporting UNAVAILABLE every day, correctly.

This is the fetcher, built entirely from sources that cost nothing: Yahoo for
what it carries, FRED for the two series Yahoo does not have. FRED needs a key,
which is free and takes a minute to obtain; Yahoo needs nothing at all.

EXACT VERSUS PROXY IS THE WHOLE POINT OF THIS FILE

Two of the five drivers have no free direct source, and the tempting move is to
substitute something correlated and say nothing. That would be the worst kind of
error here, because `attribution.py` reports a SIGN VIOLATION when a fitted beta
contradicts the declared one — and a proxy with a different sign convention
would fire that alarm forever while the market did nothing unusual. So every
observation carries `exact: bool`, the proxy formula is written down where it is
used, and `coverage_note` states plainly which numbers are the thing and which
merely move with it.

    dxy               EXACT   ICE dollar index
    spx               EXACT   S&P 500
    vix               EXACT   CBOE volatility index
    real_yield_10y    EXACT   FRED DFII10, the 10y TIPS constant-maturity yield
                      PROXY   TIP/IEF total-return ratio when no FRED key
    breakeven_10y     EXACT   FRED T10YIE
                      ABSENT  no free proxy that is not circular

THE LAST LINE MATTERS. A breakeven proxy built from the same nominal yield and
the same TIPS ETF the real-yield proxy uses is not a second observation; it is
the first one rearranged, and feeding both to a regression manufactures
collinearity that looks like two independent drivers. Absent is the honest
answer, and `crossmarket` already reads absence as UNAVAILABLE rather than zero.

NOTHING HERE IS FETCHED AT IMPORT

Every network call is inside a function, deferred, and wrapped. A driver feed
that raises takes the daily cycle's attribution step with it; a driver feed that
raises at import takes the whole desk.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

DRIVERS_FREE_VERSION = "drivfree-2026-08-18-a"

#: Yahoo tickers for the drivers it actually carries.
YF = {
    "dxy": "DX-Y.NYB",
    "spx": "^GSPC",
    "vix": "^VIX",
    "nominal_10y": "^TNX",          # quoted as yield x 10; scaled below
    "tips_etf": "TIP",
    "treas_etf": "IEF",
}

#: FRED series for the two Yahoo cannot supply. Free key from
#: https://fred.stlouisfed.org/docs/api/api_key.html
FRED = {"real_yield_10y": "DFII10", "breakeven_10y": "T10YIE"}


@dataclass(frozen=True)
class DriverPoint:
    key: str
    change_pct: Optional[float]
    level: Optional[float]
    as_of: Optional[datetime]
    source: str
    #: False when this is a correlate rather than the series itself. Carried so
    #: a proxy cannot silently trip attribution's sign-violation alarm.
    exact: bool = True
    why: str = ""

    @property
    def observed(self) -> bool:
        return self.change_pct is not None


def _yf_history(ticker: str, days: int = 30):
    """Deferred import, deferred call, never raises."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — free driver feed unavailable")
        return None
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        df = yf.download(ticker, start=start, interval="1d",
                         progress=False, auto_adjust=False)
    except Exception as e:                           # noqa: BLE001
        log.warning("yfinance %s failed: %s", ticker, e)
        return None
    if df is None or df.empty:
        return None
    try:
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception:                                # noqa: BLE001
        pass
    return df


def _pct_change(df, lookback: int = 1) -> tuple:
    if df is None or len(df) < lookback + 1:
        return None, None, None
    col = "Close" if "Close" in df.columns else df.columns[-1]
    s = df[col].dropna()
    if len(s) < lookback + 1:
        return None, None, None
    a, b = float(s.iloc[-1 - lookback]), float(s.iloc[-1])
    if a == 0:
        return None, b, s.index[-1].to_pydatetime()
    ts = s.index[-1]
    return (b - a) / abs(a) * 100.0, b, (ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else None)


def fetch_yahoo(lookback: int = 1) -> dict:
    """The three drivers Yahoo carries exactly, plus a nominal-yield reading."""
    out: dict = {}
    for key in ("dxy", "spx", "vix"):
        pct, lvl, ts = _pct_change(_yf_history(YF[key]), lookback)
        out[key] = DriverPoint(key, pct, lvl, ts, f"yahoo/{YF[key]}", exact=True,
                               why="the index itself")
    return out


def fetch_real_yield(fred_key: Optional[str] = None,
                     lookback: int = 1) -> DriverPoint:
    """FRED's TIPS yield where a key exists, an ETF-ratio PROXY where it does not.

    The proxy is TIP/IEF: inflation-protected against nominal Treasuries at
    comparable duration. When real yields RISE, TIPS underperform nominals and
    the ratio falls — so the proxy moves OPPOSITE to the yield, and the sign is
    inverted here rather than left for a caller to discover. That inversion is
    exactly the kind of thing that silently flips an attribution beta, so it is
    done once, here, and labelled.
    """
    key = fred_key or os.environ.get("FRED_API_KEY", "").strip()
    if key:
        pt = _fetch_fred(FRED["real_yield_10y"], key, lookback)
        if pt is not None:
            return pt
    tip = _yf_history(YF["tips_etf"])
    ief = _yf_history(YF["treas_etf"])
    p_tip, _, ts = _pct_change(tip, lookback)
    p_ief, _, _ = _pct_change(ief, lookback)
    if p_tip is None or p_ief is None:
        return DriverPoint("real_yield_10y", None, None, None, "UNAVAILABLE",
                           exact=False,
                           why="no FRED key and the ETF proxy did not fetch")
    # Ratio falls as real yields rise, hence the negation.
    return DriverPoint("real_yield_10y", -(p_tip - p_ief), None, ts,
                       "PROXY:yahoo/TIP-IEF", exact=False,
                       why=("TIP/IEF relative move, sign INVERTED so it points "
                            "the same way as a yield. A correlate, not the "
                            "yield: set FRED_API_KEY for DFII10, which is free."))


def fetch_breakeven(fred_key: Optional[str] = None,
                    lookback: int = 1) -> DriverPoint:
    """FRED T10YIE, or ABSENT. There is deliberately no proxy.

    Any breakeven built from the same nominal yield and the same TIPS ETF the
    real-yield proxy uses is not a second observation — it is the first one
    rearranged. Feeding both to a regression manufactures collinearity that
    looks like two independent drivers, and the ridge penalty then splits a
    single impulse across two coefficients that mean nothing individually.
    """
    key = fred_key or os.environ.get("FRED_API_KEY", "").strip()
    if key:
        pt = _fetch_fred(FRED["breakeven_10y"], key, lookback)
        if pt is not None:
            return pt
    return DriverPoint(
        "breakeven_10y", None, None, None, "UNAVAILABLE", exact=False,
        why=("no free proxy exists that is not circular with the real-yield "
             "proxy. FRED T10YIE is free with a key; absence is the honest "
             "answer until then."))


def _fetch_fred(series: str, api_key: str, lookback: int = 1) -> Optional[DriverPoint]:
    """One FRED series. Never raises; returns None so the caller can fall back."""
    try:
        import requests
    except ImportError:
        return None
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=45)).date().isoformat()
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series, "api_key": api_key, "file_type": "json",
                    "observation_start": start},
            timeout=15)
        if r.status_code != 200:
            log.warning("FRED %s: HTTP %s", series, r.status_code)
            return None
        obs = [o for o in r.json().get("observations", []) if o.get("value") not in (".", "", None)]
    except Exception as e:                           # noqa: BLE001
        log.warning("FRED %s failed: %s", series, e)
        return None
    if len(obs) < lookback + 1:
        return None
    a, b = float(obs[-1 - lookback]["value"]), float(obs[-1]["value"])
    ts = datetime.fromisoformat(obs[-1]["date"]).replace(tzinfo=timezone.utc)
    # Yields are already in percent, so the informative quantity is the CHANGE
    # IN BASIS POINTS, not a percentage change of a percentage — the latter is
    # unstable near zero and meaningless when the level crosses it.
    return DriverPoint(series_key(series), (b - a) * 100.0, b, ts,
                       f"FRED/{series}", exact=True,
                       why="the published series, change in basis points")


def series_key(series_id: str) -> str:
    for k, v in FRED.items():
        if v == series_id:
            return k
    return series_id


def build_drivers(fred_key: Optional[str] = None, lookback: int = 1) -> dict:
    """Every driver this desk can get for free, each labelled exact or proxy."""
    out = fetch_yahoo(lookback)
    out["real_yield_10y"] = fetch_real_yield(fred_key, lookback)
    out["breakeven_10y"] = fetch_breakeven(fred_key, lookback)
    return out


def coverage_note(points: dict) -> str:
    obs = [p for p in points.values() if p.observed]
    exact = [p for p in obs if p.exact]
    proxy = [p for p in obs if not p.exact]
    absent = [k for k, p in points.items() if not p.observed]
    lines = [f"DRIVER COVERAGE  ({DRIVERS_FREE_VERSION})  "
             f"{len(obs)}/{len(points)} observed"]
    for p in points.values():
        tag = "EXACT" if p.exact else "PROXY"
        if not p.observed:
            lines.append(f"  {p.key:<16} ABSENT   {p.why}")
        else:
            lines.append(f"  {p.key:<16} {tag:<8} {p.change_pct:+8.3f}  "
                         f"{p.source}")
    if proxy:
        lines.append("")
        lines.append("  A PROXY IS NOT THE SERIES. attribution.py reports a sign "
                     "violation when a fitted beta contradicts the declared one, "
                     "and a proxy with a different sign convention would fire "
                     "that alarm forever while the market did nothing unusual.")
    if absent:
        lines.append(f"  absent: {', '.join(absent)} — read as UNAVAILABLE, "
                     f"never as zero.")
    return "\n".join(lines)
