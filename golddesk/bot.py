"""The Telegram bot — the channel answers back.

`notify.py` is a one-way sink: the desk speaks, you read. That is enough right
up until the moment you actually want something. Then you are SSH-ing into a VPS
at 2am to answer "is it still alive", "why did it not take that", "what is open"
— questions the desk already knows the answers to and had no way to be asked.

WHAT THIS DELIBERATELY CANNOT DO

It cannot trade. Not "is configured not to" — cannot. Aurum places no orders
anywhere (`run_desk.py --assert-no-orders` proves that by reading the package),
and this module adds no exception. Every command below either READS state that
already exists or sets the halt flag. There is no code path from a Telegram
message to a position.

That matters more here than anywhere else in the desk, because this is the only
component that takes instructions from outside the process. A chat message is
attacker-reachable in a way a config file is not: bot tokens leak, group chats
get forwarded, and `getUpdates` will faithfully deliver a message from anyone
who finds the bot. So the trust model is explicit:

  AUTHORISATION IS BY CHAT ID, CHECKED ON EVERY UPDATE. Not by username, which
  is user-changeable and spoofable in forwarded contexts. Not on the first
  message only. An unauthorised chat gets silence, not an error — an error
  message confirms the bot exists and is worth attacking.

  THE COMMAND SET IS A WHITELIST OF EXACT STRINGS. Not a prefix match, not a
  regex, and emphatically not anything that reaches eval, a shell, or a file
  path built from message text. Unknown commands are refused before dispatch.

  MESSAGE TEXT IS NEVER A PATH, A KEY, OR A FORMAT STRING. The one command
  taking an argument (`/why`) matches it against an enumerated set.

WHY LONG-POLL AND NOT A WEBHOOK

A webhook needs an inbound port, a public hostname and a TLS certificate on a
box whose entire security story is currently "nothing listens". Long-polling
needs an outbound HTTPS connection the desk already makes. The cost is up to
`poll_timeout` seconds of latency on a command, which for "how is it going" is
not a cost at all.

THREADING

`serve_forever()` is blocking and meant for its own thread or its own process.
It shares nothing mutable with the trading loop: it reads the checkpoint and
ledger files the service already writes, and communicates the other direction
through a single flag file. No locks, because there is no shared memory to
protect — the filesystem is the interface, and it is the same interface the
operator has with `cat` and `touch`.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

#: Long-poll seconds. Telegram holds the connection open this long waiting for
#: an update, so this is idle time, not request rate: 25s means roughly 3.5
#: requests per minute-of-silence, not 25.
POLL_TIMEOUT_S = 25

#: Telegram rejects messages over 4096 characters outright. Truncating with a
#: visible marker beats a send that silently fails and loses the answer.
MAX_MSG = 3900


@dataclass
class BotConfig:
    token: str
    #: The ONLY chat allowed to command the desk. Notifications go here too.
    chat_id: str
    state_path: Path = Path("state/service_state.json")
    ledger_path: Path = Path("state/ledger.jsonl")
    #: Presence of this file asks the trading loop to stand down. A FILE and not
    #: an in-process flag, so it survives a restart, works when the bot is dead,
    #: and can be set or cleared by hand with touch/rm during an incident.
    halt_path: Path = Path("state/HALTED")
    poll_timeout_s: int = POLL_TIMEOUT_S


# ------------------------------------------------------------------ transport

def _api(token: str, method: str, timeout: float, **params) -> Optional[dict]:
    """One Telegram call. Returns the `result` payload, or None on any failure.

    Never raises. A control channel that can throw into its own poll loop stops
    answering the moment the network hiccups, which is exactly when you want it.
    """
    try:
        import requests                              # deferred: never a hard dep
    except ImportError:
        log.warning("requests not installed — bot cannot run")
        return None
    try:
        r = requests.get(f"{API}/bot{token}/{method}", params=params, timeout=timeout)
        if r.status_code != 200:
            log.warning("telegram %s -> %s: %s", method, r.status_code, r.text[:200])
            return None
        body = r.json()
        if not body.get("ok"):
            log.warning("telegram %s not ok: %s", method, str(body)[:200])
            return None
        return body.get("result")
    except Exception as e:                           # noqa: BLE001
        log.warning("telegram %s failed: %s", method, e)
        return None


def send(token: str, chat_id: str, text: str, timeout: float = 10.0) -> bool:
    """Reply into a chat. Truncated visibly rather than silently dropped."""
    if len(text) > MAX_MSG:
        text = text[:MAX_MSG] + "\n… (truncated)"
    # Plain text, NOT Markdown. Ledger rows carry underscores and asterisks in
    # mechanism names and reason strings; Telegram rejects the whole message on
    # an unbalanced entity, so a reply about a signal named `fvg_*` would vanish
    # entirely. The answer arriving unstyled beats the answer not arriving.
    return _api(token, "sendMessage", timeout, chat_id=chat_id, text=text) is not None


# --------------------------------------------------------------------- reading

def _read_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(Path(p).read_text())
    except Exception:                                # noqa: BLE001
        return None


def _tail_ledger(p: Path, n: int = 400) -> list[dict]:
    """Last n rows. Bad lines are skipped, not fatal: the ledger is append-only
    and a torn final write costs one row, which must not cost the whole answer."""
    try:
        lines = Path(p).read_text().splitlines()
    except Exception:                                # noqa: BLE001
        return []
    out: list[dict] = []
    for ln in lines[-n:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _age(iso: Optional[str]) -> str:
    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(iso)
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f}m ago"
    return f"{secs / 3600:.1f}h ago"


# -------------------------------------------------------------------- commands

def cmd_status(cfg: BotConfig) -> str:
    st = _read_json(cfg.state_path)
    if st is None:
        return ("NO CHECKPOINT at " + str(cfg.state_path) +
                "\nThe desk has not written state. Either it never started, or "
                "it is running from a different directory.")
    halted = cfg.halt_path.exists()
    lines = [
        "AURUM — advisory desk (places no orders)",
        f"halt flag       {'SET — standing down' if halted else 'clear'}",
        f"last bar        {_age(st.get('last_bar_ts'))}",
        f"started         {_age(st.get('started_at'))}",
        f"restarts        {st.get('restarts', 0)}",
        f"bars processed  {st.get('bars_processed', 0)}",
        f"ticks seen      {st.get('ticks_seen', 0)}",
        f"reconnects      {st.get('reconnects', 0)}",
        f"stale suspends  {st.get('stale_suspensions', 0)}",
    ]
    # A checkpoint is not a heartbeat. The desk writes it after events that
    # change state, so a quiet market legitimately produces a stale-looking
    # file. Say what the number means rather than implying a fault.
    if st.get("open_trade"):
        lines.append("position        OPEN — see /positions")
    else:
        lines.append("position        flat")

    # THE CHANNEL'S OWN HEALTH. If you are reading this, the bot works — but the
    # bot and the desk are separate processes, so a working bot and a dead sink
    # is both possible and exactly the case worth surfacing: the desk would be
    # producing signals that reach nobody while looking perfectly alive.
    h = st.get("notification_health") or {}
    if h:
        if h.get("healthy") is False:
            lines.append(f"SIGNAL CHANNEL  DOWN — {h.get('consecutive_failures')} "
                         f"consecutive failures, {h.get('sent', 0)} delivered ever. "
                         f"Signals are going NOWHERE.")
        elif h.get("sent") is not None:
            lines.append(f"signal channel  ok — {h.get('sent')} delivered, "
                         f"{h.get('failed', 0)} failed")
        else:
            lines.append(f"signal channel  UNKNOWN — {h.get('sink', '?')} does not "
                         f"track delivery, which is not the same as healthy")
    return "\n".join(lines)


def cmd_positions(cfg: BotConfig) -> str:
    st = _read_json(cfg.state_path) or {}
    t = st.get("open_trade")
    if not t:
        return "flat — no open position in the checkpoint."
    keep = ("symbol", "direction", "entry", "stop", "tp1", "tp2", "size_r",
            "opened_at", "mechanism", "state_id", "shadow")
    rows = [f"{k:<12}{t[k]}" for k in keep if k in t]
    extra = [k for k in t if k not in keep]
    if extra:
        rows.append(f"(also: {', '.join(sorted(extra))})")
    return "OPEN POSITION\n" + "\n".join(rows)


def _kind(row: dict) -> str:
    return str(row.get("kind") or row.get("decision") or row.get("action") or "?").upper()


def cmd_recent(cfg: BotConfig, n: int = 10) -> str:
    rows = _tail_ledger(cfg.ledger_path)
    if not rows:
        return f"ledger empty or unreadable at {cfg.ledger_path}"
    out = [f"LAST {min(n, len(rows))} DECISIONS (of {len(rows)} recent rows)"]
    for r in rows[-n:]:
        ts = str(r.get("ts") or r.get("time") or "")[:16]
        out.append(f"{ts}  {_kind(r):<10} {str(r.get('mechanism', ''))[:28]}")
    return "\n".join(out)


def cmd_refusals(cfg: BotConfig, n: int = 10) -> str:
    """The false-negative ledger. The charter calls these the point, so they get
    a first-class command rather than living only in a nightly report."""
    rows = [r for r in _tail_ledger(cfg.ledger_path)
            if _kind(r) in ("NO_SETUP", "REFUSED", "REFUSAL", "VETO", "BLOCKED")]
    if not rows:
        return "no refusals in the recent ledger."
    out = [f"LAST {min(n, len(rows))} REFUSALS"]
    for r in rows[-n:]:
        ts = str(r.get("ts") or r.get("time") or "")[:16]
        why = str(r.get("reason") or r.get("why") or r.get("note") or "")[:60]
        fwd = r.get("forward_r")
        cost = f"  forgone {fwd:+.2f}R" if isinstance(fwd, (int, float)) else ""
        out.append(f"{ts}  {why}{cost}")
    return "\n".join(out)


def cmd_pnl(cfg: BotConfig) -> str:
    """Realised R over the resolved rows. Deliberately NOT money: the desk is
    advisory, it does not know your size, and a currency figure here would be a
    fabrication dressed as a fact."""
    rows = [r for r in _tail_ledger(cfg.ledger_path, n=5000)
            if isinstance(r.get("realised_r"), (int, float))]
    if not rows:
        return ("no resolved outcomes yet. R accrues as signals resolve forward; "
                "unresolved signals are deliberately not counted.")
    rs = [float(r["realised_r"]) for r in rows]
    wins = [x for x in rs if x > 0]
    tot = sum(rs)
    shadow = sum(1 for r in rows if r.get("shadow"))
    return "\n".join([
        f"RESOLVED SIGNALS   {len(rs)}   ({shadow} shadow)",
        f"total              {tot:+.2f}R",
        f"mean               {tot / len(rs):+.3f}R",
        f"hit rate           {len(wins)}/{len(rs)} = {100 * len(wins) / len(rs):.0f}%",
        f"best / worst       {max(rs):+.2f}R / {min(rs):+.2f}R",
        "",
        "R, not currency: an advisory desk does not know your size.",
    ])


def cmd_growth(cfg: BotConfig) -> str:
    """The derived risk fraction and heat budget, solved from the live ledger.

    Read-only like everything else here: it reports what the evidence currently
    supports. Nothing in this process sizes anything — the desk is advisory and
    the operator places every order by hand.
    """
    rows = _tail_ledger(cfg.ledger_path, n=20000)
    rs = [float(r["realised_r"]) for r in rows
          if isinstance(r.get("realised_r"), (int, float))]
    if not rs:
        return ("no resolved R-multiples yet, so no size is supported.\n"
                "That is not a zero-risk book — it is a book nobody has watched "
                "long enough to solve a size from.")
    from golddesk.growth import recommend
    return recommend(rs, rows=rows).render()


def cmd_why(cfg: BotConfig) -> str:
    """The reasoning behind the most recent decision, whatever kind it was."""
    rows = _tail_ledger(cfg.ledger_path)
    if not rows:
        return "ledger empty."
    r = rows[-1]
    parts = [f"MOST RECENT DECISION — {_kind(r)}",
             f"at {r.get('ts') or r.get('time')}"]
    for k in ("mechanism", "reason", "why", "note", "narrative", "confidence",
              "regime", "entry", "stop", "tp1", "rr", "expected_value"):
        if r.get(k) not in (None, ""):
            parts.append(f"{k}: {r[k]}")
    return "\n".join(parts)


def cmd_halt(cfg: BotConfig) -> str:
    cfg.halt_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.halt_path.write_text(
        f"halted via telegram at {datetime.now(timezone.utc).isoformat()}\n")
    return ("HALT SET. The desk stands down at its next check.\n"
            "It does NOT close anything — Aurum has never had a position to "
            "close; whatever is open is open in YOUR terminal and is untouched.\n"
            "/resume to clear.")


def cmd_resume(cfg: BotConfig) -> str:
    if not cfg.halt_path.exists():
        return "not halted — nothing to clear."
    cfg.halt_path.unlink()
    return "HALT CLEARED. The desk resumes at its next check."


def cmd_help(cfg: BotConfig) -> str:
    return "\n".join([
        "AURUM — read-only, plus halt.",
        "",
        "/status      alive, halted, counters",
        "/positions   the open position in the checkpoint",
        "/recent      last 10 decisions",
        "/refusals    last 10 refusals and what they cost",
        "/pnl         resolved R, hit rate",
        "/growth      risk per trade and heat, derived from the ledger",
        "/why         the reasoning behind the last decision",
        "/halt        ask the desk to stand down",
        "/resume      clear the halt",
        "/help        this list",
        "",
        "There is no order command. This desk cannot trade — by construction,",
        "not by configuration.",
    ])


#: The whitelist. Exact strings, matched after lowercasing and stripping the
#: @botname suffix Telegram appends in groups. A command not in this dict is
#: refused before anything is dispatched, so adding a capability is a deliberate
#: edit here rather than an emergent property of string matching.
COMMANDS: dict[str, Callable[[BotConfig], str]] = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/status": cmd_status,
    "/positions": cmd_positions,
    "/recent": cmd_recent,
    "/refusals": cmd_refusals,
    "/pnl": cmd_pnl,
    "/growth": cmd_growth,
    "/why": cmd_why,
    "/halt": cmd_halt,
    "/resume": cmd_resume,
}


def normalise(text: str) -> str:
    """First token, lowercased, @botname stripped.

    Telegram delivers `/status@aurum_bot` in groups, and users type trailing
    arguments and stray capitals. Everything past the first token is DISCARDED
    rather than parsed — no command here takes free text, and the surest way to
    keep it that way is to never carry it.
    """
    tok = (text or "").strip().split()[:1]
    if not tok:
        return ""
    return tok[0].split("@")[0].lower()


def dispatch(cfg: BotConfig, text: str) -> Optional[str]:
    """Map a message to an answer. None means "say nothing at all".

    Silence rather than "unknown command" for non-commands: the authorised chat
    is also where notifications land, and echoing an error at every stray
    message makes the channel unusable for its primary job.
    """
    cmd = normalise(text)
    if not cmd.startswith("/"):
        return None
    fn = COMMANDS.get(cmd)
    if fn is None:
        return f"unknown command {cmd} — /help for the list."
    try:
        return fn(cfg)
    except Exception as e:                           # noqa: BLE001
        log.exception("command %s failed", cmd)
        # The operator gets the failure rather than silence: a command that
        # quietly does nothing is indistinguishable from a dead bot, and this
        # channel exists precisely to answer "is it dead".
        return f"{cmd} failed: {type(e).__name__}: {e}"


# ----------------------------------------------------------------- the loop

@dataclass
class Bot:
    cfg: BotConfig
    offset: int = 0
    handled: int = 0
    rejected: int = 0
    _stop: bool = field(default=False, repr=False)

    def poll_once(self) -> int:
        """One getUpdates round. Returns the number of updates ACTED ON."""
        res = _api(self.cfg.token, "getUpdates", self.cfg.poll_timeout_s + 10,
                   offset=self.offset, timeout=self.cfg.poll_timeout_s)
        if not res:
            return 0
        acted = 0
        for upd in res:
            # ACKNOWLEDGE FIRST. The offset advances past every update, including
            # ones from unauthorised chats and ones that raise. Advancing only on
            # success means a single poison message is re-delivered forever and
            # the bot never processes anything again.
            self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != str(self.cfg.chat_id):
                # Silence, not an error. A reply confirms a live bot to whoever
                # is probing, and there is nothing to gain by answering.
                self.rejected += 1
                log.warning("ignored message from unauthorised chat %s", chat)
                continue
            reply = dispatch(self.cfg, msg.get("text", ""))
            if reply is None:
                continue
            send(self.cfg.token, self.cfg.chat_id, reply)
            self.handled += 1
            acted += 1
        return acted

    def stop(self) -> None:
        self._stop = True

    def serve_forever(self, max_polls: Optional[int] = None) -> int:
        """Blocking. `max_polls` bounds it for tests; production passes None."""
        polls = 0
        while not self._stop and (max_polls is None or polls < max_polls):
            try:
                self.poll_once()
            except Exception:                        # noqa: BLE001
                log.exception("poll failed; continuing")
                # Back off on a hard failure so a persistent error does not
                # become a hot loop against Telegram's rate limiter.
                time.sleep(5)
            polls += 1
        return self.handled


def is_halted(halt_path: Path = Path("state/HALTED")) -> bool:
    """Read by the trading loop. The desk's side of the halt contract."""
    return Path(halt_path).exists()


def build_bot(secrets_dir: Optional[Path] = Path("secrets"), **overrides) -> Optional[Bot]:
    """Bot from the same credentials the sink uses, or None if unconfigured.

    Deliberately the SAME resolver as `notify.build_sink`. Two implementations
    of "do we have Telegram credentials" is how a desk sends notifications fine
    and silently never answers a command.
    """
    from golddesk.notify import resolve_telegram
    tok, cid, _ = resolve_telegram(secrets_dir)
    if not (tok and cid):
        return None
    return Bot(BotConfig(token=tok, chat_id=cid, **overrides))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Aurum Telegram control bot (read-only + halt)")
    ap.add_argument("--secrets", default="secrets")
    ap.add_argument("--state", default="state/service_state.json")
    ap.add_argument("--ledger", default="state/ledger.jsonl")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    bot = build_bot(Path(a.secrets), state_path=Path(a.state), ledger_path=Path(a.ledger))
    if bot is None:
        print(f"no telegram credentials in {a.secrets}/ or the environment.")
        print("see deploy/env.example — the bot needs the same token the sink uses.")
        return 2
    send(bot.cfg.token, bot.cfg.chat_id, "Aurum control bot up. /help for commands.")
    if a.once:
        bot.poll_once()
        return 0
    print("polling. ctrl-c to stop.")
    bot.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
