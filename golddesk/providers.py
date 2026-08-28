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
import os
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

    def survey(self, brief: MarketBrief, charts: Sequence[Chart] = ()):
        """Every proposition available now, not just the best one.

        The default wraps read() into a one-candidate universe, so every
        provider — replay, deterministic, and any future vendor — works in
        universe mode without changes. That default is honest about itself: a
        single-read provider returns one candidate because that is what its
        interface can express, and the wrapper's survey text says so rather than
        letting a downstream reader conclude only one opportunity existed.

        A provider that can genuinely enumerate should override this. Returns
        (ProviderRead-like stamp, AnalystUniverse).
        """
        from .universe import as_universe
        pr = self.read(brief, charts)
        return pr, as_universe(pr.read)

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

    def survey(self, brief: MarketBrief, charts: Sequence[Chart] = ()):
        """A real enumeration, not a wrapped single read.

        Same cached system prefix as read(); the universe addendum is a second,
        uncached system block, so a universe run and a single-read run share the
        cache entry for everything before it.
        """
        import anthropic
        from .universe import (MAX_CANDIDATES, UNIVERSE_ADDENDUM, UNIVERSE_SCHEMA,
                               AnalystUniverse)
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
                model=self.model, max_tokens=self.max_tokens * 2,
                system=[{"type": "text", "text": ANALYST_SYSTEM,
                         "cache_control": {"type": "ephemeral"}},
                        {"type": "text",
                         "text": UNIVERSE_ADDENDUM.format(cap=MAX_CANDIDATES)}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema",
                                          "schema": UNIVERSE_SCHEMA}},
                messages=[{"role": "user", "content": content}])
        except anthropic.RateLimitError as e:
            raise AnalystError(f"rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise AnalystError(f"api {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AnalystError(f"connection: {e}") from e
        dt = (time.monotonic() - t0) * 1000

        if resp.stop_reason == "refusal":
            raise AnalystError("declined the universe request")
        if resp.stop_reason == "max_tokens":
            raise AnalystError("universe truncated — raise max_tokens or lower the cap")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise AnalystError("no text block")
        uni = AnalystUniverse.model_validate_json(text)
        # The stamp carries the FIRST candidate purely so provenance has a read
        # to attach to; selection does not privilege it in any way.
        head = uni.candidates[0] if uni.candidates else None
        stamp = ProviderRead(head, self.name, self.model, dt,
                             {"in": resp.usage.input_tokens,
                              "cache_read": resp.usage.cache_read_input_tokens,
                              "out": resp.usage.output_tokens,
                              "candidates": len(uni.candidates)})
        return stamp, uni

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


class ClaudeCodeAnalyst(AnalystProvider):
    """The same model, reached through the Claude Code CLI instead of the API.

    WHY THIS EXISTS. The metered API bills per token, and at this desk's
    configured cadence -- a 30-minute heartbeat with min_gap=0 on M15 bars --
    that is 46 to 92 reads a day, which prices out at roughly $290-580 a month
    against an account of EUR 1,500. budget.py's own docstring predicted this
    exactly: at a small account an analyst call can cost more than the trade
    makes. The CLI authenticates against a Pro/Max subscription, so the same
    read consumes subscription quota rather than dollars.

    WHAT IT COSTS INSTEAD, MEASURED NOT ASSUMED. Claude Code injects its own
    scaffolding: a trivial prompt with --system-prompt replacing the default
    and --allowed-tools disabled still reported 26,488 cache-creation tokens.
    That is free of DOLLARS under a subscription but it is not free of QUOTA,
    and it is the reason this provider does not make the heartbeat affordable
    so much as make it unnecessary -- three session-anchored reads a day is
    ~80k tokens of overhead, ninety-two is 2.4M.

    WHAT IT CANNOT DO. The CLI has no structured-output mode, so the schema is
    requested in the prompt and enforced here by validation; a read that does
    not parse is an error, never a shrug. It also has no image input on stdin,
    so `charts` RAISES rather than being quietly dropped -- a chart arm that
    silently ran without charts would make competition.py's paired comparison
    and budget.py's "does the chart arm pay for itself" question return
    confident fiction.
    """

    name = "claudecode"

    #: Requested in-band because the CLI cannot enforce a JSON schema. The
    #: model is then held to it by model_validate_json, which is the only part
    #: that actually guarantees anything.
    _SCHEMA_INSTRUCTION = (
        "Return ONLY a single JSON object conforming to this JSON Schema. No "
        "prose, no explanation, no markdown code fences.\n\nSCHEMA:\n{schema}\n")

    #: Matches AnthropicAnalyst's own accepted values exactly -- confirmed
    #: against `claude --help`, which documents this literal set for its own
    #: --effort flag. A value neither side accepts is not a case worth coding
    #: for defensively; it is caught by choices= on the argparse flag that
    #: feeds this, the same way an invalid --management value already is.
    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    #: 240s was measured too tight on 2026-08-26: the live desk logged "claude timed out after
    #: 240.0s" on repeated reads (22:00, 23:32), each one discarded as a refusal. A reasoning
    #: model asked for a full structured read legitimately spends minutes, so the old bound was
    #: cutting off sound work rather than catching a hang.
    #:
    #: THE CEILING IS THE READ CADENCE, NOT COMFORT. Observed live gaps between reads ran ~15
    #: to ~75 minutes, so the shortest real gap is about 15 minutes; a timeout at or above that
    #: would let one slow call still be running when the next bar's read begins, which is the
    #: failure this bound exists to prevent. 600s keeps a comfortable margin under that floor
    #: while giving 2.5x the room -- a call still running at ten minutes is genuinely wedged.
    DEFAULT_TIMEOUT_S = 600.0

    #: The degraded retry. `low` is the floor of EFFORTS, and 240s was the OLD full budget --
    #: known from production to be enough for many reads, so it is a measured fallback rather
    #: than a guessed one. 600 + 240 = 840s worst case, inside the 900s M15 bar that bounds the
    #: whole exchange. Raising either number without re-checking that sum reintroduces the
    #: backlog the bar bound exists to prevent.
    FALLBACK_EFFORT = "low"
    FALLBACK_TIMEOUT_S = 240.0

    def __init__(self, model: str = "claude-opus-5", binary: str = "claude",
                 timeout_s: float = DEFAULT_TIMEOUT_S, cwd: Optional[str] = None,
                 billed: Optional[bool] = None, runner: Any = None,
                 effort: Optional[str] = None):
        self.model, self.binary, self.timeout_s = model, binary, timeout_s
        self.cwd, self._billed, self._runner = cwd, billed, runner
        self.effort = effort

    def billed(self) -> bool:
        """Whether these tokens are being charged in dollars.

        HEURISTIC, AND LABELLED AS ONE. Claude Code bills against a metered API
        key when one is present and against the subscription otherwise, and it
        does not report which it used. So this reads the environment and the
        operator can override it. It matters because budget.py prices tokens at
        API list: reporting real dollars for a subscription read would overstate
        cost, and reporting zero for an API-key read would hide it. Guessing
        silently in either direction corrupts every net-value number downstream.
        """
        if self._billed is not None:
            return self._billed
        return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())

    @staticmethod
    def _repair(text: str) -> tuple[str, list[str]]:
        """Bounded repair of the two schema violations actually observed in production.

        WHY THIS IS NOT "BEING LENIENT WITH THE MODEL". Both repairs are provably incapable of
        changing what the desk would do, because of how AnalystRead is shaped: every field
        carrying a DECISION -- setup, direction, entry_ref, stop_ref, tp1_ref, tp2_ref,
        confidence -- is uncapped and is never touched here. Every field that IS length-capped
        is prose commentary. So a truncated `why` loses the tail of an explanation and cannot
        turn a NONE into a LONG; a missing or malformed decision field still fails validation
        exactly as before, which is the property that matters.

        The two repairs:

          - EXTRA FIELDS ARE DROPPED. `extra: "forbid"` exists on AnalystRead specifically to
            keep price fields out of the model's output surface ("No price fields exist here by
            design"), so a model inventing `entry_price` is a violation whose correct handling
            is to discard it -- which is what the config already intends. Dropping it enforces
            the schema rather than bypassing it.
          - OVER-LONG PROSE IS TRUNCATED to the model's own declared cap.

        Caps and the allowed key set are READ OFF AnalystRead rather than restated, so this
        cannot drift into a second copy of the schema: change a Field(max_length=...) and this
        follows automatically.

        Returns (possibly-rewritten JSON text, human-readable list of repairs made). An empty
        repair list means nothing here applied and the caller must surface the original error --
        silence would turn a genuine schema failure into a shrug.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text, []                       # not JSON at all; not this function's problem
        if not isinstance(data, dict):
            return text, []

        allowed = set(AnalystRead.model_fields)
        caps: dict[str, int] = {}
        for fname, finfo in AnalystRead.model_fields.items():
            for meta in finfo.metadata:
                cap = getattr(meta, "max_length", None)
                if cap is not None:
                    caps[fname] = cap

        repairs: list[str] = []
        for key in [k for k in data if k not in allowed]:
            data.pop(key)
            repairs.append(f"dropped forbidden extra field {key!r}")
        for fname, cap in caps.items():
            value = data.get(fname)
            if isinstance(value, str) and len(value) > cap:
                data[fname] = value[:cap]
                repairs.append(f"truncated {fname} {len(value)}->{cap} chars")
        if not repairs:
            return text, []
        return json.dumps(data), repairs

    @staticmethod
    def _unfence(text: str) -> str:
        """Strip a markdown code fence if the model wrapped its JSON in one.

        Not defensive programming -- observed. Asked for bare JSON with an
        explicit "no markdown code fences", the CLI still returned
        ```json\\n{...}\\n```. The instruction reduces it; it does not remove it.
        """
        s = text.strip()
        if not s.startswith("```"):
            return s
        s = s.split("\n", 1)[1] if "\n" in s else ""
        return s.rsplit("```", 1)[0].strip() if "```" in s else s.strip()

    #: Characters of system prompt above which it is moved OFF the command line
    #: and into stdin.
    #:
    #: WHY THIS EXISTS. `--system-prompt` is passed as a single argv element, and
    #: on Windows a launcher shim that re-invokes through cmd.exe truncates the
    #: command line at 8,191 characters. Over that, the CLI fails LOCALLY: exit
    #: 1, is_error true, duration_api_ms 0, zero input and output tokens -- it
    #: never reaches the API, so it looks like neither a rate limit nor an
    #: outage nor a timeout.
    #:
    #: OBSERVED 2026-08-27/28. universe_system() is 9,098 chars against a
    #: single-read ANALYST_SYSTEM of 7,226, so the desk answered normally on the
    #: single-read path and went blind on EVERY survey -- which is exactly the
    #: shape the ledger showed: every failure carrying stage "survey", 59
    #: successful reads at a healthy 115s median in the same window.
    #:
    #: 7,900 is chosen to sit BETWEEN the two real prompts, not at a round
    #: number: single-read is 7,226 and stays on argv, universe is 9,098 and does
    #: not. That matters because the single-read path is currently WORKING and
    #: producing evidence -- moving it too would silently change how a working
    #: arm addresses the model, and a change to the arm must be deliberate.
    #:
    #: The rest of argv (binary path, model, flags) is ~130 chars on the live
    #: box, so 7,900 leaves ~160 of margin inside the 8,191 budget. If
    #: ANALYST_SYSTEM grows past this the single-read path flips to stdin too --
    #: automatically, loudly, and still working, which is the entire point.
    MAX_SYSTEM_ARGV_CHARS = 7900

    def _argv(self, system: Optional[str] = None,
              effort: Optional[str] = None) -> list[str]:
        """`effort` overrides self.effort for ONE call -- used by the degraded retry below."""
        sys_text = system or ANALYST_SYSTEM
        if len(sys_text) > self.MAX_SYSTEM_ARGV_CHARS:
            # Signalled by omitting the flag; _invoke prepends the text to stdin.
            sys_text = None
        argv = [self.binary, "-p",
                "--output-format", "json",
                "--model", self.model,
                "--allowed-tools", "",
                "--max-turns", "1"]
        if sys_text is not None:
            argv += ["--system-prompt", sys_text]
        chosen = effort if effort is not None else self.effort
        if chosen is not None:
            if chosen not in self.EFFORTS:
                raise AnalystError(
                    f"effort {chosen!r} not in {self.EFFORTS} -- the CLI "
                    f"would reject it too, but failing here names the actual "
                    f"problem instead of an opaque nonzero exit from claude")
            argv += ["--effort", chosen]
        return argv

    def _env(self) -> dict:
        """The child's environment.

        When the operator has declared this UNBILLED, the API key is actively
        REMOVED rather than merely unused. A key sitting in the environment is
        how a desk configured for a subscription quietly runs up a metered bill,
        and the failure is invisible until the invoice.
        """
        env = dict(os.environ)
        if not self.billed():
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def _invoke(self, prompt: str, system: Optional[str] = None,
                effort: Optional[str] = None, timeout_s: Optional[float] = None) -> dict:
        import subprocess
        budget = self.timeout_s if timeout_s is None else timeout_s
        try:
            # THE INJECTED TRANSPORT IS INSIDE THE TRY ON PURPOSE. It used to sit above it, which
            # meant the degrade-on-timeout policy below existed only on the subprocess path and
            # could not be exercised by any test -- retry policy silently coupled to transport.
            # A test runner that raises TimeoutExpired now travels the same path production does.
            argv = self._argv(system, effort)
            sys_text = system or ANALYST_SYSTEM
            if "--system-prompt" not in argv:
                # TOO LONG FOR THE COMMAND LINE. Carried in stdin instead, which
                # is unbounded. Logged at WARNING every time: a transport that
                # silently changes how the model is addressed is a change to the
                # arm, and it must be visible in the log rather than inferred
                # later from a shift in behaviour.
                log.warning("system prompt is %d chars, over the %d-char argv "
                            "budget -- sending it in stdin instead. The model "
                            "sees the same text; only the transport changes.",
                            len(sys_text), self.MAX_SYSTEM_ARGV_CHARS)
                prompt = f"{sys_text}\n\n---\n\n{prompt}"
            if self._runner is not None:
                return self._runner(argv, prompt)
            p = subprocess.run(argv, input=prompt, env=self._env(),
                               cwd=self.cwd, capture_output=True, text=True,
                               timeout=budget)
        except FileNotFoundError as e:
            raise AnalystError(
                f"{self.binary!r} not found. Install Claude Code on this box and "
                f"log in once interactively, or use provider 'anthropic:' and "
                f"pay per token.") from e
        except subprocess.TimeoutExpired as e:
            # DEGRADE, DO NOT SURRENDER. A timeout discards the whole read and books a refusal,
            # so the desk's answer to "this one was slow" was to learn nothing from that bar at
            # all -- 6 of the last 10 failures on 2026-08-26 were exactly this. A completed
            # low-effort read is strictly better evidence than no read, so the retry trades
            # depth of reasoning for an answer that actually arrives, rather than trading the
            # answer away.
            #
            # ONE retry, at the floor effort, on a SHORTER budget. The bound that matters is the
            # bar: at M15 a read must finish inside 900s or it is still running when the next
            # bar's read begins, and the backlog compounds. self.timeout_s (600) plus
            # FALLBACK_TIMEOUT_S (240) is 840s worst case, which fits with margin. Retrying at
            # the same effort would just spend the budget twice on the thing that already proved
            # too slow, and a second full-length attempt would breach the bar outright.
            if effort == self.FALLBACK_EFFORT:
                raise AnalystError(f"claude timed out after {budget}s at "
                                   f"{self.FALLBACK_EFFORT} effort -- the floor arm could not "
                                   f"finish either, so this is latency on the box or the "
                                   f"venue, not reasoning depth") from e
            log.warning("claude timed out after %ss; retrying once at %s effort within %ss",
                        budget, self.FALLBACK_EFFORT, self.FALLBACK_TIMEOUT_S)
            return self._invoke(prompt, system, effort=self.FALLBACK_EFFORT,
                                timeout_s=self.FALLBACK_TIMEOUT_S)
        if p.returncode != 0:
            # 300 chars was cutting off every failure at almost exactly the point where the
            # CLI's own JSON error payload names the actual problem -- every "analyst
            # unavailable" line in production ended with "output_tokens": and nothing after,
            # for days, because the field that would have explained the failure sits past that
            # cutoff. 2000 chars comfortably covers the CLI's error JSON without risking an
            # unbounded log line from a truly pathological response.
            raise AnalystError(f"claude exited {p.returncode}: "
                               f"{(p.stderr or p.stdout)[:2000]}")
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise AnalystError(f"claude did not return JSON: "
                               f"{p.stdout[:300]!r}") from e

    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead:
        if charts:
            raise AnalystError(
                f"{self.name!r} cannot send charts: the CLI takes no image input. "
                f"Run the chart arm on provider 'anthropic:' or pass no charts — "
                f"silently dropping them would make every chart-vs-text "
                f"comparison meaningless.")
        prompt = (self._SCHEMA_INSTRUCTION.format(
            schema=json.dumps(ANALYST_SCHEMA)) + "\n" + brief.render())

        t0 = time.monotonic()
        env = self._invoke(prompt)
        dt = (time.monotonic() - t0) * 1000

        if env.get("is_error") or env.get("subtype") not in (None, "success"):
            raise AnalystError(f"claude reported failure: "
                               f"{env.get('subtype')} {str(env.get('result'))[:200]}")
        if env.get("permission_denials"):
            raise AnalystError(f"claude was denied a permission it wanted: "
                               f"{env['permission_denials']}")
        text = self._unfence(str(env.get("result") or ""))
        if not text:
            raise AnalystError("claude returned an empty result")
        try:
            read = AnalystRead.model_validate_json(text)
        except Exception as strict_err:                          # noqa: BLE001
            # A CLEAN READ STAYS CLEAN: strict validation is tried first and unchanged, so this
            # path only runs on output that already failed. Observed live 2026-08-26: the desk
            # logged "analyst unavailable ... N validation errors" on read after read, refused
            # every one, and looked perfectly healthy doing it (preflight PASS, Telegram PASS,
            # process up) because a refusal is a legitimate outcome. Discarding a sound read
            # over an over-long `why` is a formatting failure wearing a judgement's clothes.
            repaired, repairs = self._repair(text)
            if not repairs:
                raise AnalystError(f"claude returned text that is not a valid "
                                   f"AnalystRead: {strict_err}; got {text[:300]!r}"
                                   ) from strict_err
            try:
                read = AnalystRead.model_validate_json(repaired)
            except Exception as e:                               # noqa: BLE001
                raise AnalystError(f"claude returned text that is not a valid AnalystRead "
                                   f"even after repair ({'; '.join(repairs)}): {e}; "
                                   f"got {text[:300]!r}") from e
            # LOUD ON PURPOSE. The repair is safe but it is not free: a truncated field means
            # the model wrote more than the schema allows, and a persistent stream of these is
            # a prompt/schema mismatch to fix at the source, not a condition to normalise.
            log.warning("analyst read accepted after repair (%s)", "; ".join(repairs))

        u = env.get("usage") or {}
        fresh = int(u.get("input_tokens") or 0)
        created = int(u.get("cache_creation_input_tokens") or 0)
        cached = int(u.get("cache_read_input_tokens") or 0)
        billed = self.billed()
        return ProviderRead(read, self.name, self.model, dt, {
            # budget.Pricing expects `in` to include cache reads and derives
            # fresh by subtraction. Cache CREATION is folded into fresh: it is
            # actually dearer than fresh input, so this understates rather than
            # flatters, which is the right direction for a cost estimate.
            "in": fresh + created + cached,
            "cache_read": cached,
            "out": int(u.get("output_tokens") or 0),
            # The CLI always reports API-equivalent dollars. Under a
            # subscription nobody is charged them, so they are labelled rather
            # than passed off as spend.
            "billed": billed,
            "cost_usd_api_equivalent": env.get("total_cost_usd"),
            "cost_usd": env.get("total_cost_usd") if billed else 0.0,
        })


    def survey(self, brief: MarketBrief, charts: Sequence[Chart] = ()):
        """A REAL enumeration through the CLI, not a wrapped single read.

        WHY THIS OVERRIDE HAD TO EXIST. Without it this provider inherited
        AnalystProvider.survey, which calls read() once and wraps the answer in
        a one-candidate universe. That default is honest -- it says a
        single-read provider returns one candidate because that is what its
        interface can express -- but the consequence was that `--universe` on
        this provider changed the LABEL and nothing else. An operator enabling
        universe mode to widen capture would have got the same single read,
        reported as a universe, with no error anywhere.

        WHY IT IS THE HIGHEST-VALUE CHANGE PER UNIT OF QUOTA. A subscription
        meters TOKENS, and the expensive part of a read is the brief -- levels,
        macro, context, timeline -- which is sent whether the model returns one
        proposition or twelve. Asking for the full set costs one extra request's
        worth of OUTPUT and reuses the whole input, so it raises capture without
        raising call frequency. On a plan with a weekly ceiling that is strictly
        better than more calls.

        Same system prefix as read(), with the universe addendum appended, so
        the two modes stay comparable rather than becoming different prompts
        that happen to share a name.
        """
        from .universe import (MAX_CANDIDATES, AnalystUniverse,  # noqa: PLC0415
                               UNIVERSE_SCHEMA, universe_system)
        if charts:
            raise AnalystError(
                f"{self.name!r} cannot send charts: the CLI takes no image input. "
                f"Run the chart arm on provider 'anthropic:'.")

        prompt = (self._SCHEMA_INSTRUCTION.format(
            schema=json.dumps(UNIVERSE_SCHEMA)) + "\n" + brief.render())

        t0 = time.monotonic()
        env = self._invoke(prompt, system=universe_system(ANALYST_SYSTEM,
                                                          MAX_CANDIDATES))
        dt = (time.monotonic() - t0) * 1000

        if env.get("is_error") or env.get("subtype") not in (None, "success"):
            raise AnalystError(f"claude reported failure: "
                               f"{env.get('subtype')} {str(env.get('result'))[:200]}")
        text = self._unfence(str(env.get("result") or ""))
        if not text:
            raise AnalystError("claude returned an empty result")
        try:
            uni = AnalystUniverse.model_validate_json(text)
        except Exception as e:                                   # noqa: BLE001
            # NOT silently downgraded to a single read. A universe run that
            # quietly becomes a one-candidate answer is the same defect this
            # override exists to remove, one layer deeper.
            raise AnalystError(f"claude returned text that is not a valid "
                               f"AnalystUniverse: {e}; got {text[:300]!r}") from e

        u = env.get("usage") or {}
        fresh = int(u.get("input_tokens") or 0)
        created = int(u.get("cache_creation_input_tokens") or 0)
        cached = int(u.get("cache_read_input_tokens") or 0)
        billed = self.billed()
        # The stamp carries the FIRST candidate purely so provenance has a read
        # to attach to; selection does not privilege it in any way. Matches
        # AnthropicAnalyst.survey exactly, so the two providers' universe runs
        # stay comparable rather than differing in how they report.
        head = uni.candidates[0] if uni.candidates else None
        pr = ProviderRead(head, self.name, self.model, dt, {
            "in": fresh + created + cached,
            "cache_read": cached,
            "out": int(u.get("output_tokens") or 0),
            "billed": billed,
            "cost_usd_api_equivalent": env.get("total_cost_usd"),
            "cost_usd": env.get("total_cost_usd") if billed else 0.0,
            "candidates": len(uni.candidates),
        })
        return pr, uni


PROVIDERS = {"anthropic": AnthropicAnalyst, "replay": ReplayAnalyst,
             "deterministic": DeterministicProvider,
             "claudecode": ClaudeCodeAnalyst}


def build_provider(spec: str, **kw) -> AnalystProvider:
    """spec = 'anthropic:claude-opus-5', 'claudecode:claude-opus-5', or 'replay'.

    'claudecode' routes the SAME AnthropicAnalyst through a Claude Code CLI
    client instead of the metered API -- your subscription pays, nothing else
    about the desk changes. Vendor choice is config either way.
    """
    name, _, model = spec.partition(":")
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; have {sorted(PROVIDERS)}")
    if model:
        kw["model"] = model
    try:
        return PROVIDERS[name](**kw)
    except TypeError as e:
        # Most likely --effort against 'deterministic' or 'replay', neither of
        # which reasons about anything and so has nothing to apply effort to.
        # A raw TypeError from here reads as an internal bug; naming the
        # actual mismatched argument is the same courtesy every other
        # preflight refusal in this desk already gives.
        raise ValueError(
            f"provider {name!r} does not accept one of these arguments: "
            f"{sorted(kw)}. Original error: {e}") from e
