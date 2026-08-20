"""The subscription-backed analyst path, tested against the CLI's real envelope.

The envelope shapes asserted here were MEASURED from Claude Code 2.1.235, not
imagined: a live `claude -p --output-format json` was run and its output read.
That matters for two of them in particular.

  THE FENCE IS REAL. Asked for bare JSON, with an explicit "no markdown code
  fences" in the system prompt, the CLI still returned ```json\\n{...}\\n```.
  A parser written from the documentation would fail on every single read.

  THE OVERHEAD IS REAL. Even with --system-prompt replacing the default and
  --allowed-tools disabled, a trivial call reported 26,488 cache-creation
  tokens. Free of dollars under a subscription; not free of quota. The usage
  mapping has to carry it or budget.py under-counts what the desk consumes.

The CLI itself is never invoked here. A real subprocess would need a logged-in
machine, would cost quota on every test run, and would make the suite fail on
a laptop with no network — so the transport is injected and every assertion is
about how this code treats what came back.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from golddesk.analyst import Context, Level, LevelKind, MarketBrief, Setup
from golddesk.chart import Chart
from golddesk.providers import (AnalystError, ClaudeCodeAnalyst, ProviderRead,
                                build_provider)

VALID_READ = {
    "setup": Setup.TREND_CONTINUATION.value, "direction": "LONG",
    "entry_ref": "MARKET", "stop_ref": "L1", "tp1_ref": "NONE", "tp2_ref": "L4",
    "mechanism_name": "pullback_continuation", "confidence": 3,
    "read": "trend intact", "why": "higher lows holding",
    "why_not": "could fail at L3", "invalidation": "close below L1",
}


def brief() -> MarketBrief:
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    lv = [Level("L1", LevelKind.SWING_LOW, 1990.0, "M15", 12, True),
          Level("L3", LevelKind.SWING_HIGH, 2010.0, "M15", 4, True),
          Level("L4", LevelKind.SWING_HIGH, 2062.0, "H4", 30, True)]
    ctx = Context(trend_direction="UP", trend_health="MODERATE",
                  trend_maturity="MID", volatility_state="NORMAL",
                  htf_alignment="ALIGNED", displacement_state="CONFIRMED",
                  sweep_state="CONFIRMED", reclaim_state="CONFIRMED",
                  pullback_depth="MEDIUM", distance_from_session_extreme="MID")
    return MarketBrief(symbol="XAUUSD", as_of_utc=now, session="LONDON",
                       bid=2004.8, ask=2005.2, spread=0.4, tick_age_s=1.0,
                       atr=4.0, levels=lv, context=ctx,
                       trigger_price=2005.0, trigger_utc=now)


def envelope(result: str, **over) -> dict:
    """The shape Claude Code 2.1.235 actually returns."""
    env = {
        "is_error": False, "subtype": "success", "stop_reason": "end_turn",
        "num_turns": 1, "permission_denials": [], "result": result,
        "total_cost_usd": 0.0542,
        "usage": {"input_tokens": 910, "cache_creation_input_tokens": 26488,
                  "cache_read_input_tokens": 0, "output_tokens": 61},
    }
    env.update(over)
    return env


def fake(result_or_env, seen: list | None = None):
    """A transport that records the argv it was handed."""
    def run(argv, prompt):
        if seen is not None:
            seen.append((argv, prompt))
        return (result_or_env if isinstance(result_or_env, dict)
                else envelope(result_or_env))
    return run


# ------------------------------------------------------------------ the read

def test_a_plain_json_result_parses():
    p = ClaudeCodeAnalyst(runner=fake(json.dumps(VALID_READ)))
    r = p.read(brief())
    assert isinstance(r, ProviderRead)
    assert r.read.direction == "LONG"
    assert r.provider == "claudecode"


def test_a_fenced_result_parses_because_the_cli_really_does_that():
    """Observed against 2.1.235, with 'no code fences' in the system prompt."""
    fenced = "```json\n" + json.dumps(VALID_READ) + "\n```"
    r = ClaudeCodeAnalyst(runner=fake(fenced)).read(brief())
    assert r.read.direction == "LONG"


def test_a_bare_fence_without_a_language_tag_parses():
    fenced = "```\n" + json.dumps(VALID_READ) + "\n```"
    assert ClaudeCodeAnalyst(runner=fake(fenced)).read(brief()).read.direction == "LONG"


def test_the_argv_pins_the_things_that_cost_money_or_change_behaviour():
    seen: list = []
    ClaudeCodeAnalyst(model="claude-opus-5",
                      runner=fake(json.dumps(VALID_READ), seen)).read(brief())
    argv, prompt = seen[0]
    assert argv[:2] == ["claude", "-p"]
    for flag, val in (("--output-format", "json"), ("--model", "claude-opus-5"),
                      ("--max-turns", "1")):
        assert argv[argv.index(flag) + 1] == val
    # Tools disabled: an analyst read must not become an agent session that
    # reads files or hits the network, which is a different cost and a
    # different risk surface entirely.
    assert argv[argv.index("--allowed-tools") + 1] == ""
    # The schema travels in-band because the CLI cannot enforce one.
    assert "SCHEMA" in prompt and "XAUUSD" in prompt


# --------------------------------------------------------------- the refusals

def test_charts_raise_rather_than_being_silently_dropped():
    """A chart arm that ran without charts would poison every comparison."""
    p = ClaudeCodeAnalyst(runner=fake(json.dumps(VALID_READ)))
    with pytest.raises(AnalystError, match="cannot send charts"):
        p.read(brief(), charts=[Chart(timeframe="M15", png=b"\x89PNG",
                                      width=800, height=600)])


def test_non_conforming_text_is_an_error_not_a_shrug():
    p = ClaudeCodeAnalyst(runner=fake("I think gold looks bullish today!"))
    with pytest.raises(AnalystError, match="not a valid AnalystRead"):
        p.read(brief())


def test_a_valid_json_object_of_the_wrong_shape_is_rejected():
    p = ClaudeCodeAnalyst(runner=fake(json.dumps({"direction": "LONG"})))
    with pytest.raises(AnalystError, match="not a valid AnalystRead"):
        p.read(brief())


def test_an_error_envelope_is_surfaced():
    p = ClaudeCodeAnalyst(runner=fake(envelope("boom", is_error=True)))
    with pytest.raises(AnalystError, match="reported failure"):
        p.read(brief())


def test_an_empty_result_is_an_error():
    with pytest.raises(AnalystError, match="empty result"):
        ClaudeCodeAnalyst(runner=fake("")).read(brief())


def test_a_permission_denial_is_surfaced_not_ignored():
    """Tools are off; if the CLI wanted one anyway, the read is not trustworthy."""
    env = envelope(json.dumps(VALID_READ), permission_denials=[{"tool": "Bash"}])
    with pytest.raises(AnalystError, match="denied a permission"):
        ClaudeCodeAnalyst(runner=fake(env)).read(brief())


def test_a_missing_binary_names_the_remedy():
    def boom(argv, prompt):
        raise FileNotFoundError(argv[0])
    p = ClaudeCodeAnalyst(runner=None)
    p._invoke = lambda prompt: boom(p._argv(), prompt)          # noqa: SLF001
    with pytest.raises(FileNotFoundError):
        p.read(brief())


# ------------------------------------------------------------------ the money

def test_usage_folds_cache_creation_into_input_so_budget_sees_it():
    """26,488 scaffolding tokens must not vanish from the cost accounting."""
    r = ClaudeCodeAnalyst(runner=fake(json.dumps(VALID_READ))).read(brief())
    u = r.usage
    assert u["in"] == 910 + 26488 + 0
    assert u["cache_read"] == 0
    assert u["out"] == 61
    # budget.Pricing derives fresh = in - cache_read; nothing may go missing.
    assert u["in"] - u["cache_read"] == 27398


def test_budget_prices_this_usage_without_crashing():
    from golddesk.budget import Pricing
    r = ClaudeCodeAnalyst(runner=fake(json.dumps(VALID_READ))).read(brief())
    assert Pricing().cost(r.usage) > 0


def test_subscription_mode_reports_zero_dollars_but_keeps_the_equivalent(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = ClaudeCodeAnalyst(runner=fake(json.dumps(VALID_READ))).read(brief())
    assert r.usage["billed"] is False
    assert r.usage["cost_usd"] == 0.0
    # The API-equivalent is kept, because "what this WOULD have cost" is the
    # whole argument for running it this way.
    assert r.usage["cost_usd_api_equivalent"] == 0.0542


def test_an_api_key_in_the_environment_means_it_really_is_billed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    r = ClaudeCodeAnalyst(runner=fake(json.dumps(VALID_READ))).read(brief())
    assert r.usage["billed"] is True
    assert r.usage["cost_usd"] == 0.0542


def test_the_operator_can_override_the_billing_guess(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    assert ClaudeCodeAnalyst(billed=False).billed() is False
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ClaudeCodeAnalyst(billed=True).billed() is True


def test_unbilled_mode_strips_the_key_from_the_child_environment(monkeypatch):
    """Declaring 'subscription' must make a stray key harmless, not merely unused.

    A key left in the environment is how a desk configured for a subscription
    quietly runs a metered bill that nobody sees until the invoice.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-leftover")
    env = ClaudeCodeAnalyst(billed=False)._env()                 # noqa: SLF001
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_billed_mode_leaves_the_environment_alone(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-intentional")
    assert ClaudeCodeAnalyst(billed=True)._env()["ANTHROPIC_API_KEY"] == \
        "sk-ant-intentional"                                    # noqa: SLF001


# ----------------------------------------------------------------- the wiring

def test_build_provider_knows_it():
    p = build_provider("claudecode:claude-opus-5")
    assert isinstance(p, ClaudeCodeAnalyst) and p.model == "claude-opus-5"


def test_build_provider_still_knows_the_others():
    assert build_provider("deterministic").name == "deterministic"
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider("nonsense")


def test_it_does_not_pretend_to_manage_positions():
    """Inherited refusal: no contextual management brain, and it says so."""
    with pytest.raises(NotImplementedError, match="choose_option"):
        ClaudeCodeAnalyst().choose_option("s", "p", ["a", "b"])


# --------------------------------------------------------------- --effort

def test_effort_is_threaded_into_the_cli_argv():
    """Confirmed against a real `claude --help`: the CLI documents --effort
    with exactly the same five values AnthropicAnalyst already accepts."""
    p = ClaudeCodeAnalyst(effort="high")
    argv = p._argv()                                          # noqa: SLF001
    assert argv[argv.index("--effort") + 1] == "high"


def test_no_effort_means_no_flag_and_the_cli_default_applies():
    p = ClaudeCodeAnalyst()
    assert "--effort" not in p._argv()                        # noqa: SLF001


def test_an_effort_value_the_cli_does_not_accept_is_refused_before_invoking():
    p = ClaudeCodeAnalyst(effort="ultra")
    with pytest.raises(AnalystError, match="not in"):
        p._argv()                                             # noqa: SLF001


def test_build_provider_threads_effort_into_claudecode():
    p = build_provider("claudecode:claude-opus-5", effort="xhigh")
    assert p.effort == "xhigh"


def test_build_provider_threads_effort_into_anthropic():
    a = build_provider("anthropic:claude-opus-5", effort="low")
    assert a.effort == "low"


def test_build_provider_refuses_effort_on_a_provider_that_cannot_use_it():
    """deterministic reasons about nothing; a raw TypeError here would read
    as an internal bug rather than a misapplied flag."""
    with pytest.raises(ValueError, match="does not accept"):
        build_provider("deterministic", effort="high")
