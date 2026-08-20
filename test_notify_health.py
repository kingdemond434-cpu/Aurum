"""Aurum places no orders. The message IS the product, so a channel that fails
silently is not a degraded mode — it is total failure wearing a healthy process.

`TelegramSink.send` returns False rather than raising (correct: a notification
channel must not halt the loop) and `DeskService._notify` wrapped it in a bare
`except: pass`. A revoked bot meant every signal went nowhere, forever, with no
trace in the code, the log or the state file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from golddesk.notify import (
    FileSink, HealthTrackingSink, NullSink, build_sink, probe)


class Dead:
    """A sink that fails the way a revoked bot does: quietly, returning False."""
    def __init__(self): self.calls = 0
    def send(self, text): self.calls += 1; return False


class Raiser:
    def send(self, text): raise RuntimeError("connection reset")


class Live:
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(text); return True


# ------------------------------------------------- failure becomes visible

def test_a_silently_failing_sink_is_counted_not_discarded():
    """THE DEFECT. False was returned and thrown away by every caller."""
    s = HealthTrackingSink(Dead())
    for _ in range(3):
        s.send("signal")
    assert s.failed == 3 and s.sent == 0
    assert s.consecutive_failures == 3


def test_the_channel_is_declared_DOWN_after_enough_failures():
    s = HealthTrackingSink(Dead())
    for _ in range(HealthTrackingSink.ALARM_AFTER):
        s.send("x")
    assert not s.healthy


def test_one_dropped_message_does_not_trip_the_alarm():
    """A health flag that trips on a single blip is one people learn to ignore."""
    s = HealthTrackingSink(Dead())
    s.send("x")
    assert s.healthy


def test_a_success_clears_the_streak():
    s = HealthTrackingSink(Dead())
    for _ in range(4):
        s.send("x")
    s.inner = Live()
    s.send("x")
    assert s.consecutive_failures == 0 and s.healthy


def test_a_raising_sink_still_cannot_reach_the_loop():
    """Never propagates — but no longer vanishes."""
    s = HealthTrackingSink(Raiser())
    assert s.send("x") is False
    assert s.failed == 1


def test_the_alarm_fires_once_not_on_every_message(caplog):
    """An error line per signal for a week is how a real alarm gets filtered."""
    import logging
    s = HealthTrackingSink(Dead())
    with caplog.at_level(logging.ERROR):
        for _ in range(HealthTrackingSink.ALARM_AFTER + 5):
            s.send("x")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1


def test_the_alarm_says_the_desk_is_producing_nothing_visible(caplog):
    import logging
    s = HealthTrackingSink(Dead())
    with caplog.at_level(logging.ERROR):
        for _ in range(HealthTrackingSink.ALARM_AFTER):
            s.send("x")
    assert "producing nothing you can see" in caplog.text


def test_stats_carry_what_an_operator_needs():
    s = HealthTrackingSink(Live())
    s.send("x")
    st = s.stats()
    assert st["sent"] == 1 and st["healthy"] and st["last_ok_at"]
    assert st["sink"] == "Live"


# ------------------------------------------------------- it is the default

def test_health_tracking_is_on_by_default(tmp_path):
    """Anything you must remember to switch on for a SILENT failure is a thing
    that will be off when it matters."""
    s = build_sink(tmp_path / "absent")
    assert isinstance(s, HealthTrackingSink)


def test_the_wrapper_preserves_the_resolved_sink(tmp_path):
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "telegram_token").write_text("tok")
    (sec / "telegram_chat_id").write_text("123")
    s = build_sink(sec)
    assert type(s.inner).__name__ == "TelegramSink"


def test_an_unconfigured_desk_still_falls_back_to_a_file(tmp_path):
    s = build_sink(tmp_path / "absent", shadow_log=tmp_path / "shadow.jsonl")
    assert isinstance(s.inner, FileSink)
    s.send("would have sent")
    assert "would have sent" in (tmp_path / "shadow.jsonl").read_text()


def test_tracking_can_be_turned_off_deliberately(tmp_path):
    assert isinstance(build_sink(tmp_path / "x", track_health=False), NullSink)


# --------------------------------------------------- preflight must deliver

def test_probe_reports_a_real_delivery():
    ok, why = probe(Live())
    assert ok and "DELIVERED" in why


def test_probe_reports_a_failure_and_explains_the_trap():
    """Credentials being present is not the same as them working."""
    ok, why = probe(Dead())
    assert not ok
    assert "revoked bot" in why and "wrong chat id" in why


def test_preflight_sends_rather_than_only_reading_files(tmp_path, monkeypatch):
    """A non-empty token file proves somebody typed something into a file."""
    import run_desk
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "telegram_token").write_text("tok")
    (sec / "telegram_chat_id").write_text("123")
    sent = []
    monkeypatch.setattr("golddesk.notify.TelegramSink.send",
                        lambda self, text: sent.append(text) or True)
    c = run_desk._telegram_check(True, sec)
    assert c.ok and sent, "preflight passed without delivering anything"
    assert "DELIVERED" in c.detail


def test_preflight_FAILS_when_credentials_exist_but_do_not_work(tmp_path, monkeypatch):
    """THE CASE THAT USED TO PASS. This is the whole reason the check changed."""
    import run_desk
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "telegram_token").write_text("revoked-token")
    (sec / "telegram_chat_id").write_text("123")
    monkeypatch.setattr("golddesk.notify.TelegramSink.send",
                        lambda self, text: False)
    c = run_desk._telegram_check(True, sec)
    assert not c.ok and c.fatal


def test_empty_credentials_still_fail_before_any_send(tmp_path):
    import run_desk
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "telegram_token").write_text("")
    (sec / "telegram_chat_id").write_text("")
    c = run_desk._telegram_check(True, sec)
    assert not c.ok and "EMPTY" in c.detail


def test_delivery_can_be_skipped_deliberately(tmp_path):
    import run_desk
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "telegram_token").write_text("tok")
    (sec / "telegram_chat_id").write_text("123")
    c = run_desk._telegram_check(True, sec, deliver=False)
    assert c.ok and "NOT verified by delivery" in c.detail


def test_not_wanting_telegram_is_not_a_failure(tmp_path):
    import run_desk
    assert run_desk._telegram_check(False, tmp_path).ok


def test_claudecode_provider_without_numeric_only_fails_preflight():
    """The failure this catches: a desk that starts, ticks, warms bars, and
    refuses every analyst call forever because 'claudecode' cannot send charts
    and --numeric-only was never passed. It looked exactly like a healthy
    process -- MT5 connected, Telegram delivering -- while producing zero
    signals, on a real account, until someone read the log line by line."""
    import run_desk
    checks = run_desk.preflight(
        "XAUUSD", False, Path("secrets"), feed="mt5",
        provider_spec="claudecode:claude-opus-5", numeric_only=False)
    bad = [c for c in checks if c.name == "provider/vision match"]
    assert bad and not bad[0].ok and bad[0].fatal
    assert "--numeric-only" in bad[0].detail


def test_claudecode_provider_with_numeric_only_does_not_fail_this_check():
    import run_desk
    checks = run_desk.preflight(
        "XAUUSD", False, Path("secrets"), feed="mt5",
        provider_spec="claudecode:claude-opus-5", numeric_only=True)
    assert not [c for c in checks if c.name == "provider/vision match"]


def test_anthropic_provider_is_unaffected_by_the_numeric_only_check():
    """The check is specific to the CLI's no-image-input limitation, not a
    general opinion about charts vs numeric."""
    import run_desk
    checks = run_desk.preflight(
        "XAUUSD", False, Path("secrets"), feed="mt5",
        provider_spec="anthropic:claude-opus-5", numeric_only=False)
    assert not [c for c in checks if c.name == "provider/vision match"]
