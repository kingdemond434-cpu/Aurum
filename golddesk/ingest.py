"""Turning MT5 history into analysable trades, every day, without losing anything.

The reverse-engineering module takes `Trade` rows. This makes them, from the
things a broker or a copy platform will actually hand you: an MT5 HTML
statement, a CSV export, or the deal dicts the MetaTrader5 Python API returns.

THREE WAYS THIS GOES WRONG SILENTLY, ALL HANDLED HERE

1. DEALS ARE NOT TRADES. MT5 history is a list of DEALS — an entry deal, then
   one or more exit deals against the same position. The naive parser treats
   each row as a trade, which doubles the count, halves the average size, turns
   every position into an instant round trip at the wrong price, and makes the
   basket analysis meaningless. Deals are paired by `position_id` here, and a
   position closed in three partials becomes ONE trade with a volume-weighted
   exit.

2. STATEMENT TIMES ARE BROKER TIME, NOT UTC. Almost every MT5 broker runs the
   server at UTC+2/+3, and a statement carries no timezone at all. Parse it as
   UTC and every entry lands two or three hours from where it happened — which
   silently destroys the entire session, sweep and news alignment that step 2 of
   the mandate depends on, while every timestamp still looks perfectly ordinary.
   `server_offset_hours` is REQUIRED and has no default. Guessing UTC would be
   the one error that corrupts everything downstream and shows no symptom.

3. RE-INGESTING DOUBLE-COUNTS. A daily run over an overlapping export must add
   only what is new. Dedup is on the deal ticket, which the broker guarantees
   unique, and the log persists — so the same file ingested twice yields nothing
   the second time.

WHAT IS DELIBERATELY NOT HERE

Any fetching, scraping or login. This module parses what you already have. What
is lawful to collect is a question about collection, and it stays with whoever
collects it rather than being answered implicitly by an import in a library.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from golddesk.reverse import Trade

INGEST_VERSION = "ingest-2026-08-18-a"

#: Deal `type` / `entry` vocabulary across MT5 exports. Statements localise the
#: words but keep the underlying in/out semantics, so both are matched.
_IN = {"in", "buy", "sell", "0", "entry_in"}
_OUT = {"out", "close", "1", "entry_out"}


class IngestError(Exception):
    """Refused rather than guessed. Every raise here is a case where continuing
    would produce plausible-looking rows that are wrong."""


@dataclass(frozen=True)
class Deal:
    """One MT5 deal. The atom a broker actually reports."""
    ticket: str
    position_id: str
    symbol: str
    #: BUY or SELL — the direction of THIS DEAL, which for an exit is the
    #: opposite of the position's direction. Resolved in `deals_to_trades`.
    action: str
    entry: str                      # IN | OUT
    volume: float
    price: float
    time_utc: datetime
    profit: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: str = ""


def _to_utc(naive: datetime, server_offset_hours: float) -> datetime:
    """Broker wall-clock -> UTC. The offset is the caller's to know."""
    return (naive - timedelta(hours=server_offset_hours)).replace(tzinfo=timezone.utc)


_TIME_FORMATS = ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S",
                 "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S")


def parse_time(raw: str, server_offset_hours: float) -> datetime:
    s = (raw or "").strip()
    for f in _TIME_FORMATS:
        try:
            return _to_utc(datetime.strptime(s, f), server_offset_hours)
        except ValueError:
            continue
    raise IngestError(
        f"unparseable timestamp {raw!r}. Known layouts: {', '.join(_TIME_FORMATS)}. "
        f"A wrong timestamp is worse than a missing one — it aligns the trade "
        f"against the wrong bars and nothing downstream can tell.")


def _f(raw, default=None) -> Optional[float]:
    """Statement numbers carry spaces, non-breaking spaces and thousands commas."""
    if raw is None:
        return default
    s = str(raw).replace("\xa0", "").replace(" ", "").replace(",", "").strip()
    if not s or s in ("-", "—"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


# ------------------------------------------------------------------ parsers

_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)


def parse_mt5_html(text: str, server_offset_hours: float) -> list:
    """Deals from an MT5 `ReportHistory-*.html` statement.

    Column ORDER is used rather than header names, because MT5 localises the
    headers — a German statement says "Zeit" and "Volumen" — and matching on
    English words silently returns zero deals from a perfectly good file. The
    Deals table has a stable layout: time, deal, symbol, type, direction,
    volume, price, order, commission, fee, swap, profit, balance, comment.
    """
    deals: list = []
    for rm in _ROW.finditer(text):
        cells = [_TAG.sub("", c).replace("&nbsp;", " ").strip()
                 for c in _CELL.findall(rm.group(1))]
        if len(cells) < 8:
            continue
        t_raw, ticket, symbol, dtype, direction, vol, price = cells[:7]
        if not re.match(r"^\d{4}[.\-]\d{2}[.\-]\d{2}", t_raw):
            continue                                  # header or summary row
        if not ticket.isdigit():
            continue
        v, p = _f(vol), _f(price)
        if v is None or p is None:
            continue
        ent = direction.strip().lower()
        if ent not in _IN and ent not in _OUT:
            continue                                  # balance/credit operations
        deals.append(Deal(
            ticket=ticket,
            # An MT5 statement does not print position_id. Deals against one
            # position share a symbol and alternate in/out in time order, so the
            # pairing is reconstructed in `deals_to_trades` by FIFO per symbol
            # rather than invented here.
            position_id="",
            symbol=symbol.strip(),
            action=dtype.strip().upper()[:4],
            entry="IN" if ent in _IN else "OUT",
            volume=v, price=p,
            time_utc=parse_time(t_raw, server_offset_hours),
            profit=_f(cells[11]) if len(cells) > 11 else None,
            comment=cells[-1] if len(cells) > 13 else ""))
    return deals


#: Header aliases for CSV exports, which vary by platform.
_ALIASES = {
    "ticket": ("ticket", "deal", "deal_id", "id", "order"),
    "position": ("position", "position_id", "positionid", "pos"),
    "symbol": ("symbol", "instrument", "pair"),
    "type": ("type", "action", "side", "cmd"),
    "entry": ("entry", "direction", "dir", "in_out"),
    "volume": ("volume", "lots", "size", "qty", "amount"),
    "price": ("price", "open_price", "fill_price", "rate"),
    "time": ("time", "open_time", "date", "datetime", "timestamp", "close_time"),
    "profit": ("profit", "pnl", "p/l", "net"),
    "sl": ("sl", "s/l", "stop", "stoploss", "stop_loss"),
    "tp": ("tp", "t/p", "target", "takeprofit", "take_profit"),
}


def _pick(row: dict, key: str):
    for alias in _ALIASES[key]:
        for k in row:
            if k and k.strip().lower().replace(" ", "_") == alias:
                return row[k]
    return None


def parse_csv(text: str, server_offset_hours: float) -> list:
    """Deals from a CSV export. Header names are matched by alias, not position."""
    rdr = csv.DictReader(io.StringIO(text))
    out: list = []
    for row in rdr:
        tk = _pick(row, "ticket")
        vol, px = _f(_pick(row, "volume")), _f(_pick(row, "price"))
        t_raw = _pick(row, "time")
        if not tk or vol is None or px is None or not t_raw:
            continue
        ent = str(_pick(row, "entry") or "").strip().lower()
        typ = str(_pick(row, "type") or "").strip().upper()
        out.append(Deal(
            ticket=str(tk).strip(),
            position_id=str(_pick(row, "position") or "").strip(),
            symbol=str(_pick(row, "symbol") or "").strip(),
            action=typ[:4],
            entry="OUT" if ent in _OUT else "IN",
            volume=vol, price=px,
            time_utc=parse_time(str(t_raw), server_offset_hours),
            profit=_f(_pick(row, "profit")),
            sl=_f(_pick(row, "sl")), tp=_f(_pick(row, "tp"))))
    return out


def parse_api_deals(rows: Iterable, server_offset_hours: float = 0.0) -> list:
    """Deals from `MetaTrader5.history_deals_get()`.

    The API returns epoch seconds in UTC already, so the offset defaults to
    zero HERE and only here — this is the one source where the timezone is not
    the caller's problem, and saying so is better than making every caller pass
    a zero they might get wrong.
    """
    out: list = []
    for r in rows:
        g = (lambda k, d=None: r.get(k, d)) if isinstance(r, dict) else (
            lambda k, d=None: getattr(r, k, d))
        vol, px = _f(g("volume")), _f(g("price"))
        if vol is None or px is None:
            continue
        ts = g("time")
        if ts is None:
            continue
        t = (datetime.fromtimestamp(float(ts), tz=timezone.utc)
             if not isinstance(ts, datetime) else ts)
        if t.tzinfo is None:
            t = _to_utc(t, server_offset_hours)
        ent_raw = g("entry", 0)
        out.append(Deal(
            ticket=str(g("ticket", "")), position_id=str(g("position_id", "")),
            symbol=str(g("symbol", "")),
            action="BUY" if int(g("type", 0) or 0) == 0 else "SELL",
            entry="OUT" if str(ent_raw).lower() in _OUT else "IN",
            volume=vol, price=px, time_utc=t, profit=_f(g("profit")),
            sl=_f(g("sl")), tp=_f(g("tp")), comment=str(g("comment", ""))))
    return out


# ------------------------------------------------------- deals -> trades

def deals_to_trades(deals: Sequence[Deal]) -> tuple:
    """Pair entry and exit deals into positions. Returns (trades, unmatched).

    Grouped by `position_id` when the source supplies one, and FIFO per symbol
    and side when it does not — which is the MT5 statement case, since a
    statement never prints the position id.

    PARTIAL CLOSES BECOME ONE TRADE. A position closed in three tranches is one
    decision, and counting it as three inflates the trade count, flatters the
    win rate whenever the winning tranche closes separately, and turns a single
    entry into what looks like a basket.

    Unmatched deals are RETURNED, not dropped. An entry with no exit is an open
    position; an exit with no entry means the export starts mid-position. Both
    are facts about the data and neither should vanish.
    """
    ordered = sorted(deals, key=lambda d: (d.time_utc, d.ticket))
    opens: dict = {}
    closes: dict = {}
    for d in ordered:
        key = d.position_id or f"{d.symbol}|{position_side(d)}"
        (opens if d.entry == "IN" else closes).setdefault(key, []).append(d)

    trades: list = []
    unmatched: list = []
    for key, ins in opens.items():
        outs = list(closes.pop(key, []))
        if not d_has_position_id(ins):
            # FIFO within symbol+side: the first entry is closed by the first
            # exit that follows it. Not perfect against a broker that closes
            # LIFO, which is why position_id is preferred wherever it exists.
            outs = sorted(outs, key=lambda d: d.time_utc)
        for i, entry in enumerate(ins):
            matched = [o for o in outs if o.time_utc >= entry.time_utc]
            if not matched:
                unmatched.append(entry)
                continue
            # Take exits until this entry's volume is covered — the partial-close
            # case. Volume-weighted exit price, because a single "close price"
            # for a position closed in tranches is a fiction.
            need, used, notional = entry.volume, [], 0.0
            for o in matched:
                if need <= 1e-9:
                    break
                take = min(need, o.volume)
                notional += take * o.price
                need -= take
                used.append(o)
            if not used:
                unmatched.append(entry)
                continue
            filled = entry.volume - max(need, 0.0)
            for o in used:
                outs.remove(o)
            trades.append(Trade(
                ticket=entry.ticket, symbol=entry.symbol,
                direction=position_side(entry), lots=entry.volume,
                open_utc=entry.time_utc,
                close_utc=max(o.time_utc for o in used),
                open_price=entry.price,
                close_price=notional / filled if filled > 0 else entry.price,
                sl=entry.sl, tp=entry.tp,
                profit=sum(o.profit for o in used if o.profit is not None) or None))
        unmatched.extend(outs)
    # Closes whose position was never opened in this export. The loop above only
    # visits keys that HAVE an entry, so without this an export beginning
    # mid-position silently discards its orphan exits — the one case where the
    # data is telling you the window is incomplete.
    for leftover in closes.values():
        unmatched.extend(leftover)
    return sorted(trades, key=lambda t: t.open_utc), unmatched


def position_side(deal: Deal) -> str:
    """The direction of the POSITION this deal belongs to, not of the deal.

    An exit deal's type is the OPPOSITE of the position it closes — a long is
    closed by a SELL deal. Grouping on the raw type therefore files the entry
    under BUY and its own exit under SELL, and they never pair: every position
    comes back as two unmatched deals and the trade list is empty. The module
    documented this and then did it anyway; the statement round-trip test is
    what caught it.
    """
    side = "BUY" if deal.action.upper().startswith("BUY") else "SELL"
    if deal.entry == "OUT":
        side = "SELL" if side == "BUY" else "BUY"
    return side


def d_has_position_id(deals: Sequence[Deal]) -> bool:
    return any(d.position_id for d in deals)


# ---------------------------------------------------------------- the log

@dataclass
class IngestLog:
    """What has already been taken in. Makes a daily re-run idempotent."""
    seen: set = field(default_factory=set)
    deals: list = field(default_factory=list)

    def add(self, deals: Iterable[Deal]) -> list:
        """Returns only the deals that are NEW. Dedup is on the broker's ticket,
        which is unique by guarantee — a content hash would treat a corrected
        row as a second trade."""
        fresh = []
        for d in deals:
            if not d.ticket or d.ticket in self.seen:
                continue
            self.seen.add(d.ticket)
            self.deals.append(d)
            fresh.append(d)
        return fresh

    def trades(self) -> tuple:
        return deals_to_trades(self.deals)

    def to_json(self) -> str:
        return json.dumps({
            "version": INGEST_VERSION,
            "deals": [{**d.__dict__, "time_utc": d.time_utc.isoformat()}
                      for d in self.deals]}, indent=1)

    def save(self, path: Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(p)                       # atomic: a torn write loses history

    @staticmethod
    def load(path: Path) -> "IngestLog":
        p = Path(path)
        if not p.exists():
            return IngestLog()
        d = json.loads(p.read_text(encoding='utf-8'))
        log = IngestLog()
        for row in d.get("deals", ()):
            row = dict(row)
            row["time_utc"] = datetime.fromisoformat(row["time_utc"])
            log.add([Deal(**row)])
        return log


def ingest_file(path: Path, server_offset_hours: Optional[float] = None,
                log: Optional[IngestLog] = None) -> tuple:
    """Read one export into a log. Returns (log, n_new, note).

    `server_offset_hours` is required for statements and CSVs and has no
    default. Almost every MT5 broker runs at UTC+2/+3 and prints no timezone;
    assuming UTC would shift every trade two or three hours from where it
    happened, misaligning every session, sweep and news inference while every
    timestamp still looks ordinary. There is no safe guess, so there is no guess.
    """
    p = Path(path)
    text = p.read_text(errors="replace", encoding="utf-8")
    log = log or IngestLog()
    if server_offset_hours is None:
        raise IngestError(
            "server_offset_hours is required. MT5 statements are in BROKER "
            "server time and carry no timezone; most brokers run UTC+2 or +3. "
            "Parsing as UTC silently misaligns every trade against the bars, "
            "and nothing downstream can detect it. Check one known trade's time "
            "against the chart and pass the difference.")
    if p.suffix.lower() in (".html", ".htm"):
        deals = parse_mt5_html(text, server_offset_hours)
        kind = "MT5 HTML statement"
    else:
        deals = parse_csv(text, server_offset_hours)
        kind = "CSV export"
    fresh = log.add(deals)
    trades, unmatched = log.trades()
    note = (f"{kind}: {len(deals)} deal(s) parsed, {len(fresh)} new "
            f"({len(deals) - len(fresh)} already seen) -> {len(trades)} paired "
            f"trade(s), {len(unmatched)} unmatched")
    if unmatched:
        note += (". Unmatched deals are open positions or an export that starts "
                 "mid-position — kept, not dropped.")
    return log, len(fresh), note
