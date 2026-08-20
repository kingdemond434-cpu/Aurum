"""Local analyst provider -- Ollama, zero API key, nothing leaves this machine.

Plugs into the seam analyst.py already has: `call_analyst(..., client=...)` takes
anything with a `.messages.create(...)` method shaped like `anthropic.Anthropic()`.
This is that stand-in, backed by a local Ollama model instead of the hosted API.

WHAT THIS DOES NOT GIVE YOU

Anthropic's structured-output guarantee (`output_config.format.type=json_schema`)
is SERVER-ENFORCED -- the API will not return content that violates the schema.
Ollama's JSON mode is best-effort, not a guarantee, and a small local vision
model is meaningfully worse than Opus at what this task actually demands: read
structure, refuse when it should, pick levels BY ID rather than inventing a
price. Malformed output here still raises AnalystError exactly like a real
schema violation would -- the compiler's veto in analyst.py is unchanged
either way -- but expect more refusals and retries than the hosted model.

SETUP (Windows, one time, no billing anywhere)

    1. install Ollama: https://ollama.com/download/windows
    2. pull a vision model:   ollama pull qwen2.5vl:7b
       (needs ~8GB VRAM/RAM; qwen2.5vl:3b if the box is tighter)
    3. nothing else -- Ollama serves on localhost:11434 automatically after install

USE

    from golddesk.analyst import call_analyst
    from golddesk.local_analyst import OllamaClient

    read = call_analyst(brief, charts, client=OllamaClient())
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5vl:7b"


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
    """Just enough of `anthropic.types.Message`'s shape for analyst.py's
    existing parsing to run unmodified: `.stop_reason`, `.content[i].type/.text`,
    `.stop_details`, `.usage.*`."""
    stop_reason: str
    content: list = field(default_factory=list)
    stop_details: Any = None
    usage: _Usage = field(default_factory=_Usage)


class _Messages:
    def __init__(self, model: str, timeout: float):
        self.model, self.timeout = model, timeout

    def create(self, *, model: str, max_tokens: int, system=None,
               output_config=None, messages, **_ignored) -> _Response:
        # Anthropic's content blocks -> Ollama's shape: images are a flat
        # base64 list on the message, not typed blocks with a media_type.
        text_parts: list[str] = []
        images: list[str] = []
        for block in messages[0]["content"]:
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "image":
                images.append(block["source"]["data"])

        sys_text = ""
        if system:
            sys_text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": "\n\n".join(text_parts), "images": images},
            ],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        # Ollama's `format` field accepts a JSON schema directly (best-effort
        # constraint, not the server-enforced guarantee the hosted API gives).
        if output_config and output_config.get("format", {}).get("type") == "json_schema":
            payload["format"] = output_config["format"]["schema"]

        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=self.timeout)
            r.raise_for_status()
        except requests.ConnectionError as e:
            raise ConnectionError(
                "Ollama is not reachable at localhost:11434 -- is it running? "
                "It starts automatically after install; try `ollama serve` if not."
            ) from e

        out = r.json()
        text = out.get("message", {}).get("content", "")
        if not text:
            return _Response(stop_reason="refusal")
        return _Response(
            stop_reason="end_turn",
            content=[_Block(type="text", text=text)],
            usage=_Usage(input_tokens=out.get("prompt_eval_count", 0),
                        output_tokens=out.get("eval_count", 0)),
        )


class OllamaClient:
    """Drop into `call_analyst(..., client=OllamaClient())`. That's the whole API."""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 300.0):
        self.messages = _Messages(model, timeout)


if __name__ == "__main__":
    # Smoke test: is Ollama up, does the model answer at all.
    c = OllamaClient()
    resp = c.messages.create(
        model=DEFAULT_MODEL, max_tokens=100,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Reply with exactly: OLLAMA OK"}]}],
    )
    print("stop_reason:", resp.stop_reason)
    print("text:", resp.content[0].text if resp.content else "(empty)")
