"""Who is actually holding gold: ETF tonnage and speculative positioning.

WHAT THIS CLOSES, AND WHAT IT HONESTLY DOES NOT

The analyst reads structure, levels, sessions and macro. Elite gold operators additionally read
COMEX order flow, dealer options gamma, physical/ETF flows, CTA positioning and central bank
demand. Two of those five are free and real; three are not, and pretending otherwise would put a
confident number in the prompt resting on nothing.

  BUILT, BECAUSE THE DATA GENUINELY EXISTS AND IS FREE
    ETF TONNAGE. SPDR publishes GLD's holdings every day. Creations and redemptions are physical
    demand expressing itself -- tonnes leaving the trust is real metal being sold, not an opinion
    about it -- and it is one of the few gold series that is a FLOW rather than a price.
    SPECULATIVE POSITIONING. The CFTC's Commitments of Traders gives managed-money net length
    weekly. Managed money is the standard public proxy for CTA/trend-follower positioning, and
    crowding is the mechanism: a stretched net long is fuel for a liquidation, not a forecast.

  NOT BUILT, AND THE REASON MATTERS MORE THAN THE GAP
    COMEX ORDER FLOW. Tape and depth require a paid CME subscription. There is no free version,
    and a "proxy" assembled from bar volume would be a different measurement wearing its name.
    DEALER OPTIONS GAMMA. Options open interest is published; DEALER SIGN IS NOT. Every public
    GEX figure rests on an assumption about which side dealers are on, and that assumption is
    the entire calculation. A number this desk could not falsify has no business in a prompt
    that forbids unfalsifiable claims.
    CENTRAL BANK DEMAND. World Gold Council, quarterly, roughly six weeks late. It is a real and
    powerful driver of the multi-year level and it cannot condition an M15 read; carrying it
    would be theatre.

EVERY VALUE IS EVIDENCE WITH NO VOTE. Same standing as `trend`, `macro` and every Context field.
The module reports what is held and how it changed; it never says whether that is bullish.

STALENESS IS REPORTED, NEVER HIDDEN. Both series update on their own calendar -- GLD daily, COT
weekly with a three-day publication lag -- so a cached value is normal and its AGE is the thing
that decides whether it can be reasoned from. A fetch failure falls back to the cache WITH ITS
AGE STATED, and an empty cache renders UNMEASURED rather than a zero.

QUANT ALREADY MEASURED THE OBVIOUS USE AND IT LOST. The sibling desk swept COT-conditioned
ENTRIES across FX and metals: zero cells cleared its multiplicity-corrected bar and gold's were
negative or flat. That is a finding about COT AS AN ENTRY TRIGGER, which is not what this is for
-- crowding here is context the analyst weighs, exactly as it weighs the dollar. It is recorded
here so nobody re-derives that dead end believing it is new.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: SPDR's own published archive of GLD holdings. First-party, not a scraper's copy.
GLD_URL = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"

#: CFTC's Socrata endpoint for the disaggregated futures-only report, NOT the positional
#: f_disagg.txt. The flat file has no header and must be read by field INDEX, and this module's
#: own rule is that a silently mis-parsed column is a plausible wrong number -- the worst kind.
#: Named fields cannot shift under us; a renamed one returns nothing and says so. Filtered and
#: ordered server-side so the response is one row, not a year of them.
COT_URL = ("https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
           "?cftc_contract_market_code=088691"
           "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=1")
COT_GOLD_CODE = "088691"

#: Shanghai Gold Exchange, first-party. Deliberately NOT via akshare: that scrapes East Money
#: and breaks when the page moves, and it drags a large dependency in for one number. SGE is the
#: world's largest PHYSICAL gold market, and its premium or discount to London is Chinese
#: physical demand expressed as a price -- the one genuinely free thing on this desk that a
#: Western structural read does not already contain.
SGE_URL = "https://www.sge.com.cn/graph/Dailyhq"
SGE_CONTRACT = "Au99.99"

#: The two legs needed to put a CNY/gram price into USD/oz alongside London.
USDCNY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/CNY=X?range=5d&interval=1d"
LONDON_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d"

GRAMS_PER_TROY_OZ = 31.1034768

#: SGE publishes on Chinese trading days, so a long weekend or a mainland holiday week is
#: normal. Past this the premium is an assumption about a market that has since moved.
SGE_MAX_AGE_D = 5

#: Beyond this a value stops being "the last print" and becomes an assumption. GLD publishes
#: daily, so 5 days covers a holiday week without inventing a fortnight. COT is weekly with a
#: publication lag, so 14 days covers one missed release and refuses two.
GLD_MAX_AGE_D = 5
COT_MAX_AGE_D = 14

_UA = {"User-Agent": "Mozilla/5.0 (aurum-desk flows collector)"}


@dataclass
class FlowState:
    """What is held, and how it changed. No verdict."""
    gld_tonnes: Optional[float] = None
    gld_change_1d: Optional[float] = None
    gld_change_5d: Optional[float] = None
    gld_change_20d: Optional[float] = None
    gld_as_of: Optional[str] = None
    mm_net: Optional[int] = None                 # managed money net contracts
    mm_net_change: Optional[int] = None          # vs the prior week
    mm_pctile_52w: Optional[float] = None        # where this sits in a year of its own history
    cot_as_of: Optional[str] = None
    #: Shanghai physical, put on London's terms. `sge_premium_usd_oz` is the number that
    #: matters: positive means Shanghai is paying UP for metal against London, which is
    #: physical demand, and negative means the opposite. Both legs of the conversion are
    #: stored so the premium can be audited rather than trusted.
    sge_usd_oz: Optional[float] = None
    sge_cny_gram: Optional[float] = None
    sge_premium_usd_oz: Optional[float] = None
    usdcny: Optional[float] = None
    london_usd_oz: Optional[float] = None
    sge_as_of: Optional[str] = None
    fetched_utc: Optional[str] = None
    errors: dict = field(default_factory=dict)

    def _age_days(self, stamp: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
        if not stamp:
            return None
        now = now or datetime.now(timezone.utc)
        try:
            d = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (now - d).total_seconds() / 86400.0

    def to_prompt(self, now: Optional[datetime] = None) -> str:
        lines = ["[FLOWS]  who is holding gold -- evidence only, no verdict"]

        age = self._age_days(self.gld_as_of, now)
        if self.gld_tonnes is None:
            why = self.errors.get("gld", "never fetched")
            lines.append(f"  ETF TONNAGE (GLD)        UNMEASURED -- {why}")
        elif age is not None and age > GLD_MAX_AGE_D:
            lines.append(f"  ETF TONNAGE (GLD)        STALE ({age:.0f}d old, "
                         f"limit {GLD_MAX_AGE_D}d) -- not reasoned from")
        else:
            def chg(v: Optional[float], label: str) -> str:
                return f"{label} {v:+.1f}t" if v is not None else f"{label} n/a"
            lines.append(
                f"  ETF TONNAGE (GLD)        {self.gld_tonnes:,.1f}t   "
                + "  ".join((chg(self.gld_change_1d, "1d"), chg(self.gld_change_5d, "5d"),
                             chg(self.gld_change_20d, "20d")))
                + f"   as of {self.gld_as_of}")
            lines.append("                           tonnes leaving the trust is metal actually "
                         "sold, not a view about it")

        age = self._age_days(self.cot_as_of, now)
        if self.mm_net is None:
            why = self.errors.get("cot", "never fetched")
            lines.append(f"  SPEC POSITIONING (COT)   UNMEASURED -- {why}")
        elif age is not None and age > COT_MAX_AGE_D:
            lines.append(f"  SPEC POSITIONING (COT)   STALE ({age:.0f}d old, "
                         f"limit {COT_MAX_AGE_D}d) -- not reasoned from")
        else:
            pct = (f"{self.mm_pctile_52w:.0%} of its own 52w range"
                   if self.mm_pctile_52w is not None else "range unmeasured")
            chg = f"{self.mm_net_change:+,}" if self.mm_net_change is not None else "n/a"
            lines.append(f"  SPEC POSITIONING (COT)   managed money net {self.mm_net:+,} "
                         f"contracts   wk {chg}   {pct}   as of {self.cot_as_of}")
            lines.append("                           crowding is fuel for a liquidation, not a "
                         "direction; quant measured COT as an ENTRY trigger and it lost")

        age = self._age_days(self.sge_as_of, now)
        if self.sge_premium_usd_oz is None:
            why = self.errors.get("sge", "never fetched")
            lines.append(f"  SHANGHAI PREMIUM (SGE)   UNMEASURED -- {why}")
        elif age is not None and age > SGE_MAX_AGE_D:
            lines.append(f"  SHANGHAI PREMIUM (SGE)   STALE ({age:.0f}d old, "
                         f"limit {SGE_MAX_AGE_D}d) -- not reasoned from")
        else:
            side = "OVER" if self.sge_premium_usd_oz >= 0 else "UNDER"
            lines.append(
                f"  SHANGHAI PREMIUM (SGE)   {self.sge_premium_usd_oz:+.2f} USD/oz  "
                f"(Shanghai ${self.sge_usd_oz:,.2f} {side} London "
                f"${self.london_usd_oz:,.2f}; {self.sge_cny_gram:.2f} CNY/g @ "
                f"{self.usdcny:.4f})   as of {self.sge_as_of}")
            lines.append("                           the largest PHYSICAL market paying up or "
                         "down for metal against London -- demand, not a view")

        lines.append("[/FLOWS]")
        return "\n".join(lines)


def parse_yahoo_close(txt: str) -> Optional[float]:
    """Last non-null close from a Yahoo chart response. None, never a default."""
    try:
        res = json.loads(txt)["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    for v in reversed(closes or []):
        if isinstance(v, (int, float)):
            return float(v)
    return None


def parse_sge(txt: str, contract: str = SGE_CONTRACT) -> tuple[Optional[float], Optional[str]]:
    """(CNY per gram, as-of ISO date) for one SGE contract.

    BY NAME, AND IT REFUSES. SGE's payload shape is not something this desk can pin from
    outside China, so the parser walks the JSON for a record whose contract field matches and
    reads its close by key. An unrecognised shape returns None: a mis-read gold price would be
    a plausible number in the prompt every day, and the premium computed from it would be
    confidently wrong rather than absent.
    """
    try:
        doc = json.loads(txt)
    except json.JSONDecodeError:
        return None, None

    def rows(node):
        if isinstance(node, list):
            for item in node:
                yield from rows(item)
        elif isinstance(node, dict):
            if any(k in node for k in ("instid", "Instid", "variety", "contract")):
                yield node
            for v in node.values():
                if isinstance(v, (list, dict)):
                    yield from rows(v)

    for row in rows(doc):
        name = str(row.get("instid") or row.get("Instid") or row.get("variety")
                   or row.get("contract") or "")
        if contract.lower() not in name.lower():
            continue
        price = None
        for key in ("close", "Close", "closeprice", "last", "lastprice", "price", "settle"):
            v = row.get(key)
            try:
                if v is not None and float(v) > 0:
                    price = float(v)
                    break
            except (TypeError, ValueError):
                continue
        if price is None:
            continue
        as_of = None
        for key in ("date", "Date", "tradedate", "time", "updatetime"):
            raw = str(row.get(key) or "")[:10]
            for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
                try:
                    as_of = datetime.strptime(raw, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            if as_of:
                break
        return price, as_of
    return None, None


def sge_premium(cny_per_gram: float, usdcny: float,
                london_usd_oz: float) -> tuple[float, float]:
    """(SGE in USD/oz, premium over London in USD/oz).

    CNY/gram -> USD/oz needs both legs, and both are stored on the state so the arithmetic can
    be checked rather than believed. A wrong USDCNY would move the premium by more than the
    premium itself typically is.
    """
    usd_oz = (cny_per_gram / usdcny) * GRAMS_PER_TROY_OZ
    return round(usd_oz, 2), round(usd_oz - london_usd_oz, 2)


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - fixed https consts
        return r.read().decode("utf-8", errors="replace")


def parse_gld(csv_text: str) -> tuple[Optional[float], Optional[str], list[float]]:
    """(latest tonnes, as-of ISO date, full descending series).

    Format-tolerant on purpose: SPDR's column order has moved before. Finds the date column and
    the tonnes column by NAME, and if the header is unrecognisable returns nothing rather than
    guessing an index -- a silently mis-parsed column would be a plausible wrong number, which
    is the worst kind.
    """
    # csv.reader, NOT split(","). SPDR quotes its numbers and they contain thousands
    # separators -- "98,000,000,000" -- so a naive split shatters every row into fragments and
    # silently yields either nothing or the wrong column. Caught by a fixture before it ever
    # reached the box.
    import csv
    import io

    rows = [r for r in csv.reader(io.StringIO(csv_text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        return None, None, []
    header = [h.strip().lower() for h in rows[0]]
    di = next((i for i, h in enumerate(header) if "date" in h), None)
    ti = next((i for i, h in enumerate(header) if "tonne" in h), None)
    if di is None or ti is None:
        return None, None, []

    series: list[float] = []
    latest_date = None
    for parts in rows[1:]:
        parts = [p.strip() for p in parts]
        if len(parts) <= max(di, ti):
            continue
        try:
            tonnes = float(parts[ti].replace(",", ""))
        except ValueError:
            continue
        if latest_date is None:
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                try:
                    latest_date = datetime.strptime(parts[di], fmt).date().isoformat()
                    break
                except ValueError:
                    continue
        series.append(tonnes)
    if not series:
        return None, None, []
    return series[0], latest_date, series


def parse_cot(txt: str, code: str = COT_GOLD_CODE) -> tuple[Optional[int], Optional[str]]:
    """(managed money net contracts, as-of ISO date) from the CFTC's named-field JSON.

    BY NAME, NOT BY POSITION. The positional flat file requires counting commas in a
    header-less layout, and getting that count wrong yields a number that looks entirely
    plausible and is about the wrong column. Here a renamed or missing field produces None --
    an honest gap -- rather than a confident fiction. There is likewise no such thing as a zero
    net position by absence, so a row that will not parse returns None.
    """
    try:
        rows = json.loads(txt)
    except json.JSONDecodeError:
        return None, None
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if code and str(row.get("cftc_contract_market_code", "")).strip() != code:
            continue
        try:
            net = (int(float(row["m_money_positions_long_all"]))
                   - int(float(row["m_money_positions_short_all"])))
        except (KeyError, TypeError, ValueError):
            continue
        as_of = None
        raw = str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                as_of = datetime.strptime(raw, fmt).date().isoformat()
                break
            except ValueError:
                continue
        return net, as_of
    return None, None


def collect(cache: Optional[Path] = None, *, getter=_get) -> FlowState:
    """Fetch both series, falling back to cache per-series rather than all-or-nothing.

    `getter` is injected so the parsing and staleness logic are testable without a network --
    the desk's own rule that a test needing the internet is a test that will not run.
    """
    prev = load(cache) if cache else FlowState()
    st = FlowState(fetched_utc=datetime.now(timezone.utc).isoformat())

    try:
        tonnes, as_of, series = parse_gld(getter(GLD_URL))
        if tonnes is None:
            raise ValueError("GLD CSV header not recognised -- refusing to guess a column")
        st.gld_tonnes, st.gld_as_of = tonnes, as_of
        for span, attr in ((1, "gld_change_1d"), (5, "gld_change_5d"), (20, "gld_change_20d")):
            if len(series) > span:
                setattr(st, attr, round(series[0] - series[span], 2))
    except Exception as e:                                    # noqa: BLE001
        st.errors["gld"] = f"{type(e).__name__}: {str(e)[:90]}"
        st.gld_tonnes, st.gld_as_of = prev.gld_tonnes, prev.gld_as_of
        st.gld_change_1d, st.gld_change_5d = prev.gld_change_1d, prev.gld_change_5d
        st.gld_change_20d = prev.gld_change_20d

    try:
        net, as_of = parse_cot(getter(COT_URL))
        if net is None:
            raise ValueError(f"gold market code {COT_GOLD_CODE} not found in the COT file")
        st.mm_net, st.cot_as_of = net, as_of
        if prev.mm_net is not None and prev.cot_as_of != as_of:
            st.mm_net_change = net - prev.mm_net
        else:
            st.mm_net_change = prev.mm_net_change
        st.mm_pctile_52w = prev.mm_pctile_52w
    except Exception as e:                                    # noqa: BLE001
        st.errors["cot"] = f"{type(e).__name__}: {str(e)[:90]}"
        st.mm_net, st.cot_as_of = prev.mm_net, prev.cot_as_of
        st.mm_net_change, st.mm_pctile_52w = prev.mm_net_change, prev.mm_pctile_52w

    # SHANGHAI PREMIUM. Three legs, and it is ALL-OR-NOTHING on purpose: a premium computed
    # from a stale FX rate or yesterday's London close is not a smaller measurement, it is a
    # different and wrong one. Any missing leg falls back to the cached premium with its age.
    try:
        cny_gram, as_of = parse_sge(getter(SGE_URL))
        if cny_gram is None:
            raise ValueError(f"{SGE_CONTRACT} not found, or SGE payload shape unrecognised")
        usdcny = parse_yahoo_close(getter(USDCNY_URL))
        london = parse_yahoo_close(getter(LONDON_URL))
        if not usdcny or not london:
            raise ValueError(f"conversion leg missing (usdcny={usdcny}, london={london})")
        usd_oz, prem = sge_premium(cny_gram, usdcny, london)
        st.sge_cny_gram, st.usdcny, st.london_usd_oz = cny_gram, round(usdcny, 4), london
        st.sge_usd_oz, st.sge_premium_usd_oz, st.sge_as_of = usd_oz, prem, as_of
    except Exception as e:                                    # noqa: BLE001
        st.errors["sge"] = f"{type(e).__name__}: {str(e)[:90]}"
        for attr in ("sge_usd_oz", "sge_cny_gram", "sge_premium_usd_oz",
                     "usdcny", "london_usd_oz", "sge_as_of"):
            setattr(st, attr, getattr(prev, attr))

    if cache:
        save(st, cache)
    return st


def save(st: FlowState, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(asdict(st), indent=2), encoding="utf-8")


def load(path: Optional[Path]) -> FlowState:
    if not path or not Path(path).exists():
        return FlowState()
    try:
        return FlowState(**json.loads(Path(path).read_text(encoding="utf-8")))
    except Exception:                                          # noqa: BLE001
        return FlowState()                                     # unreadable == unmeasured
