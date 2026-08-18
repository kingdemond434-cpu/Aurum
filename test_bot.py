"""The control channel is the only component that takes instructions from
outside the process, so its tests are mostly about what it REFUSES.

No network anywhere in here: `_api` is the single transport seam and every test
either avoids it or replaces it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from golddesk import bot as B


@pytest.fixture()
def desk(tmp_path: Path) -> B.BotConfig:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "service_state.json").write_text(json.dumps({
        "last_bar_ts": "2026-08-17T12:00:00+00:00",
        "started_at": "2026-08-17T09:00:00+00:00",
        "restarts": 2, "bars_processed": 41, "ticks_seen": 9001,
        "reconnects": 1, "stale_suspensions": 0, "open_trade": None,
    }))
    (tmp_path / "state" / "ledger.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-08-17T10:00:00+00:00", "kind": "NO_SETUP",
         "reason": "no alignment", "forward_r": 1.4},
        {"ts": "2026-08-17T11:00:00+00:00", "kind": "SIGNAL",
         "mechanism": "london_sweep", "realised_r": 1.8, "shadow": True},
        {"ts": "2026-08-17T12:00:00+00:00", "kind": "SIGNAL",
         "mechanism": "fvg_retest", "realised_r": -1.0, "shadow": True,
         "reason": "stopped"},
    ]) + "\n")
    return B.BotConfig(token="T", chat_id="42",
                       state_path=tmp_path / "state" / "service_state.json",
                       ledger_path=tmp_path / "state" / "ledger.jsonl",
                       halt_path=tmp_path / "state" / "HALTED")


# ------------------------------------------------------- authorisation

def test_a_foreign_chat_is_ignored_entirely(desk, monkeypatch):
    """THE TEST THAT MATTERS MOST. getUpdates delivers messages from anyone who
    finds the bot; only the configured chat may be obeyed."""
    sent: list = []
    monkeypatch.setattr(B, "_api", lambda *a, **k: [
        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/halt"}}])
    monkeypatch.setattr(B, "send", lambda *a, **k: sent.append(a) or True)
    b = B.Bot(desk)
    assert b.poll_once() == 0
    assert sent == [], "replied to an unauthorised chat"
    assert not desk.halt_path.exists(), "an unauthorised chat halted the desk"
    assert b.rejected == 1


def test_the_authorised_chat_is_obeyed(desk, monkeypatch):
    monkeypatch.setattr(B, "_api", lambda *a, **k: [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/status"}}])
    monkeypatch.setattr(B, "send", lambda *a, **k: True)
    assert B.Bot(desk).poll_once() == 1


def test_chat_id_compares_as_string_not_int(desk, monkeypatch):
    """Telegram sends the id as a number; the config holds a string read from a
    file. An == across those types is False and locks the owner out."""
    monkeypatch.setattr(B, "_api", lambda *a, **k: [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/status"}}])
    monkeypatch.setattr(B, "send", lambda *a, **k: True)
    assert B.Bot(B.BotConfig(token="T", chat_id="42", state_path=desk.state_path,
                             ledger_path=desk.ledger_path,
                             halt_path=desk.halt_path)).poll_once() == 1


# ------------------------------------------------------------ the offset

def test_the_offset_advances_past_a_rejected_update(desk, monkeypatch):
    """Advancing only on success re-delivers a poison message forever, and the
    bot never processes anything again."""
    monkeypatch.setattr(B, "_api", lambda *a, **k: [
        {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/halt"}}])
    b = B.Bot(desk)
    b.poll_once()
    assert b.offset == 8


def test_the_offset_advances_past_a_command_that_raised(desk, monkeypatch):
    monkeypatch.setattr(B, "_api", lambda *a, **k: [
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status"}}])
    monkeypatch.setattr(B, "send", lambda *a, **k: True)
    monkeypatch.setitem(B.COMMANDS, "/status",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    b = B.Bot(desk)
    b.poll_once()
    assert b.offset == 4


def test_a_failing_command_answers_rather_than_going_quiet(desk, monkeypatch):
    monkeypatch.setitem(B.COMMANDS, "/status",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    out = B.dispatch(desk, "/status")
    assert "failed" in out and "boom" in out


# ------------------------------------------------------------- the whitelist

def test_an_unknown_command_is_not_dispatched(desk):
    assert "unknown command" in B.dispatch(desk, "/exec")


def test_plain_chatter_is_answered_with_silence(desk):
    """This chat is also where notifications land. Erroring at every stray
    message makes the channel unusable for its primary job."""
    assert B.dispatch(desk, "morning") is None
    assert B.dispatch(desk, "") is None


def test_the_botname_suffix_is_stripped(desk):
    assert B.normalise("/status@aurum_desk_bot") == "/status"


def test_arguments_are_discarded_not_parsed(desk):
    """No command takes free text, and the way to keep it that way is to never
    carry it past normalise()."""
    assert B.normalise("/status ../../etc/passwd") == "/status"


def test_no_command_can_place_an_order():
    """The charter property, asserted against the command table itself rather
    than against a claim in a docstring."""
    import ast
    src = Path(B.__file__).read_text()
    banned = {"order_send", "order_check", "positions_modify", "eval", "exec",
              "system", "popen", "check_output"}
    hits = [f"{n.lineno}:{n.attr}" for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Attribute) and n.attr in banned]
    hits += [f"{n.lineno}:{n.id}" for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Name) and n.id in banned]
    assert not hits, f"bot.py can reach {hits}"


# ---------------------------------------------------------------- the halt

def test_halt_sets_and_resume_clears(desk):
    assert not B.is_halted(desk.halt_path)
    B.cmd_halt(desk)
    assert B.is_halted(desk.halt_path)
    B.cmd_resume(desk)
    assert not B.is_halted(desk.halt_path)


def test_halt_says_it_closes_nothing(desk):
    """An advisory desk holds no position. A halt message implying otherwise
    would have the operator believe something was flattened that was not."""
    assert "does NOT close" in B.cmd_halt(desk)


def test_resume_on_a_clear_desk_is_not_an_error(desk):
    assert "nothing to clear" in B.cmd_resume(desk)


# --------------------------------------------------------------- the answers

def test_status_survives_a_missing_checkpoint(tmp_path):
    cfg = B.BotConfig(token="T", chat_id="1", state_path=tmp_path / "nope.json",
                      ledger_path=tmp_path / "no.jsonl", halt_path=tmp_path / "H")
    assert "NO CHECKPOINT" in B.cmd_status(cfg)


def test_status_reports_the_halt_flag(desk):
    B.cmd_halt(desk)
    assert "standing down" in B.cmd_status(desk)


def test_pnl_counts_only_resolved_rows(desk):
    out = B.cmd_pnl(desk)
    assert "2" in out and "+0.80R" in out


def test_pnl_refuses_to_quote_currency(desk):
    """The desk does not know your size. A euro figure would be a fabrication
    dressed as a fact."""
    out = B.cmd_pnl(desk)
    assert "€" not in out and "$" not in out
    assert "does not know your size" in out


def test_refusals_surface_what_they_cost(desk):
    out = B.cmd_refusals(desk)
    assert "no alignment" in out and "+1.40R" in out


def test_a_torn_ledger_line_costs_one_row_not_the_answer(desk):
    with desk.ledger_path.open("a") as fh:
        fh.write('{"ts": "2026-08-17T13:00:00+00:00", "kind": "SIG')
    assert "2" in B.cmd_pnl(desk)
    assert len(B._tail_ledger(desk.ledger_path)) == 3


def test_an_empty_ledger_is_reported_not_crashed(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    cfg = B.BotConfig(token="T", chat_id="1", state_path=tmp_path / "s.json",
                      ledger_path=p, halt_path=tmp_path / "H")
    assert "empty" in B.cmd_recent(cfg)
    assert "no resolved outcomes" in B.cmd_pnl(cfg)


def test_positions_says_flat_rather_than_nothing(desk):
    assert "flat" in B.cmd_positions(desk)


def test_growth_reports_a_derived_size_from_the_live_ledger(desk):
    """The sizing module is reachable from the channel, not shelf-ware."""
    out = B.cmd_growth(desk)
    assert "DERIVED, not chosen" in out or "watched long enough" in out


def test_growth_on_an_unwatched_book_refuses_to_name_a_size(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text("")
    cfg = B.BotConfig(token="T", chat_id="1", state_path=tmp_path / "s.json",
                      ledger_path=p, halt_path=tmp_path / "H")
    assert "watched long enough" in B.cmd_growth(cfg)


def test_help_lists_exactly_the_whitelist(desk):
    """A command reachable but undocumented is a command nobody audits."""
    text = B.cmd_help(desk)
    for c in B.COMMANDS:
        if c != "/start":
            assert c in text, f"{c} is dispatchable but not in /help"


# ----------------------------------------------------------------- transport

def test_an_over_long_reply_is_truncated_not_dropped(monkeypatch):
    """Telegram rejects >4096 chars outright, losing the whole answer."""
    seen = {}
    monkeypatch.setattr(B, "_api",
                        lambda tok, m, to, **p: seen.update(p) or {"ok": True})
    B.send("T", "1", "x" * 9000)
    assert len(seen["text"]) <= 4000 and seen["text"].endswith("(truncated)")


def test_replies_are_sent_as_plain_text(monkeypatch):
    """Mechanism names carry underscores and asterisks. Telegram rejects the
    whole message on an unbalanced Markdown entity, so a reply about `fvg_*`
    would vanish entirely."""
    seen = {}
    monkeypatch.setattr(B, "_api",
                        lambda tok, m, to, **p: seen.update(p) or {"ok": True})
    B.send("T", "1", "mechanism fvg_retest *stopped*")
    assert "parse_mode" not in seen


def test_the_bot_survives_a_dead_transport(desk, monkeypatch):
    monkeypatch.setattr(B, "_api", lambda *a, **k: None)
    assert B.Bot(desk).poll_once() == 0


def test_serve_forever_survives_a_raising_poll(desk, monkeypatch):
    monkeypatch.setattr(B, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    monkeypatch.setattr(B.Bot, "poll_once",
                        lambda self: (_ for _ in ()).throw(RuntimeError("net")))
    assert B.Bot(desk).serve_forever(max_polls=3) == 0


def test_build_bot_shares_the_sink_resolver(tmp_path):
    """Two implementations of "do we have credentials" is how a desk notifies
    fine and silently never answers a command."""
    assert B.build_bot(tmp_path / "absent") is None
    s = tmp_path / "secrets"
    s.mkdir()
    (s / "telegram_token").write_text("tok")
    (s / "telegram_chat_id").write_text("123")
    b = B.build_bot(s)
    assert b is not None and b.cfg.chat_id == "123"


def test_empty_credential_files_do_not_build_a_bot(tmp_path):
    """install.sh creates these empty so the operator has somewhere to put the
    values. Empty is not present."""
    s = tmp_path / "secrets"
    s.mkdir()
    (s / "telegram_token").write_text("")
    (s / "telegram_chat_id").write_text("")
    assert B.build_bot(s) is None
