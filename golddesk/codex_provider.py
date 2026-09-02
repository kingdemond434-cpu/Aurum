"""A second brain on the same box, for when the first one runs out of allowance.

WHAT THIS IS FOR. The desk goes blind for reasons that have nothing to do with
the market: a subscription session limit, an expired login, a provider outage, a
timeout. Observed live, more than once, and the desk's honest response was to
record BLIND and produce no signal. That is the correct answer when there is no
brain available — it is the wrong answer when there is a perfectly good one
installed on the same machine and nothing was asking it.

So this is an analyst provider that talks to a locally installed Codex CLI
through `codex exec`, the non-interactive path. It is a peer of the Claude CLI
provider, not a lesser one: it receives the SAME frozen brief, is held to the
SAME AnalystRead schema, and its answer goes through the SAME deterministic
compiler, the same cost model, the same expectancy gate and the same risk
checks. The compiler does not know or care which brain produced the read, and
that property is the reason this is safe to add.

THREE THINGS IT DELIBERATELY DOES NOT DO

  IT DOES NOT WRITE. Invoked as an analyst, Codex is given a read-only sandbox
  and no approvals. It reads a brief and returns JSON. An agentic coding tool
  pointed at a live trading desk with write access is a category of risk this
  desk has no reason to accept in exchange for a market read.

  IT DOES NOT INVENT A MODEL. There is no default model name in this file. The
  model is configuration -- AURUM_CODEX_MODEL, or the `model` argument -- and
  when neither is set no --model flag is passed at all and the CLI's own default
  is used. A model identifier hardcoded from memory is a claim about a vendor's
  catalogue that goes stale silently and is wrong in a way nothing tests.

  IT DOES NOT PRETEND TO SEE CHARTS. Whether this CLI accepts image input is a
  question about a binary that may not be installed here, so charts raise unless
  an image flag has been configured explicitly. Silently dropping charts would
  make every chart-versus-text comparison in the desk's record meaningless, and
  the Claude CLI provider already refuses on exactly this ground.

WHAT IS SHARED WITH THE CLAUDE CLI PROVIDER, ON PURPOSE. The JSON unfencing and
the schema repair are imported from it rather than reimplemented. Two copies of
a parser drift, and the day they disagree one brain's reads get accepted and the
other's get thrown away for a reason nobody can see in the ledger.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Optional, Sequence

from .analyst import ANALYST_SCHEMA, AnalystRead, MarketBrief
from .chart import Chart
from .providers import (AnalystError, AnalystProvider, ClaudeCodeAnalyst,
                        ProviderRead)

log = logging.getLogger(__name__)

CODEX_PROVIDER_VERSION = "codex-2026-08-29-a"

#: Env override for the binary, for a box where it is not on PATH.
BINARY_ENV = "AURUM_CODEX_BINARY"
#: Env override for the model. NO DEFAULT, deliberately — see the module note.
MODEL_ENV = "AURUM_CODEX_MODEL"
#: Optional flag used to attach an image, e.g. "-i". Unset means charts are
#: refused rather than silently dropped.
IMAGE_FLAG_ENV = "AURUM_CODEX_IMAGE_FLAG"

#: Seconds. Generous: this runs only when the primary is already unavailable, so
#: the alternative to waiting is no signal at all.
DEFAULT_TIMEOUT = 300.0

SCHEMA_INSTRUCTION = (
    "You are a market analyst. Reply with ONE JSON object and nothing else — no "
    "prose before or after, no code fence. It must validate against this schema:\n"
    "{schema}\n\n"
    "You do not choose prices. You cite LEVEL IDS from the table below and the "
    "desk's compiler resolves them; a price you invent will be discarded and the "
    "read refused. `why_not` is compulsory and must state the strongest case "
    "AGAINST your own read.\n")


class CodexLocalAnalyst(AnalystProvider):
    """The Codex CLI on this machine, doing the analyst's job and nothing else."""

    name = "codex_local"

    def __init__(self, model: str = "", binary: str = "",
                 cwd: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
                 runner=None, image_flag: str = ""):
        self.model = model or os.environ.get(MODEL_ENV, "").strip()
        self.binary = binary or os.environ.get(BINARY_ENV, "").strip() or "codex"
        self.cwd = cwd
        self.timeout = float(timeout)
        self.image_flag = image_flag or os.environ.get(IMAGE_FLAG_ENV, "").strip()
        #: Injected transport for tests. Same seam the Claude CLI provider uses:
        #: policy is testable without a binary, and a test that stubs the binary
        #: is testing the stub.
        self._runner = runner

    # ---------------------------------------------------------------- probing

    def available(self) -> tuple[bool, str]:
        """Is this brain actually installed? Answered before it is relied on.

        A failover chain whose last link is a binary that does not exist is not
        a failover chain; it is the same blindness with more steps. The desk's
        own audit should be able to say which brains are really on the box.
        """
        if self._runner is not None:
            return True, "injected runner"
        path = shutil.which(self.binary)
        if not path:
            return False, (f"{self.binary!r} is not on PATH — install the Codex "
                           f"CLI or set {BINARY_ENV}")
        return True, path

    def describe(self) -> dict:
        ok, why = self.available()
        return {"provider": self.name, "model": self.model or "(cli default)",
                "available": ok, "basis": why, "version": CODEX_PROVIDER_VERSION}

    # ------------------------------------------------------------- transport

    def _argv(self, prompt_file: Optional[str] = None) -> list[str]:
        argv = [self.binary, "exec"]
        if self.model:
            argv += ["--model", self.model]
        # READ-ONLY, NO APPROVALS. An analyst has no reason to touch the disk,
        # and a coding agent that can is not something to point at a live desk
        # in exchange for a market read. Both flags are passed; a CLI that
        # rejects one is a CLI this provider must not be used with, and the
        # error says so rather than being retried without the guard.
        argv += ["--sandbox", "read-only", "--skip-git-repo-check"]
        return argv

    def _run(self, argv: list[str], prompt: str) -> tuple[int, str, str]:
        """TRANSPORT ONLY. Returns (returncode, stdout, stderr); decides nothing."""
        if self._runner is not None:
            out = self._runner(argv, prompt)
            if isinstance(out, tuple):
                rc, so, se = out
                return int(rc), so or "", se or ""
            return 0, str(out), ""
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                           timeout=self.timeout, cwd=self.cwd,
                           env={**os.environ, "PYTHONUTF8": "1"})
        return p.returncode, p.stdout or "", p.stderr or ""

    # ------------------------------------------------------------------ read

    def read(self, brief: MarketBrief, charts: Sequence[Chart] = ()) -> ProviderRead:
        if charts and not self.image_flag:
            raise AnalystError(
                f"{self.name!r} was given {len(charts)} chart(s) and no image "
                f"flag is configured ({IMAGE_FLAG_ENV}). Refusing rather than "
                f"dropping them: a text-only read recorded as a chart read makes "
                f"every chart-versus-text comparison in the ledger meaningless.")
        ok, why = self.available()
        if not ok:
            raise AnalystError(f"{self.name!r} unavailable: {why}")

        prompt = (SCHEMA_INSTRUCTION.format(schema=json.dumps(ANALYST_SCHEMA))
                  + "\n" + brief.render())
        t0 = time.monotonic()
        try:
            rc, out, err = self._run(self._argv(), prompt)
        except subprocess.TimeoutExpired as e:
            raise AnalystError(f"{self.name!r} timed out after {self.timeout}s") from e
        except OSError as e:
            raise AnalystError(f"{self.name!r} could not start: {e}") from e
        dt = (time.monotonic() - t0) * 1000

        if rc != 0:
            raise AnalystError(f"{self.name!r} exited {rc}: "
                               f"{(err or out).strip()[:300]!r}")
        text = ClaudeCodeAnalyst._unfence(out.strip())
        if not text:
            raise AnalystError(f"{self.name!r} returned nothing")
        read = self._parse(text)
        return ProviderRead(read, self.name, self.model or "cli-default", dt, {
            # NO TOKEN ACCOUNTING CLAIMED. The CLI's stdout is the answer, not a
            # billing envelope, and inventing zeros here would let budget.py
            # report a spend of nothing for work that was really done. Absent is
            # the honest value; a fabricated zero is not.
            "billing_basis": "unmeasured_cli",
        })

    @staticmethod
    def _parse(text: str) -> AnalystRead:
        """Strict first, then the SAME repair the other CLI provider uses.

        A clean read stays clean: repair only runs on output that already failed
        validation, and it is logged loudly when it does — a stream of repairs
        is a schema mismatch to fix at the source, not a condition to normalise.
        """
        try:
            return AnalystRead.model_validate_json(text)
        except Exception as strict_err:                          # noqa: BLE001
            repaired, repairs = ClaudeCodeAnalyst._repair(text)
            if not repairs:
                raise AnalystError(f"codex returned text that is not a valid "
                                   f"AnalystRead: {strict_err}; got {text[:300]!r}"
                                   ) from strict_err
            try:
                out = AnalystRead.model_validate_json(repaired)
            except Exception as e:                               # noqa: BLE001
                raise AnalystError(f"codex returned text that is not a valid "
                                   f"AnalystRead even after repair "
                                   f"({'; '.join(repairs)}): {e}") from e
            log.warning("codex read accepted after repair (%s)", "; ".join(repairs))
            return out
