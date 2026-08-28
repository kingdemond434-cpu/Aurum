"""Immutable capture of external Gold calls. Collector only — decides nothing.

WHY THIS EXISTS AND WHY IT IS SEPARATE FROM THE DESK

External signal data is PERISHABLE in a way market data is not. A provider can
delete a losing call, edit an entry price after the move, widen a target, or
move a stop retrospectively. Scraping a channel's history months later gives you
their edited highlight reel, and any statistic computed from it is a measurement
of their editing, not their trading.

So the collector has to exist BEFORE the analysis that would use it, and it has
to be append-only. That is the whole justification for building this now, while
the desk itself has no measured edge and is in no position to consume it.

WHAT THIS DELIBERATELY DOES NOT DO

  * It does not score providers.
  * It does not compute consensus, confidence, or "effective independent sources".
  * It does not cluster, weight, rank, or aggregate anything.
  * It touches no part of the trading path and no LiveDesk ever imports it.

Those are all downstream questions that need months of captured data to answer,
and every one of them introduces thresholds. A derived scalar with a tuned
cutoff sitting upstream of a trade decision is a rule, whatever it is called.
When the time comes, the analyst should receive the raw calls plus what
measurably happened after comparable ones, and do the synthesis itself.

WHAT IT RECORDS, AND WHY EACH FIELD

  received_utc      when WE saw it, on our clock — never the provider's claim
  raw_text          the message verbatim, always, even when parsing fails
  parsed            best-effort entry/SL/TP, with the parse marked uncertain
                    rather than dropped, so parser quality can be audited later
  edit / deletion   recorded as NEW events. The original is never overwritten;
                    that difference is the entire point of the exercise
  quote_at_receipt  gold bid/ask the moment the message arrived, so "posted
                    after the move" is measurable instead of arguable
  prev_hash         hash chain over the log, so a later edit to OUR OWN records
                    is detectable too. A tamper-evident log kept by an
                    interested party is worth more than a trusted one

RUNNING IT (on your machine — this container's egress to Telegram is blocked)

    pip install telethon pandas pyarrow MetaTrader5
    python signals_capture.py --setup          # one-time login
    python signals_capture.py --run            # then leave it running

Capture only channels you have legitimate access to, under their terms.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG_VERSION = "signalcap-2026-08-14-a"
DEFAULT_LOG = Path("external/signals.jsonl")
DEFAULT_STATE = Path("external/capture_state.json")


# --------------------------------------------------------------------------
# Parsing — best effort, never destructive
# --------------------------------------------------------------------------

DIRECTION_RE = re.compile(r"\b(buy|long|sell|short)\b", re.I)
# Gold trades in the low thousands; a 3-5 digit number, optional thousands
# separator, optional decimals. Written to accept "4,362.50" and "4362".
# Two alternatives, longest-first. The separator is `[,.]` in BOTH positions
# because channels mix conventions freely: "4,362.50", "4362,50" and "4362.5"
# all appear, sometimes in the same message. The thousands separator is
# MANDATORY in the first alternative so that "4362,50" falls through to the
# second and keeps its decimals, instead of matching "4362" and dropping ",50".
PRICE = r"(\d{1,2}[,.]\d{3}(?:[.,]\d{1,2})?|\d{3,5}(?:[.,]\d{1,2})?)"
# NOTE: no \b before the alternation. `\b@` can never match — both sides of a
# space-then-@ are non-word characters, so there is no boundary there, and the
# first version of this silently failed to read "SELL @ 4385" as an entry.
ENTRY_RE = re.compile(rf"(?:entry|enter|zone|@|\bat\b|\bnow\b)\s*[:=-]?\s*{PRICE}", re.I)
SL_RE = re.compile(rf"(?:\bsl\b|stop\s*loss|\bstop\b)\s*[:=-]?\s*{PRICE}", re.I)
TP_RE = re.compile(rf"(?:\btp\d?\b|take\s*profit|\btarget\b)\s*[:=-]?\s*{PRICE}", re.I)
ANY_PRICE_RE = re.compile(PRICE)


def _num(s: str) -> Optional[float]:
    """Parse a price written by a human in any of the usual conventions.

    A blanket comma->dot replacement is wrong and was: "4,362.50" became
    "4.362.50" and failed to parse, so a perfectly well-formed call was recorded
    as having no entry. The comma means different things depending on what
    follows it, and both conventions appear in the same channels.
    """
    if not s:
        return None
    s = s.strip()
    try:
        if "," in s and "." in s:
            # "4,362.50" — comma is a thousands separator
            return float(s.replace(",", ""))
        if "," in s:
            head, _, tail = s.rpartition(",")
            # "4,362" is thousands; "4362,50" is a decimal comma
            return float(s.replace(",", "" if len(tail) == 3 else "."))
        return float(s)
    except (ValueError, AttributeError):
        return None


def parse_signal(text: str) -> dict:
    """Extract a trade call if there is one. Uncertainty is RECORDED, not hidden.

    A message that does not parse is still logged in full. Discarding
    unparseable messages would silently select for a provider's tidiest posts,
    which is the same bias as letting them delete their losers.
    """
    t = text or ""
    d = DIRECTION_RE.search(t)
    direction = None
    if d:
        w = d.group(1).lower()
        direction = "LONG" if w in ("buy", "long") else "SHORT"
    sl = _num(m.group(1)) if (m := SL_RE.search(t)) else None
    tps = [v for x in TP_RE.findall(t) if (v := _num(x)) is not None]
    entry = _num(m.group(1)) if (m := ENTRY_RE.search(t)) else None

    if entry is None and direction is not None:
        # Many real calls state the level bare: "GOLD BUY NOW 4362.50 sl ...".
        # Fall back to the first price that is not already spoken for as the
        # stop or a target. Marked uncertain via `complete` either way, so a
        # wrong guess is auditable rather than invisible.
        claimed = {sl, *tps} - {None}
        for cand in (v for x in ANY_PRICE_RE.findall(t)
                     if (v := _num(x)) is not None):
            if cand not in claimed:
                entry = cand
                break

    # A call is only actionable if we can reconstruct its risk. Say so plainly.
    complete = direction is not None and entry is not None and sl is not None
    return {"direction": direction, "entry": entry, "sl": sl, "tps": tps,
            "is_trade_call": direction is not None,
            "complete": complete,
            "parser": LOG_VERSION,
            "uncertain": (direction is not None and not complete)}


# --------------------------------------------------------------------------
# The append-only, hash-chained log
# --------------------------------------------------------------------------

class SignalLog:
    """Append-only JSONL with a hash chain. Nothing is ever rewritten."""

    def __init__(self, path: Path = DEFAULT_LOG):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return "genesis"
        last = None
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return "genesis"
        try:
            return json.loads(last).get("hash", "genesis")
        except json.JSONDecodeError:
            return "genesis"

    def append(self, event: dict) -> dict:
        event = dict(event)
        event["log_version"] = LOG_VERSION
        event["prev_hash"] = self._last
        body = json.dumps({k: v for k, v in event.items() if k != "hash"},
                          sort_keys=True, default=str)
        event["hash"] = hashlib.sha256(body.encode()).hexdigest()[:32]
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        self._last = event["hash"]
        return event

    def verify(self) -> tuple[bool, str]:
        """Walk the chain. Detects edits to OUR log, not just to theirs."""
        if not self.path.exists():
            return True, "no log yet"
        prev = "genesis"
        n = 0
        with self.path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("prev_hash") != prev:
                    return False, f"chain broken at line {i}: prev_hash mismatch"
                body = json.dumps({k: v for k, v in row.items() if k != "hash"},
                                  sort_keys=True, default=str)
                if hashlib.sha256(body.encode()).hexdigest()[:32] != row.get("hash"):
                    return False, f"row {i} was modified after it was written"
                prev = row["hash"]
                n += 1
        return True, f"{n} events, chain intact"

    def stats(self) -> dict:
        if not self.path.exists():
            return {}
        kinds: dict[str, int] = {}
        chans: dict[str, int] = {}
        calls = edits = dels = complete = 0
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                k = r.get("event", "?")
                kinds[k] = kinds.get(k, 0) + 1
                if k == "message":
                    chans[r.get("channel", "?")] = chans.get(r.get("channel", "?"), 0) + 1
                    p = r.get("parsed") or {}
                    calls += bool(p.get("is_trade_call"))
                    complete += bool(p.get("complete"))
                edits += (k == "edit")
                dels += (k == "deletion")
        return {"events": kinds, "channels": chans, "trade_calls": calls,
                "complete_calls": complete, "edits": edits, "deletions": dels}


# --------------------------------------------------------------------------
# Price at receipt — "posted after the move" must be measurable
# --------------------------------------------------------------------------

class QuoteSource:
    """Gold bid/ask at the instant a message arrives.

    Without this, "the call was posted after the move had already run" is an
    argument. With it, it is a number.
    """

    def __init__(self, symbol: str = "XAUUSD"):
        self.symbol = symbol
        self._mt5 = None
        try:
            import MetaTrader5 as mt5
            if mt5.initialize():
                self._mt5 = mt5
                mt5.symbol_select(symbol, True)
        except Exception:
            self._mt5 = None

    @property
    def available(self) -> bool:
        return self._mt5 is not None

    def quote(self) -> Optional[dict]:
        if not self._mt5:
            return None
        t = self._mt5.symbol_info_tick(self.symbol)
        if t is None:
            return None
        return {"bid": float(t.bid), "ask": float(t.ask),
                "server_time": int(t.time)}


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------

async def run_capture(channels: list[str], log: SignalLog, quotes: QuoteSource,
                      session: str = "external/aurum_capture") -> None:
    from telethon import TelegramClient, events

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH not set.\n"
              "Get them free at https://my.telegram.org -> API development tools.")
        return

    client = TelegramClient(session, int(api_id), api_hash)
    await client.start()
    print(f"connected. watching {len(channels)} channel(s). Ctrl-C to stop.")
    qmode = ("MT5 live" if quotes.available else
             "UNAVAILABLE — post-hoc reconstruction will be needed")
    print(f"quotes at receipt: {qmode}")

    chan_by_id: dict = {}

    def _resolve_channel(chat_id) -> str:
        return chan_by_id.get(chat_id, str(chat_id))

    def base(ev_kind: str, msg: Any, chan: str) -> dict:
        return {"event": ev_kind,
                "received_utc": datetime.now(timezone.utc).isoformat(),
                "channel": chan,
                "message_id": getattr(msg, "id", None),
                "provider_ts": (msg.date.isoformat()
                                if getattr(msg, "date", None) else None),
                "quote_at_receipt": quotes.quote()}

    @client.on(events.NewMessage(chats=channels))
    async def on_new(ev):
        chan = getattr(ev.chat, "username", None) or str(ev.chat_id)
        chan_by_id[ev.chat_id] = chan          # so deletions key the same way
        text = ev.message.message or ""
        row = base("message", ev.message, chan)
        row["raw_text"] = text
        row["parsed"] = parse_signal(text)
        log.append(row)
        p = row["parsed"]
        tag = ("CALL" if p["is_trade_call"] else "note")
        print(f"  [{tag}] {chan} #{ev.message.id} "
              f"{p.get('direction') or ''} "
              f"{'complete' if p['complete'] else ''}")

    @client.on(events.MessageEdited(chats=channels))
    async def on_edit(ev):
        # THE POINT OF THE WHOLE EXERCISE. The original stays; this is a new row.
        chan = getattr(ev.chat, "username", None) or str(ev.chat_id)
        chan_by_id[ev.chat_id] = chan
        row = base("edit", ev.message, chan)
        row["edited_text"] = ev.message.message or ""
        row["parsed_after_edit"] = parse_signal(row["edited_text"])
        log.append(row)
        print(f"  [EDIT] {chan} #{ev.message.id} — original preserved")

    @client.on(events.MessageDeleted(chats=channels))
    async def on_delete(ev):
        # Deletion events carry only a numeric chat_id while ordinary messages
        # are keyed by @username, so counting them naively attaches a channel's
        # deletions to a source id that has none of its messages — the deletion
        # rate, which is the entire point of this log, silently lands on the
        # wrong provider. Resolve back to the same key the messages used.
        chan = _resolve_channel(getattr(ev, "chat_id", None))
        for mid in ev.deleted_ids:
            log.append({"event": "deletion",
                        "received_utc": datetime.now(timezone.utc).isoformat(),
                        "channel": chan, "message_id": mid,
                        "quote_at_receipt": quotes.quote()})
            print(f"  [DELETE] {chan} #{mid} — recorded")

    await client.run_until_disconnected()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="start capturing")
    ap.add_argument("--setup", action="store_true", help="one-time Telegram login")
    ap.add_argument("--verify", action="store_true", help="check the hash chain")
    ap.add_argument("--stats", action="store_true", help="what has been captured")
    ap.add_argument("--channels", default="external/channels.txt",
                    help="one channel @username per line")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    args = ap.parse_args()

    log = SignalLog(Path(args.log))

    if args.verify:
        ok, why = log.verify()
        print(f"chain: {'INTACT' if ok else 'BROKEN'} — {why}")
        return 0 if ok else 1

    if args.stats:
        s = log.stats()
        if not s:
            print("nothing captured yet")
            return 0
        print(json.dumps(s, indent=2))
        n = s.get("trade_calls", 0)
        if n:
            print(f"\nedits as a share of trade calls    : "
                  f"{s.get('edits', 0) / n:.1%}")
            print(f"deletions as a share of trade calls: "
                  f"{s.get('deletions', 0) / n:.1%}")
            print("\nThose two numbers are the reason this log exists. A history "
                  "scraped later\nwould show neither.")
        return 0

    if args.setup:
        print("1. Get API credentials at https://my.telegram.org (free)")
        print("2. export TELEGRAM_API_ID=... TELEGRAM_API_HASH=...")
        print(f"3. list channels, one @username per line, in {args.channels}")
        print("4. python signals_capture.py --run")
        return 0

    if not args.run:
        ap.print_help()
        return 0

    cp = Path(args.channels)
    if not cp.exists():
        print(f"no channel list at {cp} — see --setup")
        return 1
    channels = [l.strip() for l in cp.read_text(encoding='utf-8').splitlines()
                if l.strip() and not l.startswith("#")]
    if not channels:
        print("channel list is empty")
        return 1

    try:
        asyncio.run(run_capture(channels, log, QuoteSource()))
    except KeyboardInterrupt:
        print("\nstopped. log is append-only; restart to continue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
