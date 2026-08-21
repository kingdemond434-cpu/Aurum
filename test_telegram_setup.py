"""The setup path is exercised against a stub Telegram, end to end.

WHY A STUB AND NOT A MOCK

The failure this whole file exists to prevent is a delivery channel that passes
its tests and delivers nothing. Patching `TelegramSink.send` to return True
would test that the script calls a function; it would not test the URL it
builds, the JSON it posts, the chat id it discovered, or whether `build_sink`
reads back what `write_secrets` wrote. So the stub is a real HTTP server
speaking real Bot API shapes, and every assertion below is about what actually
arrived at it.

The loopback restriction in notify.api_base is what makes this legitimate: the
override that points the sink here cannot point it anywhere off the machine.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

requests = pytest.importorskip("requests")

from deploy import telegram_setup as ts                             # noqa: E402
from golddesk import notify                                        # noqa: E402

# A real Telegram secret is exactly 35 characters -- confirmed against a live
# token the user pasted into chat (fake/placeholder, but genuine BotFather
# shape). normalise_token anchors extraction on that exact length, so the
# fixture has to be real-shaped or it tests a format Telegram doesn't issue.
GOOD = "123456789:AAquitedefinitelyafaketoken00000000"


class _State:
    """What the stub should pretend, and what it saw."""
    def __init__(self):
        self.updates: list[dict] = []
        self.webhook: str = ""
        self.token_ok: bool = True
        self.sent: list[dict] = []
        self.paths: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *a):                       # silence the test output
        pass

    def _reply(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _route(self, payload: dict | None):
        st = self.state
        path = urlparse(self.path).path
        st.paths.append(path)
        parts = path.strip("/").split("/")
        if len(parts) != 2 or not parts[0].startswith("bot"):
            return self._reply(404, {"ok": False})
        token, method = parts[0][3:], parts[1]
        if not st.token_ok or token != GOOD:
            return self._reply(401, {"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return self._reply(200, {"ok": True, "result": {
                "id": 123456789, "username": "aurum_test_bot", "is_bot": True}})
        if method == "getWebhookInfo":
            return self._reply(200, {"ok": True, "result": {"url": st.webhook}})
        if method == "getUpdates":
            return self._reply(200, {"ok": True, "result": st.updates})
        if method == "sendMessage":
            st.sent.append(payload or {})
            return self._reply(200, {"ok": True, "result": {"message_id": 1}})
        return self._reply(404, {"ok": False, "description": "no such method"})

    def do_GET(self):
        self._route(None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        self._route(json.loads(raw or b"{}"))


def _update(chat_id: int, who: str = "operator") -> dict:
    return {"update_id": chat_id, "message": {
        "message_id": 1, "text": "hello",
        "chat": {"id": chat_id, "type": "private", "username": who}}}


@pytest.fixture
def telegram(monkeypatch):
    state = _State()
    handler = type("H", (_Handler,), {"state": state})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("TELEGRAM_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    try:
        yield state
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------- the guard rail

def test_a_non_loopback_api_override_is_ignored(monkeypatch):
    """The override must not be usable to send a bot token to another host."""
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://evil.example.com")
    assert notify.api_base() == notify.DEFAULT_API_BASE


def test_a_loopback_override_is_honoured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_BASE", "http://127.0.0.1:9999/")
    assert notify.api_base() == "http://127.0.0.1:9999"


def test_no_override_means_the_real_api(monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    assert notify.api_base() == "https://api.telegram.org"


# ------------------------------------------------------------ the happy path

def test_setup_discovers_the_chat_writes_secrets_and_delivers(telegram, tmp_path):
    """One command, and a message actually arrives at the far end."""
    telegram.updates = [_update(555444333)]
    rc = ts.main(["--secrets", str(tmp_path / "secrets"), "--token", GOOD])
    assert rc == 0

    sec = tmp_path / "secrets"
    assert (sec / "telegram_token").read_text(encoding='utf-8').strip() == GOOD
    assert (sec / "telegram_chat_id").read_text(encoding='utf-8').strip() == "555444333"

    # The message went out, to the discovered chat, through the real sink.
    assert len(telegram.sent) == 1
    assert telegram.sent[0]["chat_id"] == "555444333"
    assert "Aurum" in telegram.sent[0]["text"]
    # ...and it was the PRODUCTION resolver that found the credentials.
    tok, cid, _ = notify.resolve_telegram(sec)
    assert (tok, cid) == (GOOD, "555444333")


def test_the_delivery_test_uses_the_desks_own_sink(telegram, tmp_path):
    """`build_sink` on the written directory must reach the same stub.

    This is the assertion that would have caught a setup script that verified
    with its own private send() and left build_sink broken.
    """
    telegram.updates = [_update(42)]
    ts.main(["--secrets", str(tmp_path / "s"), "--token", GOOD])
    telegram.sent.clear()
    sink = notify.build_sink(tmp_path / "s")
    assert sink.send("second") is True
    assert telegram.sent[-1]["chat_id"] == "42"


def test_secrets_are_not_world_readable(telegram, tmp_path):
    telegram.updates = [_update(7)]
    sec = tmp_path / "secrets"
    ts.main(["--secrets", str(sec), "--token", GOOD])
    assert stat.S_IMODE(os.stat(sec).st_mode) == 0o700
    for f in ("telegram_token", "telegram_chat_id"):
        assert stat.S_IMODE(os.stat(sec / f).st_mode) == 0o600, f


# ----------------------------------------------------------- the refusals

def test_a_revoked_token_fails_before_anything_is_written(telegram, tmp_path):
    telegram.token_ok = False
    sec = tmp_path / "secrets"
    assert ts.main(["--secrets", str(sec), "--token", GOOD]) == 1
    assert not (sec / "telegram_token").exists()


def test_no_messages_yet_is_a_refusal_not_an_empty_chat_id(telegram, tmp_path):
    """Writing an empty chat id here is the silent-failure mode itself."""
    telegram.updates = []
    sec = tmp_path / "secrets"
    assert ts.main(["--secrets", str(sec), "--token", GOOD]) == 1
    assert not (sec / "telegram_chat_id").exists()
    assert not telegram.sent


def test_two_chats_refuses_rather_than_picking_one(telegram, tmp_path):
    telegram.updates = [_update(111, "you"), _update(222, "someone_else")]
    sec = tmp_path / "secrets"
    assert ts.main(["--secrets", str(sec), "--token", GOOD]) == 1
    assert not (sec / "telegram_chat_id").exists()


def test_an_explicit_chat_id_settles_the_ambiguity(telegram, tmp_path):
    telegram.updates = [_update(111), _update(222)]
    sec = tmp_path / "secrets"
    assert ts.main(["--secrets", str(sec), "--token", GOOD,
                    "--chat-id", "222"]) == 0
    assert (sec / "telegram_chat_id").read_text(encoding='utf-8').strip() == "222"
    assert telegram.sent[0]["chat_id"] == "222"


def test_a_registered_webhook_is_reported_not_worked_around(telegram, tmp_path):
    """getUpdates would return empty forever; that needs saying, not retrying."""
    telegram.webhook = "https://someone-elses-server.example/hook"
    telegram.updates = []
    sec = tmp_path / "secrets"
    assert ts.main(["--secrets", str(sec), "--token", GOOD]) == 1
    assert not (sec / "telegram_token").exists()


def test_discovery_does_not_consume_pending_updates(telegram, tmp_path):
    """No `offset` param: a running bot must not lose its queue to setup."""
    telegram.updates = [_update(9)]
    ts.main(["--secrets", str(tmp_path / "s"), "--token", GOOD])
    got = [p for p in telegram.paths if p.endswith("getUpdates")]
    assert got, "getUpdates was never called"
    assert all("offset" not in p for p in got)


def test_dry_run_verifies_but_writes_nothing(telegram, tmp_path):
    telegram.updates = [_update(9)]
    sec = tmp_path / "secrets"
    assert ts.main(["--secrets", str(sec), "--token", GOOD, "--dry-run"]) == 0
    assert not (sec / "telegram_token").exists()
    assert not telegram.sent


def test_the_token_is_never_printed_in_full(capsys, telegram, tmp_path):
    """This script's output is what an operator pastes when asking for help."""
    telegram.updates = [_update(9)]
    ts.main(["--secrets", str(tmp_path / "s"), "--token", GOOD])
    out = capsys.readouterr()
    assert GOOD not in out.out and GOOD not in out.err
    assert "123456789:…" in out.out


def test_mask_shows_no_secret_material_but_still_distinguishes():
    """The bot id identifies the token; the secret half must not appear."""
    a = ts.mask("111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    b = ts.mask("222222:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
    assert a != b
    assert "A" not in a and "B" not in b
    assert ts.mask("not-a-token") == "…(malformed)"


# ------------------------------------------------- paste artefacts

def test_a_space_after_the_colon_is_removed():
    """The exact shape a token takes when copied out of a chat window.

    `.strip()` does not touch it, so the token reached Telegram verbatim and
    came back 401 -- and a 401 reads as "revoked", which sends the operator to
    BotFather for a replacement that will have a space in it too.
    """
    assert ts.normalise_token("123456789: AAquitedefinitelyafaketokenforatest0") \
        == "123456789:AAquitedefinitelyafaketokenforatest0"


def test_a_wrapped_token_with_a_newline_is_repaired():
    assert ts.normalise_token("123456789:AAquitedefin\nitelyafaketokenforatest0") \
        == "123456789:AAquitedefinitelyafaketokenforatest0"


def test_tabs_and_repeated_spaces_too():
    assert ts.normalise_token("  123456789 :\tAAquitedefinitely afaketokenforatest0 ") \
        == "123456789:AAquitedefinitelyafaketokenforatest0"


@pytest.mark.parametrize("bad", ["not-a-token", "12345", "8911147517:short",
                                 "abc:AAquitedefinitelyafaketokenforatest0"])
def test_a_malformed_token_is_refused_before_any_network_call(bad):
    with pytest.raises(ValueError, match="does not look like a bot token"):
        ts.normalise_token(bad)


def test_the_refusal_message_carries_no_secret_material():
    try:
        ts.normalise_token("8911147517:short")
    except ValueError as e:
        assert "short" not in str(e) and "8911147517:…" in str(e)


def test_a_malformed_token_exits_2_rather_than_tracebacking(tmp_path, capsys):
    rc = ts.main(["--secrets", str(tmp_path / "s"), "--token", "rubbish"])
    assert rc == 2
    assert "does not look like a bot token" in capsys.readouterr().err


def test_the_normaliser_runs_on_every_input_route(telegram, tmp_path):
    """stdin, --token and --token-file must all be repaired, not just one.

    Derived from GOOD rather than a second hardcoded string: an earlier
    version duplicated the literal here, and when GOOD's secret length was
    corrected to match real Telegram tokens this copy was not, and silently
    tested a token shape that no longer existed anywhere else in the suite.
    """
    bot_id, _, secret = GOOD.partition(":")
    spaced = f"{bot_id}: {secret}"
    f = tmp_path / "tok"; f.write_text(spaced + "\n")
    telegram.updates = [_update(31)]
    assert ts.main(["--secrets", str(tmp_path / "s"), "--token-file", str(f)]) == 0
    assert (tmp_path / "s" / "telegram_token").read_text(encoding='utf-8').strip() == GOOD


def test_the_fixture_itself_is_real_shaped():
    """If GOOD's secret is not 35 chars, every extraction test below is
    exercising a format Telegram does not issue, and would pass for the wrong
    reason -- this caught exactly that the first time GOOD was chosen."""
    assert len(GOOD.partition(":")[2]) == 35


# --------------------------------------------------- botfather's own wording

BOTFATHER_MSG = (
    "Done! Congratulations on your new bot. You will find it at "
    "t.me/aurum_signals_bot. You can now add a description, about "
    "section and profile picture for your bot, see /help for a list of "
    "commands.\n\nUse this token to access the HTTP API:\n"
    f"{GOOD}\n"
    "Keep your token secure and store it safely, it can be used by anyone "
    "to control your bot.")


def test_the_full_botfather_message_extracts_cleanly(telegram, tmp_path):
    """The exact text BotFather sends, pasted whole, must still work.

    This is the case that broke the naive fix: stripping whitespace first
    glues the trailing sentence onto the token, and a greedy character class
    then swallows it -- '...XP78Keepyourtokensecure' -- which reaches Telegram
    and comes back 401. Confirmed against the running stub, not just the regex.
    """
    telegram.updates = [_update(9001)]
    rc = ts.main(["--secrets", str(tmp_path / "s"), "--token", BOTFATHER_MSG])
    assert rc == 0
    assert (tmp_path / "s" / "telegram_token").read_text(encoding='utf-8').strip() == GOOD


def test_normalise_token_extracts_from_botfathers_wording():
    assert ts.normalise_token(BOTFATHER_MSG) == GOOD


def test_a_token_wrapped_mid_secret_by_a_narrow_terminal_is_repaired():
    wrapped = GOOD[:40] + "\n" + GOOD[40:]
    assert ts.normalise_token(wrapped) == GOOD


def test_two_token_shaped_strings_in_one_paste_is_refused():
    other = "222222222:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    with pytest.raises(ValueError, match="found 2 different token-shaped"):
        ts.normalise_token(f"{GOOD}\n{other}")


def test_quote_characters_from_a_shell_example_are_stripped():
    """cmd.exe does not treat ' or " as quoting -- it passes them through
    literally, so following a Unix-shell example (single quotes around the
    token) embeds real quote characters in the input."""
    assert ts.normalise_token("'8911147517:AAquitedefinitelyafaketokenforatest0'") \
        == "8911147517:AAquitedefinitelyafaketokenforatest0"
    assert ts.normalise_token('"8911147517:AAquitedefinitelyafaketokenforatest0"') \
        == "8911147517:AAquitedefinitelyafaketokenforatest0"


def test_the_full_botfather_message_is_extracted():
    """The whole 'Use this token...Keep your token secure' paste, not just the line."""
    msg = ("Use this token to access the HTTP API:\n"
          "8911147517: AAEnCsu_fmpox-Nx_P09LdcD2JNOYz_XP78\n"
          "Keep your token secure and store it safely, it can be used by "
          "anyone to control your bot.")
    assert ts.normalise_token(msg) == \
        "8911147517:AAEnCsu_fmpox-Nx_P09LdcD2JNOYz_XP78"
