"""Telegram sink — optional at runtime, never a hard dependency.

If requests is missing, or no token is configured, or the send fails, the desk
keeps trading and the message is journalled instead. A notification channel
must never be able to halt the trading loop.
"""
from __future__ import annotations

import json
import logging
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


def build_sink(secrets_dir: Optional[Path], shadow_log: Optional[Path] = None) -> Sink:
    """Resolve a sink from secrets/, falling back to shadow file, then null."""
    if secrets_dir:
        tok = Path(secrets_dir) / "telegram_token"
        cid = Path(secrets_dir) / "telegram_chat_id"
        if tok.exists() and cid.exists():
            return TelegramSink(tok.read_text().strip(), cid.read_text().strip())
    if shadow_log:
        return FileSink(shadow_log)
    return NullSink()
