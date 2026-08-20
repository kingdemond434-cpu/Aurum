#!/usr/bin/env python3
"""Turn a bot token into a working, PROVEN delivery channel in one command.

    python3 deploy/telegram_setup.py            # prompts for the token
    echo "<token>" | python3 deploy/telegram_setup.py --stdin

WHAT WAS ACTUALLY MISSING

Everything that sends a message already existed: notify.build_sink resolves the
credentials, TelegramSink posts them, HealthTrackingSink notices when it stops
working. What did not exist was any way to GET the credentials into place
without the operator personally looking up a numeric chat id in a browser and
hand-editing two files. That lookup is the whole friction, it is entirely
mechanical, and a mechanical step done by hand is a step done wrong at 3am.
So this does it: verify the token, discover the chat, write the files, and then
prove delivery.

THE VERIFICATION IS THE POINT, NOT THE FILE WRITING

    getMe          the token is real and not revoked
    getWebhookInfo nothing else has claimed this bot's updates
    getUpdates     find the chat id from a message the user actually sent
    build_sink     re-read what we just wrote, THROUGH THE PRODUCTION RESOLVER
    send           deliver a real message and require it to arrive

The last two matter most. A setup script that writes two files and declares
success has tested that Python can write files. Aurum places no orders — the
message IS the deliverable — so the only acceptable definition of "set up" is
that a message arrived, sent by the same code path the desk itself will use.
Writing the files and then verifying via a private helper would prove the
helper works; it is `build_sink` that has to work.

WHY IT REFUSES INSTEAD OF GUESSING

Two chats in getUpdates means two people have messaged the bot, and picking
one silently is how signals end up going to a stranger. No updates at all is
not an error to retry through, it is a missing human action with an exact
remedy. Each dead end below names the remedy rather than the exception.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from golddesk.notify import api_base, build_sink, resolve_telegram  # noqa: E402


def mask(token: str) -> str:
    """Enough to tell two tokens apart, and NO secret material at all.

    A Telegram token is `<bot_id>:<secret>`. The bot id is not a secret — getMe
    prints it, it is the bot's public identity — and it already distinguishes
    any two tokens, so nothing is gained by also showing a tail of the secret
    half and four characters of a 35-character secret is four more than zero.
    This script's output is what an operator pastes into a chat when it does
    not work, so it must be safe to paste.
    """
    head, sep, _ = token.partition(":")
    return f"{head}:…" if sep and head.isdigit() else "…(malformed)"


def call(token: str, method: str, timeout: float = 15.0) -> tuple[Optional[dict], str]:
    """One Telegram GET. Returns (result, human-readable failure)."""
    try:
        import requests
    except ImportError:
        return None, ("the `requests` package is not installed in this "
                      "interpreter. Use the desk's venv: "
                      "/opt/aurum/.venv/bin/python deploy/telegram_setup.py")
    url = f"{api_base()}/bot{token}/{method}"
    try:
        r = requests.get(url, timeout=timeout)
    except Exception as e:                                   # noqa: BLE001
        return None, (f"could not reach {api_base()} ({e}). This box has no "
                      f"route to Telegram — check egress/DNS from the VPS "
                      f"itself with: curl -sS {api_base()}/")
    if r.status_code == 401:
        return None, ("Telegram rejected the token (401 Unauthorized). It is "
                      "revoked, mistyped, or from a different bot. Get a fresh "
                      "one from @BotFather with /token.")
    if r.status_code != 200:
        return None, f"{method} returned HTTP {r.status_code}: {r.text[:200]}"
    try:
        body = r.json()
    except Exception:                                        # noqa: BLE001
        return None, f"{method} returned non-JSON: {r.text[:200]}"
    if not body.get("ok"):
        return None, f"{method} not ok: {str(body)[:200]}"
    return body.get("result"), ""


#: `<bot_id>:<secret>`. The secret half is base64url-ish and ~35 chars, but the
#: length is not guaranteed by Telegram, so only the SHAPE is enforced here.
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


def normalise_token(raw: str) -> str:
    """Strip every kind of whitespace, then check the shape before spending a call.

    WHY INTERNAL WHITESPACE, NOT JUST THE ENDS. A token copied out of a chat
    window arrives as `8911147517: AAEn...` often enough that it is the normal
    case, not the edge case: phone keyboards and message renderers insert a
    space after the colon, and terminals wrap long lines. `.strip()` removes
    neither. The token then goes to Telegram verbatim, comes back 401
    Unauthorized, and the operator concludes the token is revoked and goes to
    BotFather for a new one -- which will also have a space in it.

    So whitespace anywhere is removed, and the result is shape-checked. A
    malformed token is caught here, in front of the user, instead of arriving
    as a generic auth failure from a remote API.
    """
    cleaned = "".join(raw.split())
    if not cleaned:
        return ""
    if not _TOKEN_RE.match(cleaned):
        raise ValueError(
            f"that does not look like a bot token: {mask(cleaned)}\n"
            f"  expected <digits>:<letters/digits/_/-> as one unbroken string\n"
            f"  got {len(cleaned)} characters after removing whitespace\n"
            f"  BotFather gives it to you on one line; copy the whole line.")
    if cleaned != raw.strip():
        print("note: removed whitespace from inside the token (a paste "
              "artefact, not a problem with the token itself)", file=sys.stderr)
    return cleaned


def read_token(args) -> str:
    if args.token_file:
        return normalise_token(Path(args.token_file).read_text("utf-8"))
    if args.stdin:
        return normalise_token(sys.stdin.read())
    if args.token:
        print("!! --token puts the token in your shell history and in `ps`.\n"
              "   Prefer piping it:  echo '<token>' | %s --stdin\n" % sys.argv[0],
              file=sys.stderr)
        return normalise_token(args.token)
    if not sys.stdin.isatty():
        return normalise_token(sys.stdin.read())
    # getpass rather than input(): the token does not belong on screen behind
    # someone, and this is normally run over a phone SSH session.
    return normalise_token(getpass.getpass("BotFather token (not echoed): "))


def discover_chat(token: str, want: Optional[str]) -> tuple[Optional[str], str]:
    """The chat id to send to, or a failure that says what to do about it."""
    if want:
        return want, ""

    hook, err = call(token, "getWebhookInfo")
    if hook is None:
        return None, err
    if hook.get("url"):
        return None, (
            f"this bot has a webhook registered at {hook['url']!r}, so Telegram "
            f"delivers updates there and getUpdates will always be empty. If "
            f"that webhook is not yours, the token is shared with something "
            f"else and you should /revoke it. To take the bot back:\n"
            f"    curl -sS \"{api_base()}/bot<token>/deleteWebhook\"")

    # No `offset`: this must not consume updates the running bot needs.
    updates, err = call(token, "getUpdates", timeout=20.0)
    if updates is None:
        return None, err

    chats: dict[str, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or \
            u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        who = chat.get("username") or chat.get("title") or \
            " ".join(filter(None, (chat.get("first_name"), chat.get("last_name")))) \
            or chat.get("type", "?")
        chats[str(cid)] = who

    if not chats:
        return None, (
            "Telegram has no messages for this bot, so there is no chat to "
            "send to. This is the one step only you can do:\n"
            "    1. open Telegram and find the bot (its @name is printed above)\n"
            "    2. press Start, or just send it the word  hello\n"
            "    3. re-run this command\n"
            "If you did message it and this still says none: the desk's bot "
            "service may already be running and consuming updates. Stop it "
            "first — sudo systemctl stop aurum-bot — then re-run.")
    if len(chats) > 1:
        listing = "\n".join(f"      {c}  ({w})" for c, w in sorted(chats.items()))
        return None, (
            f"{len(chats)} different chats have messaged this bot. Refusing to "
            f"pick one: the wrong choice sends every signal this desk produces "
            f"to a stranger, silently and forever. Re-run naming the one you "
            f"want:\n{listing}\n"
            f"    python3 deploy/telegram_setup.py --chat-id <id>")
    cid, who = next(iter(chats.items()))
    return cid, f"discovered chat {cid} ({who})"


def write_secrets(secrets: Path, token: str, chat_id: str) -> None:
    """0700 dir, 0600 files, written before anything is verified against them.

    Order matters: the files are written FIRST and then the delivery test reads
    them back through the production resolver. Verifying in memory and writing
    afterwards would leave the passing test and the deployed state as two
    different things.
    """
    secrets.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets, stat.S_IRWXU)
    for name, value in (("telegram_token", token), ("telegram_chat_id", chat_id)):
        p = secrets / name
        p.write_text(value + "\n", "utf-8")
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify a Telegram bot token, find the chat, write the "
                    "secrets, and prove a message arrives.")
    ap.add_argument("--secrets", default="secrets", type=Path,
                    help="secrets directory (default: secrets/)")
    ap.add_argument("--chat-id", default=None,
                    help="skip discovery and use this chat id")
    ap.add_argument("--token", default=None, help="token on the command line "
                                                  "(discouraged — see --stdin)")
    ap.add_argument("--token-file", default=None, help="read the token from a file")
    ap.add_argument("--stdin", action="store_true", help="read the token from stdin")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify and report, but write nothing")
    a = ap.parse_args(argv)

    existing_tok, existing_cid, where = resolve_telegram(a.secrets)
    try:
        token = read_token(a)
    except ValueError as e:
        # A shape error is the operator's typo, not a crash. Print it as advice.
        print(f"\n{e}", file=sys.stderr)
        return 2
    if not token and existing_tok:
        token = existing_tok
        print(f"no token given; re-verifying the one already in {where}")
    if not token:
        print("no token supplied and none configured.", file=sys.stderr)
        return 2

    print(f"token {mask(token)} -> {api_base()}")

    me, err = call(token, "getMe")
    if me is None:
        print(f"\nFAILED at getMe: {err}", file=sys.stderr)
        return 1
    name = me.get("username") or me.get("first_name") or "?"
    print(f"  getMe ok: @{name} (id {me.get('id')})")

    chat_id, note = discover_chat(token, a.chat_id)
    if chat_id is None:
        print(f"\nFAILED: {note}", file=sys.stderr)
        return 1
    print(f"  {note or f'using chat {chat_id}'}")
    if existing_cid and existing_cid != chat_id:
        print(f"  NOTE: this replaces the previously configured chat "
              f"{existing_cid}; signals will stop going there.")

    if a.dry_run:
        print(f"\ndry run — would write {a.secrets}/telegram_token and "
              f"{a.secrets}/telegram_chat_id ({chat_id}). Nothing written.")
        return 0

    write_secrets(a.secrets, token, chat_id)
    print(f"  wrote {a.secrets}/telegram_token and {a.secrets}/telegram_chat_id "
          f"(0600)")

    # The real test: resolve through the same function the desk calls, and send
    # through the same sink. Anything less proves only that this script works.
    sink = build_sink(a.secrets)
    ok = sink.send("Aurum: Telegram channel configured. This message was sent "
                   "by the desk's own notification sink, so signals will "
                   "arrive here. Send /help to the bot for control commands.")
    stats = sink.stats() if hasattr(sink, "stats") else {}
    if not ok:
        print(f"\nFAILED: credentials are written and the token is valid, but "
              f"the desk's own sink could not deliver. sink={stats}\n"
              f"Most likely the bot has never been started by this chat, or "
              f"the chat id belongs to a group the bot was removed from.",
              file=sys.stderr)
        return 1
    print(f"  DELIVERED — check Telegram. sink={stats.get('sink')}, "
          f"sent={stats.get('sent')}")
    print("\nDone. Next:\n"
          "  python3 run_desk.py --preflight     # gate before anything runs\n"
          "  sudo systemctl restart aurum-bot aurum-desk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
