"""Opus reached through the Claude Code CLI -- your subscription pays, not a metered key.

SAME PATTERN ALREADY PROVEN ON THE SIBLING DESK. `desks/mt5/mt5desk/analyst_rank.py`'s
`ClaudeCodeRanker` shells out to `claude -p`, strips `ANTHROPIC_API_KEY`/
`ANTHROPIC_AUTH_TOKEN` from the child process's environment so it falls back to the
CLI's own logged-in session rather than metered billing, and validates the reply
against a pydantic schema because `-p` has no server-enforced structured-output mode
-- only the raw Messages API does. This is that pattern, adapted to plug into
`analyst.py`'s `call_analyst(..., client=...)` seam instead of `rank()`.

`call_analyst`'s own docstring already names the seam: "SEAM 2: needs
ANTHROPIC_API_KEY (or an injected client)." This IS the injected client.

THE ENVELOPE, NOT THE RAW STDOUT. `claude -p --output-format json` prints a wrapper
object -- `is_error`, `subtype`, `permission_denials`, `result` -- and the model's own
text is `result`, which THEN needs unfencing before it is the JSON this desk's schema
expects. Two decode steps, not one; skipping the outer one silently tries to validate
the whole envelope against AnalystRead and fails confusingly.

VISION: the desk's own default is `Vision.NUMERIC_ONLY` (see live.py) -- no chart
images, `brief.render()` text only. This backend covers exactly that path, which is
what a normal run actually uses. `--allowed-tools ""` means the CLI has no Read
access to attach a file even if it wanted to, so if a caller ever passes chart
images (`Vision.WITH_CHARTS`) this REFUSES rather than silently dropping them or
guessing at an unverified image-attachment mechanism.

ONE-TIME SETUP, before this can run at all
    1. npm install -g @anthropic-ai/claude-code
    2. claude          (interactive once, to log in -- uses your Claude subscription)
    3. pip install anthropic     (analyst.py imports the package at module scope
       purely for its types; no key needed to import it, nothing billed by installing it)

USE
    from golddesk.analyst import call_analyst
    from golddesk.claude_code_analyst import ClaudeCodeAnalyst

    read = call_analyst(brief, client=ClaudeCodeAnalyst())   # charts=() only
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Usage:
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Response:
    """Just enough of `anthropic.types.Message`'s shape for `call_analyst`'s
    existing parsing to run UNCHANGED: `.stop_reason`, `.content[i].type/.text`,
    `.stop_details`, `.usage.*`. No new parsing path, no duplicated validation --
    the real `AnalystRead.model_validate_json(text)` call still does the work."""
    stop_reason: str
    content: list = field(default_factory=list)
    stop_details: Any = None
    usage: _Usage = field(default_factory=_Usage)


class _CLIError(RuntimeError):
    """Raised for backend failures (CLI missing, timeout, bad envelope). Deliberately
    NOT `AnalystError` -- that class belongs to analyst.py and importing it here would
    require `anthropic` to already be installed just to report that the CLI is
    missing, which is backwards for a path whose whole point is running without it."""


class _Messages:
    def __init__(self, parent: "ClaudeCodeAnalyst"):
        self._p = parent

    def create(self, *, model: str, max_tokens: int, system=None,
               output_config=None, messages, **_ignored) -> _Response:
        images_present = any(
            b.get("type") == "image" for b in messages[0]["content"])
        if images_present:
            raise _CLIError(
                "ClaudeCodeAnalyst was sent chart images, but -p --allowed-tools \"\" "
                "has no Read access to attach a file. Run with Vision.NUMERIC_ONLY "
                "(the desk's default) or use the hosted API path for vision.")

        brief_text = "\n\n".join(
            b["text"] for b in messages[0]["content"] if b.get("type") == "text")
        sys_text = "\n".join(
            b.get("text", "") for b in (system or []) if isinstance(b, dict))

        schema_note = ""
        if output_config and output_config.get("format", {}).get("type") == "json_schema":
            schema = output_config["format"]["schema"]
            schema_note = (
                "\n\nOutput ONLY a single JSON object, no prose, no markdown fence, "
                f"validating exactly against this schema:\n{json.dumps(schema)}")

        prompt = f"{brief_text}{schema_note}"
        envelope = self._p._invoke(sys_text, prompt, model)

        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            raise _CLIError(f"claude reported failure: {envelope.get('subtype')}")
        if envelope.get("permission_denials"):
            raise _CLIError(
                f"claude wanted a tool it does not have: {envelope['permission_denials']}")

        text = self._p._unfence(str(envelope.get("result") or ""))
        if not text:
            return _Response(stop_reason="refusal")
        return _Response(stop_reason="end_turn", content=[_Block(type="text", text=text)])


class ClaudeCodeAnalyst:
    """Drop into `call_analyst(..., client=ClaudeCodeAnalyst())`. That's the whole API."""

    def __init__(self, binary: str = "claude", timeout_s: float = 300.0,
                 billed: Optional[bool] = None, runner: Any = None):
        self.binary, self.timeout_s = binary, timeout_s
        self._billed, self._runner = billed, runner
        self.messages = _Messages(self)

    def billed(self) -> bool:
        if self._billed is not None:
            return self._billed
        return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())

    def _env(self) -> dict:
        env = dict(os.environ)
        if not self.billed():
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def _argv(self, system_prompt: str, model: str) -> list[str]:
        return [self.binary, "-p", "--output-format", "json",
                "--model", model, "--system-prompt", system_prompt,
                "--allowed-tools", "", "--max-turns", "1"]

    def _invoke(self, system_prompt: str, prompt: str, model: str) -> dict:
        if self._runner is not None:
            return self._runner(self._argv(system_prompt, model), prompt)
        try:
            p = subprocess.run(self._argv(system_prompt, model), input=prompt,
                               env=self._env(), capture_output=True, text=True,
                               timeout=self.timeout_s)
        except FileNotFoundError as e:
            raise _CLIError(
                f"{self.binary!r} not found. Install Claude Code (npm install -g "
                "@anthropic-ai/claude-code), run `claude` once to log in with your "
                "subscription, then retry.") from e
        except subprocess.TimeoutExpired as e:
            raise _CLIError(f"claude timed out after {self.timeout_s}s") from e
        if p.returncode != 0:
            raise _CLIError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:300]}")
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise _CLIError(f"claude did not return an envelope: {p.stdout[:300]!r}") from e

    @staticmethod
    def _unfence(t: str) -> str:
        s = t.strip()
        if not s.startswith("```"):
            return s
        s = s.split("\n", 1)[1] if "\n" in s else ""
        return s.rsplit("```", 1)[0].strip() if "```" in s else s.strip()


if __name__ == "__main__":
    # Smoke test: is the CLI on PATH, logged in, and does it answer at all.
    c = ClaudeCodeAnalyst()
    t0 = time.monotonic()
    env = c._invoke("Reply with exactly: CLAUDE CODE OK", "go", "claude-opus-5")
    print(f"({(time.monotonic() - t0) * 1000:.0f}ms) result:", env.get("result"))
