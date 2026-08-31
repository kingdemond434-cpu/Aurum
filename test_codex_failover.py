import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from golddesk.analyst import Context, MarketBrief
from golddesk.providers import (
    AnalystError, AnalystProvider, AnalystQuotaError, CodexCliAnalyst, FailoverAnalyst,
    ProviderRead, build_provider_chain, strict_output_schema)

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


def test_three_blind_primary_calls_open_circuit_and_fourth_skips_primary():
    primary, fallback = Broken(), Good()
    primary.calls = 0
    original = primary.read
    def counted(*args, **kwargs):
        primary.calls += 1
        return original(*args, **kwargs)
    primary.read = counted
    chain = FailoverAnalyst([primary, fallback], blind_threshold=3)
    for _ in range(4):
        assert chain.read(brief()).provider == "good"
    assert primary.calls == 3


def test_quota_opens_primary_circuit_immediately():
    class Quota(Broken):
        def __init__(self): self.calls = 0
        def read(self, brief, charts=()):
            self.calls += 1
            raise AnalystQuotaError("weekly limit")
    primary, fallback = Quota(), Good()
    chain = FailoverAnalyst([primary, fallback])
    assert chain.read(brief()).provider == "good"
    assert chain.read(brief()).provider == "good"
    assert primary.calls == 1


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


def test_claude_timeout_hands_control_to_configured_gpt_fallback():
    chain = build_provider_chain("claudecode:claude-opus-5",
                                 ("codex:gpt-5.6-sol",),
                                 fallback_kw={"effort": "high"}, effort="high")
    assert chain.providers[0].timeout_s == 240.0
    assert chain.providers[0].retry_on_timeout is False


def test_claude_stays_primary_numeric_while_gpt_retains_fallback_charts():
    class RecordingGood(Good):
        def __init__(self, name):
            self.name, self.seen = name, None
        def read(self, brief, charts=()):
            self.seen = charts
            return super().read(brief, charts)

    claude, gpt = RecordingGood("claudecode"), RecordingGood("codex")
    charts = (object(),)
    result = FailoverAnalyst([claude, gpt]).read(brief(), charts)
    assert result.provider == "claudecode"
    assert claude.seen == () and gpt.seen is None

    broken = Broken()
    broken.name = "claudecode"
    result = FailoverAnalyst([broken, gpt]).read(brief(), charts)
    assert result.provider == "codex"
    assert gpt.seen == charts


def test_codex_universe_and_failover_retain_every_candidate_and_chart():
    from golddesk.universe import AnalystUniverse

    payload = {"candidates": [VALID, VALID], "survey": "both directions checked",
               "dominant_context": "range edge", "had_more": False}
    seen = {}

    def runner(argv, prompt):
        seen["argv"], seen["prompt"] = argv, prompt
        schema_path = argv[argv.index("--output-schema") + 1]
        seen["schema"] = json.loads(open(schema_path, encoding="utf-8").read())
        return json.dumps(payload)

    gpt = CodexCliAnalyst(runner=runner)
    broken = Broken()
    broken.name = "claudecode"
    chart = SimpleNamespace(timeframe="M15", png=b"chart")
    stamp, universe = FailoverAnalyst([broken, gpt]).survey(brief(), (chart,))

    assert isinstance(universe, AnalystUniverse)
    assert len(universe.candidates) == 2
    assert stamp.provider == "codex"
    assert stamp.usage["charts_sent"] == 1
    assert "--image" in seen["argv"]
    assert set(seen["schema"]["required"]) == set(seen["schema"]["properties"])
    read_def = seen["schema"]["$defs"]["AnalystRead"]
    assert read_def["additionalProperties"] is False
    assert set(read_def["required"]) == set(read_def["properties"])
    path_def = seen["schema"]["$defs"]["PathForecast"]
    assert path_def["additionalProperties"] is False
    assert set(path_def["required"]) == set(path_def["properties"])
