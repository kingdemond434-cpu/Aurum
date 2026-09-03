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

VISION, AND THE BUG THAT MADE THIS PROVIDER LOOK BROKEN. `run_desk.py` defaults to
`Vision.NUMERIC_PLUS_CHARTS`, not NUMERIC_ONLY -- charts are ON unless you pass
--numeric-only. This backend used to RAISE on any image, so a desk configured with
--provider claudecode refused every single read before it ever reached the CLI, fell
through to the codex fallback, and recorded the FALLBACK's error. That is how 1,030
bars went BLIND against "codex exited 1" on a box that was correctly logged in: the
symptom named the wrong provider entirely.

The CLI takes no image on stdin, so charts are now spilled to a temp directory and
read back with `--allowed-tools Read --add-dir <tmp>`. Read is the only tool granted,
the temp directory the only place it reaches, and the turn budget covers one
round-trip per image. The numeric path is unchanged: zero tools, one turn.

A chart read is only BOOKED as one if the envelope shows the tool round-trip
happened. Answering in one turn means the model never opened the files, and a chart
arm that silently ran on text would make competition.py's paired comparison and
budget.py's "does the chart arm pay for itself" question return confident fiction.

ONE-TIME SETUP, before this can run at all
    1. npm install -g @anthropic-ai/claude-code
    2. claude          (interactive once, to log in -- uses your Claude subscription)
    3. pip install anthropic     (analyst.py imports the package at module scope
       purely for its types; no key needed to import it, nothing billed by installing it)

USE
    from golddesk.analyst import call_analyst
    from golddesk.claude_code_analyst import ClaudeCodeAnalyst

    read = call_analyst(brief, client=ClaudeCodeAnalyst())   # charts supported
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
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

    @staticmethod
    def _spill(images: list, into: Path) -> list:
        """Write each base64 image block to a real file. Returns their paths.

        A block whose payload does not decode is dropped rather than written as
        a corrupt PNG the model would silently fail to open — but the count of
        written files is what the turn check below is measured against, so a
        dropped image cannot masquerade as a read one.
        """
        out = []
        for i, blk in enumerate(images):
            src = blk.get("source") or {}
            if src.get("type") != "base64" or not src.get("data"):
                continue
            ext = str(src.get("media_type", "image/png")).partition("/")[2] or "png"
            p = into / f"chart{i}.{ext}"
            try:
                p.write_bytes(base64.b64decode(src["data"]))
            except (ValueError, TypeError, OSError):
                continue
            out.append(p)
        return out

    @staticmethod
    def _chart_note(paths: list) -> str:
        if not paths:
            return ""
        listing = "\n".join(f"  {p}" for p in paths)
        return ("\n\nCHARTS. Read every one of these image files before you answer. "
                "They are the visual half of this read and the numeric brief above "
                "is not a substitute for them:\n" + listing)

    def create(self, *, model: str, max_tokens: int, system=None,
               output_config=None, messages, **_ignored) -> _Response:
        images = [b for b in messages[0]["content"] if b.get("type") == "image"]

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

        # CHARTS. The CLI takes no image on stdin, so they are spilled to a temp
        # directory and READ back through the one tool this call is allowed. That
        # is the whole reason the tool surface is not empty on this path.
        #
        # This used to raise instead, which is why a subscription desk running
        # the default Vision.NUMERIC_PLUS_CHARTS refused 100% of reads before it
        # ever reached the CLI — not a login problem, not a quota problem, a
        # payload the provider could not accept.
        with tempfile.TemporaryDirectory(prefix="aurum-charts-") as tmp:
            paths = self._spill(images, Path(tmp))
            prompt = f"{brief_text}{self._chart_note(paths)}{schema_note}"
            envelope = self._p._invoke(sys_text, prompt, model,
                                       read_dir=(tmp if paths else None),
                                       n_images=len(paths))

        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            raise _CLIError(f"claude reported failure: {envelope.get('subtype')}")
        if envelope.get("permission_denials"):
            raise _CLIError(
                f"claude wanted a tool it does not have: {envelope['permission_denials']}")

        # A CHART ARM THAT RAN WITHOUT CHARTS IS WORSE THAN ONE THAT FAILED. It
        # would make competition.py's paired comparison and budget.py's "does the
        # chart arm pay for itself" question return confident fiction, so the
        # read is only trusted if the transcript shows the tool round-trip that
        # reading an image requires. One turn means it answered from the text
        # alone. An envelope that does not report turns at all cannot be checked,
        # and unverifiable is not verified.
        if images:
            turns = envelope.get("num_turns")
            if turns is None:
                raise _CLIError(
                    "charts were sent but the envelope reports no num_turns, so "
                    "there is no evidence the CLI read them. Refusing rather than "
                    "booking a chart read that may have been text-only; run with "
                    "--numeric-only to use this provider without charts.")
            if int(turns) <= 1:
                raise _CLIError(
                    f"charts were sent but the CLI answered in {turns} turn(s), so "
                    "it never opened them. The read would be text-only wearing a "
                    "chart arm's label.")

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

    def _argv(self, system_prompt: str, model: str, read_dir: Optional[str] = None,
              n_images: int = 0) -> list[str]:
        """The numeric path keeps ZERO tools and one turn; only charts widen it.

        `read_dir` grants Read, and only Read, and only inside the temp directory
        the charts were spilled to — never the repository, never the network. The
        turn budget has to cover one round-trip per image plus the final answer,
        because a Read that runs out of turns returns a text-only read wearing a
        chart arm's label.
        """
        argv = [self.binary, "-p", "--output-format", "json",
                "--model", model, "--system-prompt", system_prompt]
        if read_dir:
            argv += ["--allowed-tools", "Read", "--add-dir", read_dir,
                     "--max-turns", str(2 + n_images)]
        else:
            argv += ["--allowed-tools", "", "--max-turns", "1"]
        return argv

    def _invoke(self, system_prompt: str, prompt: str, model: str,
                read_dir: Optional[str] = None, n_images: int = 0) -> dict:
        argv = self._argv(system_prompt, model, read_dir, n_images)
        if self._runner is not None:
            return self._runner(argv, prompt)
        try:
            p = subprocess.run(argv, input=prompt,
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
