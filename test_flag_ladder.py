r"""The CLI started refusing a flag, and the desk went blind rather than degrading.

WHAT HAPPENED, 2026-08-28. After a restart every read failed identically:

    claude exited 1: {"session_id":"...","is_error":true,"duration_api_ms":0,
                      "stop_reason":"stop_sequence","num_turns":1,
                      "usage":{"input_tokens":0,"output_tokens":0}}

Zero tokens in AND out, zero API duration, but a session id and a clean exit 1 --
the CLI parsed the invocation, started, and declined BEFORE sending anything. That
rules out a rate limit, a model outage, a timeout and a bad response, and rules in
the invocation itself.

The desk's answer was to book a BLIND and wait for a human to work out which flag
a CLI update had stopped accepting. That is the wrong answer: a day spent
diagnosing is a day with no reads, and the flags this desk passes were accepted
for weeks and then were not, so it will happen again.

WHAT THIS TESTS. The ladder that replaces the diagnosis: on a local rejection the
provider drops one flag and retries, least-harmful first, remembering the rung
that worked. And, just as load-bearing, what it must NOT do -- a real API failure
must never walk the ladder, because degrading the arm in response to a problem the
flags did not cause turns one outage into a permanently weakened read.

    python3 -m pytest test_flag_ladder.py -q
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.providers import AnalystError, ClaudeCodeAnalyst

#: The exact envelope observed live on 2026-08-28. Not a simplified stand-in:
#: the discriminator this whole mechanism rests on is which fields are present
#: and zero, so a hand-tidied version would test a different thing.
LOCAL_REJECTION = json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "duration_ms": 812, "duration_api_ms": 0, "num_turns": 1,
    "session_id": "a3f0e1c2-0000-4000-8000-000000000000",
    "stop_reason": "stop_sequence", "total_cost_usd": 0.0,
    "usage": {"input_tokens": 0, "output_tokens": 0,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
})

#: A REAL failure: the API was reached, tokens were spent, the answer was bad.
#: This must be handled as an error, never as a reason to degrade the arm.
API_FAILURE = json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "duration_ms": 41_200, "duration_api_ms": 40_900, "num_turns": 1,
    "session_id": "b4f0e1c2-0000-4000-8000-000000000000",
    "usage": {"input_tokens": 26_488, "output_tokens": 0,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
})

SUCCESS = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "duration_api_ms": 9_100, "result": "{}",
                      "usage": {"input_tokens": 910, "output_tokens": 61}})


def _ladder_runner(rejected_flags, ok=SUCCESS, failure=LOCAL_REJECTION):
    """A CLI that refuses locally while ANY flag in `rejected_flags` is present.

    Returns the (rc, stdout, stderr) tuple shape, so the call travels the same
    _invoke path production does -- exit code, envelope parsing, ladder and all
    -- rather than being handed a pre-parsed success.
    """
    seen: list[list[str]] = []

    def run(argv, prompt):
        seen.append(list(argv))
        if any(f in argv for f in rejected_flags):
            return (1, failure, "")
        return (0, ok, "")

    return run, seen


def _p(**kw):
    return ClaudeCodeAnalyst(model="claude-opus-5", effort="high", **kw)


# --------------------------------------------------------------------------
# It steps down.

def test_a_rejected_flag_is_dropped_instead_of_leaving_the_desk_blind():
    run, seen = _ladder_runner({"--effort"})
    env = _p(runner=run)._invoke("BRIEF")
    assert env["subtype"] == "success"
    assert len(seen) == 2, "expected one rejected attempt then one that worked"
    assert "--effort" in seen[0] and "--effort" not in seen[1]


def test_it_keeps_stepping_until_something_works():
    """The rejected flag is not always the first rung. A ladder that gave up
    after one step would fix exactly one quarter of the failures it exists for."""
    run, seen = _ladder_runner({"--max-turns"})
    env = _p(runner=run)._invoke("BRIEF")
    assert env["subtype"] == "success"
    # --max-turns is the third rung, so it survives drop=0,1,2 and goes at 3.
    assert [("--max-turns" in a) for a in seen] == [True, True, True, False]


def test_the_model_is_the_last_thing_given_up():
    """Dropping --model changes WHICH MODEL answers, which changes the arm and
    makes reads from either side non-comparable inside one cohort. Everything
    cheaper is surrendered first."""
    run, seen = _ladder_runner({"--effort", "--allowed-tools", "--max-turns", "--model"})
    _p(runner=run)._invoke("BRIEF")
    # The model survives every attempt but the last: it is given up only once
    # nothing cheaper is left to give up.
    assert [("--model" in a) for a in seen] == [True, True, True, True, False]


def test_the_ladder_is_finite_and_the_last_word_names_the_login():
    """When every flag is gone and the CLI still refuses without spending a
    token, it is no longer a flag problem. Saying so is the difference between
    one line and another hour of guessing."""
    run, _ = _ladder_runner({"--output-format"})   # never droppable: always rejects
    with pytest.raises(AnalystError, match="NOT a rejected flag"):
        _p(runner=run)._invoke("BRIEF")


# --------------------------------------------------------------------------
# It does NOT step down for anything else. This half is the load-bearing one.

def test_a_real_api_failure_does_not_degrade_the_arm():
    """Tokens were spent, so the API was reached and the flags were accepted.
    Walking the ladder here would answer an outage by permanently weakening the
    read -- the exact overreaction this desk has shipped before under other
    names."""
    run, seen = _ladder_runner({"--effort"}, failure=API_FAILURE)
    with pytest.raises(AnalystError, match="claude exited 1"):
        _p(runner=run)._invoke("BRIEF")
    assert len(seen) == 1, "a billed failure must not be retried down the ladder"


def test_a_missing_duration_field_is_not_read_as_zero():
    """Absence is not zero (L1.28a). A CLI that stopped reporting
    duration_api_ms would otherwise make every failure look local and walk the
    whole ladder to an unpinned model on the first real outage."""
    silent = json.dumps({"is_error": True, "usage": {"input_tokens": 0,
                                                     "output_tokens": 0}})
    run, seen = _ladder_runner({"--effort"}, failure=silent)
    with pytest.raises(AnalystError):
        _p(runner=run)._invoke("BRIEF")
    assert len(seen) == 1


def test_unparseable_output_is_not_read_as_a_local_rejection():
    run, seen = _ladder_runner({"--effort"}, failure="segmentation fault")
    with pytest.raises(AnalystError):
        _p(runner=run)._invoke("BRIEF")
    assert len(seen) == 1


def test_a_successful_call_never_touches_the_ladder():
    run, seen = _ladder_runner(set())
    p = _p(runner=run)
    p._invoke("BRIEF")
    assert len(seen) == 1
    assert p._flag_drop == 0


# --------------------------------------------------------------------------
# It remembers.

def test_the_working_rung_is_remembered_so_the_probe_is_paid_for_once():
    """A ladder re-walked on every wake would spend two invocations a read
    forever, and on a subscription that is quota the desk could have spent on
    reading the market."""
    run, seen = _ladder_runner({"--effort"})
    p = _p(runner=run)
    p._invoke("BRIEF")
    assert p._flag_drop == 1
    seen.clear()
    p._invoke("BRIEF")
    assert len(seen) == 1, "the ladder was re-walked instead of remembered"
    assert "--effort" not in seen[0]


def test_a_failed_descent_is_not_remembered():
    """Memory is of a rung that WORKED. Recording one that did not would leave
    the desk permanently degraded by a transient failure."""
    run, _ = _ladder_runner({"--output-format"})
    p = _p(runner=run)
    with pytest.raises(AnalystError):
        p._invoke("BRIEF")
    assert p._flag_drop == 0


def test_the_memory_is_per_provider_not_global():
    run, _ = _ladder_runner({"--effort"})
    degraded = _p(runner=run)
    degraded._invoke("BRIEF")
    assert degraded._flag_drop == 1
    assert _p()._flag_drop == 0, "degradation leaked onto a fresh provider"


# --------------------------------------------------------------------------
# It says so.

def test_dropping_the_model_is_logged_at_error_not_warning(caplog):
    """analyst_health reports the substitution within fifteen minutes, but only
    if the desk announces it. A silent model swap makes a cohort's reads
    incomparable with nothing in the record saying when it started."""
    run, _ = _ladder_runner({"--effort", "--allowed-tools", "--max-turns", "--model"})
    with caplog.at_level(logging.DEBUG):
        _p(runner=run)._invoke("BRIEF")
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, caplog.text
    assert any("NO LONGER PINNED" in r.getMessage() for r in errs)


def test_a_harmless_degradation_says_capability_is_intact(caplog):
    """The two cases must not read alike. Dropping --effort costs depth;
    dropping --model changes the arm. An operator scanning the log has to be
    able to tell them apart at a glance."""
    run, _ = _ladder_runner({"--effort"})
    with caplog.at_level(logging.DEBUG):
        _p(runner=run)._invoke("BRIEF")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "capability intact" in msgs
    assert "NO LONGER PINNED" not in msgs


def test_every_descent_is_logged_with_the_flag_it_gave_up(caplog):
    run, _ = _ladder_runner({"--max-turns"})
    with caplog.at_level(logging.WARNING):
        _p(runner=run)._invoke("BRIEF")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    for flag in ("--effort", "--allowed-tools", "--max-turns"):
        assert flag in msgs, flag


# --------------------------------------------------------------------------
# The argv it actually produces at each rung.

def test_each_rung_removes_exactly_one_more_flag():
    p = _p()
    # Flags only: an argv also carries their VALUES, and "claude-opus-5"
    # disappearing alongside "--model" is the same fact counted twice.
    kept = [{a for a in p._argv(drop=d) if a.startswith("--")}
            for d in range(len(p.FLAG_LADDER) + 1)]
    for i, (a, b) in enumerate(zip(kept, kept[1:])):
        assert a - b == {p.FLAG_LADDER[i]}, f"rung {i} removed {a - b}"
    assert kept[0] - kept[-1] == set(p.FLAG_LADDER)


def test_the_prompt_transport_survives_every_rung():
    """Whatever is dropped, the model must still receive the brief. A rung that
    quietly stopped sending the system text would produce reads that parse,
    look healthy, and answer a question nobody asked."""
    from golddesk.analyst import ANALYST_SYSTEM
    p = _p()
    for d in range(len(p.FLAG_LADDER) + 1):
        argv = p._argv(drop=d)
        assert "--system-prompt" in argv and ANALYST_SYSTEM in argv, d


def test_an_oversized_prompt_is_not_sent_twice_on_a_retry():
    """The retry paths recurse with the caller's prompt. When the system text is
    too big for argv it is prepended for stdin -- and prepending IN PLACE meant
    the retry prepended it again, sending 9,098 chars twice on exactly the path
    that was already failing."""
    from golddesk.analyst import ANALYST_SYSTEM
    from golddesk.universe import MAX_CANDIDATES, universe_system

    big = universe_system(ANALYST_SYSTEM, MAX_CANDIDATES)
    run, _ = _ladder_runner({"--effort"})
    sent: list[str] = []

    def spy(argv, prompt):
        sent.append(prompt)
        return run(argv, prompt)

    _p(runner=spy)._invoke("BRIEF-BODY", system=big)
    assert len(sent) == 2
    for payload in sent:
        assert payload.count("BRIEF-BODY") == 1
        assert payload.count(big) == 1, "the system prompt was sent twice"


# --------------------------------------------------------------------------
# The login. Byte-identical to a rejected flag on every field the ladder reads,
# and the ladder is the WRONG answer to it.

#: Verbatim from the box, 2026-08-28 01:15Z. Note what it claims about itself:
#: subtype "success", api_error_status null, stop_reason "stop_sequence", and
#: is_error true — the only field that says anything true is `result`.
EXPIRED_LOGIN = json.dumps({
    "is_error": True, "duration_api_ms": 0, "num_turns": 1,
    "stop_reason": "stop_sequence", "subtype": "success",
    "session_id": "109b4c99-1aff-48ed-a397-d50f364fec57",
    "total_cost_usd": 0, "api_error_status": None,
    "terminal_reason": "api_error", "permission_denials": [],
    "usage": {"input_tokens": 0, "output_tokens": 0,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    "type": "result", "duration_ms": 540,
})


def test_an_expired_login_is_named_not_guessed_at():
    run, _ = _ladder_runner({"--effort"}, failure=EXPIRED_LOGIN)
    with pytest.raises(AnalystError, match="cannot authenticate"):
        _p(runner=run)._invoke("BRIEF")


def test_an_expired_login_does_not_walk_the_ladder():
    """It is indistinguishable from a rejected flag on exit code, token counts
    and API duration — so without the message check the desk would spend four
    invocations, unpin the model, log 'CLI FLAGS DEGRADED', and still be blind,
    with the log now pointing away from the cause."""
    run, seen = _ladder_runner({"--effort"}, failure=EXPIRED_LOGIN)
    p = _p(runner=run)
    with pytest.raises(AnalystError):
        p._invoke("BRIEF")
    assert len(seen) == 1, "the ladder was walked against a login problem"
    assert p._flag_drop == 0, "the arm was degraded by an expired login"


def test_the_error_carries_the_command_that_fixes_it():
    """The desk cannot clear this one. The least it can do is not make the
    person holding the browser go and look it up."""
    run, _ = _ladder_runner({"--effort"}, failure=EXPIRED_LOGIN)
    with pytest.raises(AnalystError) as ei:
        _p(runner=run)._invoke("BRIEF")
    msg = str(ei.value)
    assert "`claude`" in msg and "interactively" in msg
    assert "NOT A FLAG" in msg


def test_a_successful_read_is_never_mistaken_for_a_login_failure():
    assert ClaudeCodeAnalyst._auth_failure(SUCCESS, "") is None
    assert ClaudeCodeAnalyst._auth_failure(LOCAL_REJECTION, "") is None
    assert ClaudeCodeAnalyst._auth_failure(API_FAILURE, "") is None


def test_the_ledger_row_says_the_login_expired():
    """_explain_analyst_error is what puts a cause in the BLIND row. The
    provider now raises a plain sentence with no JSON in it for this case, so a
    JSON-first reader returned {} for the one failure whose cause is fully
    known."""
    from golddesk.live import _explain_analyst_error
    run, _ = _ladder_runner({"--effort"}, failure=EXPIRED_LOGIN)
    with pytest.raises(AnalystError) as ei:
        _p(runner=run)._invoke("BRIEF")
    detail = _explain_analyst_error(ei.value)
    assert detail.get("needs_login") is True
    assert "LOGIN" in detail["reading"]


def test_the_health_check_and_the_provider_agree_on_what_a_login_failure_says():
    """Two lists of markers in two modules is how a detector and its watchdog
    start disagreeing about the same event."""
    from golddesk.analyst_health import LOGIN_MARKERS
    assert set(LOGIN_MARKERS) <= set(ClaudeCodeAnalyst.AUTH_MARKERS)


def test_the_health_check_fires_on_a_single_row():
    """Every other check here refuses to speak under MIN_WAKES, and rightly so.
    A login is not a rate to estimate — one row carrying the CLI's own sentence
    is conclusive, and waiting for twenty is twenty blind bars."""
    from golddesk.analyst_health import check_login
    row = {"t0": "2026-08-28T01:15:00+00:00", "kind": "BLIND",
           "decision": {"cli": {"subtype": "success", "result":
                                "Failed to authenticate: OAuth session expired "
                                "and could not be refreshed"}}}
    from datetime import datetime, timezone
    now = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
    f = check_login([row], now)
    assert not f.ok
    assert "LOGIN HAS EXPIRED" in f.detail
    assert check_login([], now).ok


def test_the_health_check_ignores_blind_rows_with_other_causes():
    from datetime import datetime, timezone

    from golddesk.analyst_health import check_login
    now = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
    row = {"t0": "2026-08-28T01:15:00+00:00", "kind": "BLIND",
           "decision": {"error": "claude timed out after 600.0s"}}
    assert check_login([row], now).ok


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------
# Who is paying. Not a detail: the entire reason this provider exists is that
# the metered API priced the desk out at ~$290-580/month against a EUR 1,500
# account, and that saving is real only if the login is a subscription one.

def _clear_billing_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(ClaudeCodeAnalyst.BILLING_ENV, raising=False)


def test_an_undeclared_zero_is_labelled_an_assumption(monkeypatch):
    """THE THIRD CASE the old heuristic folded into the second. An OAuth login
    to an organisation on API usage billing has no ANTHROPIC_API_KEY anywhere
    and is charged in dollars regardless -- observed on the live box, whose CLI
    banner read 'Opus 5 (1M context) - API Usage Billing - <org>' while the desk
    stamped cost_usd 0.0 on every read."""
    _clear_billing_env(monkeypatch)
    p = ClaudeCodeAnalyst()
    assert p.billing_basis() == "assumed_subscription"
    assert p.billed() is False


def test_the_operator_can_declare_metered_billing_without_an_api_key(monkeypatch):
    """The fix has to be reachable from the box's .env, because that is where
    the fact lives -- the login is a property of the machine, not of a run."""
    _clear_billing_env(monkeypatch)
    monkeypatch.setenv(ClaudeCodeAnalyst.BILLING_ENV, "api")
    p = ClaudeCodeAnalyst()
    assert p.billing_basis() == "declared_api"
    assert p.billed() is True


def test_a_declared_subscription_is_distinguishable_from_a_guessed_one(monkeypatch):
    """Both answer False. Only one of them is evidence."""
    _clear_billing_env(monkeypatch)
    monkeypatch.setenv(ClaudeCodeAnalyst.BILLING_ENV, "subscription")
    declared = ClaudeCodeAnalyst().billing_basis()
    _clear_billing_env(monkeypatch)
    guessed = ClaudeCodeAnalyst().billing_basis()
    assert declared != guessed
    assert ClaudeCodeAnalyst().billed() is False


def test_an_api_key_still_means_metered(monkeypatch):
    _clear_billing_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    p = ClaudeCodeAnalyst()
    assert p.billing_basis() == "api_key_present"
    assert p.billed() is True


def test_an_explicit_constructor_flag_beats_every_environment_signal(monkeypatch):
    _clear_billing_env(monkeypatch)
    monkeypatch.setenv(ClaudeCodeAnalyst.BILLING_ENV, "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    p = ClaudeCodeAnalyst(billed=False)
    assert p.billing_basis() == "explicit"
    assert p.billed() is False


def test_the_read_stamp_carries_how_the_cost_was_decided(monkeypatch):
    """budget.py has to be able to tell a declared zero from a guessed one. A
    bare 0.0 reads identically either way, and one of them is wrong."""
    _clear_billing_env(monkeypatch)
    from golddesk.analyst import MarketBrief  # noqa: F401  (import shape check)
    run, _ = _ladder_runner(set())
    p = ClaudeCodeAnalyst(runner=run)
    env = p._invoke("BRIEF")
    assert env["subtype"] == "success"
    # The stamp is assembled in read()/survey(); the basis it uses is this one.
    assert p.billing_basis() == "assumed_subscription"
