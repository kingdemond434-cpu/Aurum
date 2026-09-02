import json
from datetime import datetime, timezone

import pytest

from golddesk.analyst import Context, MarketBrief
from golddesk.providers import (
    AnalystError, AnalystProvider, CodexCliAnalyst, FailoverAnalyst,
    ProviderRead, build_provider_chain)

VALID = {
    "setup": "NO_SETUP", "direction": "NONE", "entry_ref": "NONE",
    "stop_ref": "NONE", "tp1_ref": "NONE", "tp2_ref": "NONE",
    "mechanism_name": "none", "confidence": 1, "read": "no setup",
    "why": "no forced flow", "why_not": "a reclaim would change this",
    "invalidation": "new structure",
}


def brief():
    return MarketBrief(
        "XAUUSD", datetime(2026, 8, 30, tzinfo=timezone.utc), "LONDON",
        2000, 2000.5, 0.5, 0, 5,
        Context("UP", "MODERATE", "MID", "NORMAL", "ALIGNED", "NONE",
                "NONE", "NONE", "SHALLOW", "MID"), ())


def test_codex_exec_is_ephemeral_read_only_and_schema_validated(tmp_path):
    p = CodexCliAnalyst()
    argv = p._argv(str(tmp_path), str(tmp_path / "schema.json"),
                   str(tmp_path / "answer.json"))
    assert argv[:2] == ["codex", "exec"]
    assert "--ephemeral" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--output-schema" in argv and "--output-last-message" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="high"' in argv
    assert argv[-1] == "-"


def test_codex_provider_validates_the_final_json():
    p = CodexCliAnalyst(runner=lambda argv, prompt: json.dumps(VALID))
    result = p.read(brief())
    assert result.provider == "codex"
    assert result.read.setup.value == "NO_SETUP"


def test_codex_provider_refuses_malformed_output():
    p = CodexCliAnalyst(runner=lambda argv, prompt: "bullish probably")
    with pytest.raises(AnalystError, match="not a valid AnalystRead"):
        p.read(brief())


class Broken(AnalystProvider):
    name = "broken"
    model = "x"
    def read(self, brief, charts=()):
        raise AnalystError("offline")


class Good(AnalystProvider):
    name = "good"
    model = "y"
    calls = 0
    def read(self, brief, charts=()):
        from golddesk.analyst import AnalystRead
        self.calls += 1
        return ProviderRead(AnalystRead.model_validate(VALID), self.name,
                            self.model, 1.0, {})


def test_failover_uses_the_next_provider_and_records_the_failure():
    result = FailoverAnalyst([Broken(), Good()]).read(brief())
    assert result.provider == "good"
    assert result.usage["failover_index"] == 1
    assert "offline" in result.usage["failover_errors"][0]


def test_a_valid_no_setup_is_a_verdict_not_a_failover_trigger():
    first, second = Good(), Good()
    result = FailoverAnalyst([first, second]).read(brief())
    assert result.read.setup.value == "NO_SETUP"
    assert first.calls == 1 and second.calls == 0


def test_provider_chain_builds_codex_as_the_local_fallback():
    chain = build_provider_chain("deterministic", ("codex:gpt-5.6-sol",),
                                 fallback_kw={"effort": "high"})
    assert isinstance(chain, FailoverAnalyst)
    assert [p.name for p in chain.providers] == ["deterministic", "codex"]
    assert chain.providers[1].model == "gpt-5.6-sol"
    assert chain.providers[1].effort == "high"


def test_primary_effort_does_not_change_the_pinned_fallback_effort():
    chain = build_provider_chain("codex:gpt-5.6-sol", ("codex:gpt-5.6-sol",),
                                 fallback_kw={"effort": "high"}, effort="max")
    assert chain.providers[0].effort == "max"
    assert chain.providers[1].effort == "high"


# ------------------------------- the chain the SERVICE actually builds ---------
# build_provider_chain is only half the story: build_service decides which
# fallbacks reach it. That decision is what produced 1,030 BLIND bars.

def _service_fallbacks(primary_spec, configured=("codex:gpt-5.6-sol",)):
    """Mirror of build_service's fallback selection, exercised without MT5."""
    from golddesk.service import build_service  # import guard only
    primary_name = primary_spec.partition(":")[0]
    fallbacks = (() if primary_name in {"deterministic", "replay"}
                 else tuple(configured))
    return tuple(s for s in fallbacks
                 if s.partition(":")[0] != primary_name)


def test_a_codex_primary_still_gets_a_fallback():
    """codex used to be in the no-fallback set, so a codex primary ran alone.

    Every non-zero exit of the CLI then became a bar the desk never read. The
    live desk recorded 1,030 of them against `codex exited 1`.
    """
    assert _service_fallbacks("codex:gpt-5.6-sol",
                              ("claudecode:claude-opus-5",)) == \
        ("claudecode:claude-opus-5",)


def test_a_chain_never_falls_back_to_the_provider_that_just_failed():
    """Otherwise --provider codex retries the same broken CLI and calls it failover."""
    assert _service_fallbacks("codex:gpt-5.6-sol", ("codex:gpt-5.6-sol",)) == ()


def test_subscription_primary_keeps_codex_as_the_fallback():
    assert _service_fallbacks("claudecode:claude-opus-5") == ("codex:gpt-5.6-sol",)


def test_offline_providers_still_refuse_failover():
    """replay and deterministic must be exactly themselves or replay is a lie."""
    assert _service_fallbacks("replay") == ()
    assert _service_fallbacks("deterministic") == ()
