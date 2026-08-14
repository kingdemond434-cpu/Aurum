"""Provider-neutral analyst interface.

Aurum must not be architecturally married to one vendor. The desk depends on
this interface; Claude is the first implementation, not the contract.

To add a provider, implement AnalystProvider.read() so it returns a validated
AnalystRead. That is the entire obligation — the compiler, router, risk gate,
management engine and ledger are all provider-agnostic already.

Every read is stamped with provider + model + latency so the frozen evaluation
can replay identical states across providers and attribute the difference.
"""

from __future__ import annotations

import abc
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .analyst import (ANALYST_SCHEMA, ANALYST_SYSTEM, AnalystRead, MarketBrief)
from .chart import Chart

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderRead:
    read: AnalystRead
    provider: str
    model: str
    latency_ms: float
    usage: dict = field(default_factory=dict)

    def stamp(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "latency_ms": round(self.latency_ms, 1), "usage": self.usage}


class AnalystError(RuntimeError):
    pass


class AnalystProvider(abc.ABC):
    """The whole contract. Implement read(); everything else is shared."""

    name: str = "abstract"
    model: str = "none"

    @abc.abstractmethod
    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead: ...

    def choose_option(self, system: str, prompt: str,
                      option_ids: Sequence[str]) -> str:
        """Pick exactly one id from a legality-filtered set. Management brain.

        The default raises rather than guessing, because the alternative is a
        provider silently behaving as though it managed positions when it did
        not — the caller must be able to tell "no contextual management" from
        "contextual management chose HOLD". Those are different systems and the
        factorial has to be able to distinguish them.

        The returned id is validated by the caller against `option_ids`; a
        provider cannot invent a stop price because it never sees one to invent.
        """
        raise NotImplementedError(
            f"provider {self.name!r} does not implement choose_option; it cannot "
            f"be used as a contextual management brain")

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model}


# --------------------------------------------------------------------------

class AnthropicAnalyst(AnalystProvider):
    """Claude. The first provider, not a privileged one."""

    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5", effort: str = "medium",
                 max_tokens: int = 8000, client: Any = None):
        self.model, self.effort, self.max_tokens = model, effort, max_tokens
        self._client = client

    def _lazy_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead:
        import anthropic
        client = self._lazy_client()
        content: list[dict] = []
        for c in charts:
            content.append({"type": "text", "text": f"Chart — {c.timeframe}, closed bars:"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(c.png).decode("ascii")}})
        content.append({"type": "text", "text": brief.render()})

        t0 = time.monotonic()
        try:
            resp = client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=[{"type": "text", "text": ANALYST_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": ANALYST_SCHEMA}},
                messages=[{"role": "user", "content": content}])
        except anthropic.RateLimitError as e:
            raise AnalystError(f"rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise AnalystError(f"api {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AnalystError(f"connection: {e}") from e
        dt = (time.monotonic() - t0) * 1000

        if resp.stop_reason == "refusal":
            raise AnalystError(f"declined (category="
                               f"{getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise AnalystError("truncated — raise max_tokens or lower effort")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise AnalystError("no text block")
        return ProviderRead(AnalystRead.model_validate_json(text), self.name,
                            self.model, dt,
                            {"in": resp.usage.input_tokens,
                             "cache_read": resp.usage.cache_read_input_tokens,
                             "out": resp.usage.output_tokens})

    def choose_option(self, system: str, prompt: str,
                      option_ids: Sequence[str]) -> str:
        """Management brain. Constrained to an enum of ids the code produced.

        The schema is built per call from the live option list, so the model
        physically cannot return an id that is not legal in this exact position
        state. It has no field in which to express a price.
        """
        import anthropic
        client = self._lazy_client()
        schema = {"type": "object",
                  "properties": {
                      "option_id": {"type": "string", "enum": list(option_ids)},
                      "because": {"type": "string", "maxLength": 400}},
                  "required": ["option_id", "because"],
                  "additionalProperties": False}
        try:
            resp = client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}])
        except anthropic.RateLimitError as e:
            raise AnalystError(f"rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise AnalystError(f"api {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AnalystError(f"connection: {e}") from e
        if resp.stop_reason == "refusal":
            raise AnalystError("management choice declined")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise AnalystError("no text block in management choice")
        oid = json.loads(text).get("option_id")
        if oid not in option_ids:
            raise AnalystError(f"illegal option {oid!r} not in {list(option_ids)}")
        return oid


class ReplayAnalyst(AnalystProvider):
    """Replays stored reads keyed by decision_id.

    This is how a NEW provider is evaluated fairly: run it over the same briefs
    the incumbent saw, then compare on identical states rather than on whatever
    market each happened to trade.
    """

    name = "replay"

    def __init__(self, reads: dict[str, dict], model: str = "recorded",
                 choices: Optional[dict[str, str]] = None):
        self._reads, self.model = reads, model
        self._choices = dict(choices or {})

    @classmethod
    def from_ledger(cls, rows: Sequence[dict], model: str = "recorded") -> "ReplayAnalyst":
        return cls({r["decision_id"]: r for r in rows if "analyst_read" in r.get("decision", {})},
                   model)

    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead:
        key = f"{brief.symbol}-{brief.as_of_utc.isoformat()}"
        row = self._reads.get(key)
        if row is None:
            raise AnalystError(f"no recorded read for {key}")
        return ProviderRead(AnalystRead.model_validate(row["decision"]["analyst_read"]),
                            self.name, self.model, 0.0)

    def choose_option(self, system: str, prompt: str,
                      option_ids: Sequence[str]) -> str:
        """Replay a recorded management choice, refusing if the option set moved.

        If the legal options are not the ones the recorded choice was made
        against, the replay is invalid — the position state has diverged and
        pretending otherwise would fabricate a comparison.
        """
        rec = self._choices.get(prompt)
        if rec is None:
            raise AnalystError("no recorded management choice for this state")
        if rec not in option_ids:
            raise AnalystError(f"recorded choice {rec!r} is not legal in this "
                               f"option set — state diverged, replay invalid")
        return rec


class DeterministicProvider(AnalystProvider):
    """ARM A. No model at all — the floor every intelligent arm must beat.

    Wraps the rule-based reader so the baseline travels the IDENTICAL LiveDesk
    path: same compiler, same router, same risk gate, same observer, same
    management, same ledger. If the baseline ran through a different harness the
    comparison would be measuring two codebases rather than one decision layer,
    and every incremental-value claim above it would be uninterpretable.

    It deliberately does NOT implement choose_option: a rule-based entry reader
    has no opinion about managing an open position, and pretending otherwise
    would silently give the baseline a capability it does not have.
    """

    name = "deterministic"
    model = "rules-v1"

    def __init__(self, inner: Any = None):
        if inner is None:
            from .runner import DeterministicAnalyst
            inner = DeterministicAnalyst()
        self._inner = inner

    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead:
        t0 = time.monotonic()
        r = self._inner.read(brief)
        return ProviderRead(r, self.name, self.model,
                            (time.monotonic() - t0) * 1000, {"in": 0, "out": 0})


PROVIDERS = {"anthropic": AnthropicAnalyst, "replay": ReplayAnalyst,
             "deterministic": DeterministicProvider}


def build_provider(spec: str, **kw) -> AnalystProvider:
    """spec = 'anthropic:claude-opus-5' or 'replay'. Vendor choice is config."""
    name, _, model = spec.partition(":")
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; have {sorted(PROVIDERS)}")
    if model:
        kw["model"] = model
    return PROVIDERS[name](**kw)
