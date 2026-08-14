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


def build_sink(secrets_dir: Optional[Path], shadow_log: Optional[Path] = None) -> Sink:
    """Resolve a sink from secrets/ or env, falling back to shadow file, then null."""
    tok, cid, _ = resolve_telegram(secrets_dir)
    if tok and cid:
        return TelegramSink(tok, cid)
    if shadow_log:
        return FileSink(shadow_log)
    return NullSink()
