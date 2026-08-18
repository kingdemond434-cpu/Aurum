"""Telegram sink — optional at runtime, never a hard dependency.

If requests is missing, or no token is configured, or the send fails, the desk
keeps trading and the message is journalled instead. A notification channel
must never be able to halt the trading loop.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)


class Sink(Protocol):
    def send(self, text: str) -> bool: ...


class NullSink:
    """Shadow mode default. Records nothing anywhere it could be mistaken for live."""
    def send(self, text: str) -> bool:
        log.debug("notify(null): %s", text[:120])
        return True


class FileSink:
    """Shadow mode. What WOULD have been sent, appended for later inspection."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, text: str) -> bool:
        with self.path.open("a") as fh:
            fh.write(json.dumps({"text": text}) + "\n")
        return True


class TelegramSink:
    """Live channel. Import of requests is deferred so it is never required."""
    def __init__(self, token: str, chat_id: str, timeout: float = 5.0):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout

    def send(self, text: str) -> bool:
        try:
            import requests  # deferred on purpose
        except ImportError:
            log.warning("requests not installed — telegram send skipped")
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=self.timeout)
            if r.status_code != 200:
                log.warning("telegram %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:                      # never propagate to the loop
            log.warning("telegram send failed: %s", e)
            return False


class HealthTrackingSink:
    """Wraps a sink and remembers whether it is actually delivering.

    THE DEFECT THIS CLOSES. `TelegramSink.send` returns False on failure and
    never raises — correct, because a notification channel must not be able to
    halt the trading loop — and `DeskService._notify` wrapped its call in a bare
    `except: pass`. So a wrong token, a revoked bot or a changed chat id meant
    every signal this desk produced went nowhere, forever, and nothing in the
    code, the log or the state file said so.

    For a SIGNAL desk that is not a degraded mode, it is total product failure
    wearing the appearance of a healthy process. Aurum places no orders; the
    message IS the deliverable.

    Escalation cannot be "send a notification", for obvious reasons. So this
    counts, logs at rising severity, and exposes the counters — the bot's
    /status reads them, and the bot is a separate process, so a working bot and
    a broken sink is itself a useful signal about which half is wrong.
    """

    #: Consecutive failures before the log line becomes an error rather than a
    #: warning. One failure is a network blip; five is a configuration fault.
    ALARM_AFTER = 5

    def __init__(self, inner: Sink):
        self.inner = inner
        self.sent = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.last_error_at: Optional[str] = None
        self.last_ok_at: Optional[str] = None

    @property
    def healthy(self) -> bool:
        """False once the channel has failed enough times to be a fault.

        Deliberately NOT "has never failed": a single dropped message on a flaky
        VPS is not a broken desk, and a health flag that trips on one blip is a
        flag people learn to ignore.
        """
        return self.consecutive_failures < self.ALARM_AFTER

    def send(self, text: str) -> bool:
        from datetime import datetime, timezone
        ok = False
        try:
            ok = bool(self.inner.send(text))
        except Exception as e:                       # noqa: BLE001
            # Still never propagates — the loop must survive a dead channel —
            # but it is now COUNTED rather than discarded.
            log.warning("sink raised: %s", e)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if ok:
            self.sent += 1
            self.consecutive_failures = 0
            self.last_ok_at = stamp
            return True
        self.failed += 1
        self.consecutive_failures += 1
        self.last_error_at = stamp
        n = self.consecutive_failures
        if n == self.ALARM_AFTER:
            log.error("NOTIFICATION CHANNEL IS DOWN: %d consecutive failures. "
                      "This desk's only output is the message, so it is currently "
                      "producing nothing you can see. Check the token and chat id "
                      "in secrets/, and that the bot has not been revoked.", n)
        elif n > self.ALARM_AFTER and n % 20 == 0:
            log.error("notification channel still down (%d consecutive failures, "
                      "%d delivered ever)", n, self.sent)
        elif n < self.ALARM_AFTER:
            log.warning("notification send failed (%d in a row)", n)
        return False

    def stats(self) -> dict:
        return {"sent": self.sent, "failed": self.failed,
                "consecutive_failures": self.consecutive_failures,
                "healthy": self.healthy, "last_ok_at": self.last_ok_at,
                "last_error_at": self.last_error_at,
                "sink": type(self.inner).__name__}


def probe(sink: Sink, note: str = "preflight") -> tuple[bool, str]:
    """Actually deliver a message and report whether it arrived.

    PREFLIGHT CHECKED THAT CREDENTIALS EXISTED, NOT THAT THEY WORKED. A
    non-empty token file proves somebody typed something into a file. A revoked
    bot, a wrong chat id, a bot never started by the user, and a firewall all
    pass that check and deliver nothing. The only test of a delivery channel is
    a delivery.
    """
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = sink.send(f"Aurum {note} — channel test {stamp}")
    if ok:
        return True, "a test message was DELIVERED; the channel works end to end"
    return False, ("the test message was NOT delivered. Credentials being present "
                   "is not the same as them working: a revoked bot, a wrong chat "
                   "id, or a bot the user has never pressed Start on all look "
                   "correctly configured and deliver nothing.")


def resolve_telegram(secrets_dir: Optional[Path]) -> tuple[Optional[str], Optional[str], str]:
    """Find the bot credentials. Returns (token, chat_id, where).

    ONE resolver, used by both the sink and the preflight check, because two
    implementations of "do we have Telegram credentials" is how a desk passes
    preflight and then sends signals nowhere.

    Order: files first, environment second.

    FILES ARE PREFERRED. An environment variable is visible in `systemctl show`,
    in the journal when the process crashes, and to anything that can read
    /proc/<pid>/environ. A 0600 file owned by the service account is not.

    EMPTY IS NOT PRESENT. deploy/install.sh creates these files empty so the
    operator has an obvious place to put the values, and the previous check
    tested only `.exists()` — so a fresh install passed preflight with an empty
    token and the desk ran silently. Content is what counts.
    """
    if secrets_dir:
        tok_p = Path(secrets_dir) / "telegram_token"
        cid_p = Path(secrets_dir) / "telegram_chat_id"
        if tok_p.exists() and cid_p.exists():
            tok, cid = tok_p.read_text().strip(), cid_p.read_text().strip()
            if tok and cid:
                return tok, cid, f"{secrets_dir}/"
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    cid = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if tok and cid:
        return tok, cid, "environment (files are preferred — see deploy/env.example)"
    return None, None, "not configured"


def build_sink(secrets_dir: Optional[Path], shadow_log: Optional[Path] = None,
               track_health: bool = True) -> Sink:
    """Resolve a sink from secrets/ or env, falling back to shadow file, then null.

    Health tracking is ON by default and wraps whatever was resolved. It is a
    default rather than an option because the failure it catches is silent, and
    anything you have to remember to switch on for a silent failure is a thing
    that will be off when it matters.
    """
    tok, cid, _ = resolve_telegram(secrets_dir)
    if tok and cid:
        inner: Sink = TelegramSink(tok, cid)
    elif shadow_log:
        inner = FileSink(shadow_log)
    else:
        inner = NullSink()
    return HealthTrackingSink(inner) if track_health else inner
