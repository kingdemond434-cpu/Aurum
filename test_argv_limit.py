r"""The system prompt outgrew the Windows command line, and the desk went blind.

WHAT HAPPENED, 2026-08-27/28. Every survey failed with:

    claude exited 1: {"is_error":true,"duration_api_ms":0,"num_turns":1,
                      "usage":{"input_tokens":0,"output_tokens":0}}

Zero tokens AND zero API time — the CLI never reached the API. That rules out a
rate limit, a model outage and a timeout, and rules in a LOCAL failure.

THE CAUSE. `--system-prompt` is one argv element. On Windows a launcher shim
that re-invokes through cmd.exe truncates the command line at 8,191 characters.
universe_system() is 9,098; single-read ANALYST_SYSTEM is 7,226. So the desk
answered normally on the single-read path and went blind on EVERY survey —
precisely the ledger's shape: all failures carrying stage "survey" alongside 59
successful reads at a healthy 115s median in the same window.

An epistemic-framing change added ~2,000 chars to ANALYST_SYSTEM, which is what
carried universe over the line. The prompt change was right; nothing warned that
prompt text had a hard transport ceiling behind it.

    python3 -m pytest test_argv_limit.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import ANALYST_SYSTEM
from golddesk.providers import ClaudeCodeAnalyst
from golddesk.universe import MAX_CANDIDATES, universe_system

#: The real ceiling this exists to stay under.
CMD_LIMIT = 8191


def _p():
    p = ClaudeCodeAnalyst.__new__(ClaudeCodeAnalyst)
    p.binary = "C:/Users/Administrator/.local/bin/claude.EXE"
    p.model = "claude-opus-5"
    p.effort = "high"
    return p


def _argv_len(argv):
    return sum(len(a) + 1 for a in argv)


def test_the_universe_system_prompt_really_is_over_the_limit():
    """The premise, asserted rather than assumed — if this ever stops being
    true the rest of the file is testing a hypothetical."""
    assert len(universe_system(ANALYST_SYSTEM, MAX_CANDIDATES)) > CMD_LIMIT - 200


def test_an_oversized_system_prompt_leaves_the_command_line():
    argv = _p()._argv(universe_system(ANALYST_SYSTEM, MAX_CANDIDATES))
    assert "--system-prompt" not in argv
    assert _argv_len(argv) < CMD_LIMIT


def test_the_single_read_path_is_UNCHANGED():
    """It is currently working and producing evidence. Moving it too would
    silently change how a working arm addresses the model, and a change to the
    arm must be deliberate rather than a side effect of fixing another path."""
    argv = _p()._argv(ANALYST_SYSTEM)
    assert "--system-prompt" in argv
    assert ANALYST_SYSTEM in argv
    assert _argv_len(argv) < CMD_LIMIT


def test_the_threshold_sits_between_the_two_real_prompts():
    """Not a round number — chosen so the working path stays put and the broken
    one moves."""
    lim = ClaudeCodeAnalyst.MAX_SYSTEM_ARGV_CHARS
    assert len(ANALYST_SYSTEM) < lim < len(universe_system(ANALYST_SYSTEM, MAX_CANDIDATES))


def test_every_argv_stays_under_the_limit_however_big_the_prompt_gets():
    """The property that matters. A prompt of any size must not be able to
    produce a command line the shell will truncate."""
    for n in (1_000, 7_000, 7_899, 7_901, 50_000, 500_000):
        assert _argv_len(_p()._argv("x" * n)) < CMD_LIMIT, n


def test_the_model_still_receives_the_text_when_it_moves_to_stdin():
    """Falling back is not a degradation: same text, different transport."""
    seen = {}

    def runner(argv, prompt):
        seen["argv"], seen["prompt"] = argv, prompt
        return {"result": "{}", "is_error": False, "subtype": "success"}

    p = _p()
    p._runner = runner
    p.timeout_s = 60.0
    big = universe_system(ANALYST_SYSTEM, MAX_CANDIDATES)
    p._invoke("BRIEF-BODY", system=big)
    assert "--system-prompt" not in seen["argv"]
    assert big in seen["prompt"], "the system text was dropped, not relocated"
    assert "BRIEF-BODY" in seen["prompt"]


def test_the_system_text_precedes_the_brief():
    """Order is not cosmetic: instructions after the data read as an
    afterthought, and the cached prefix convention puts them first."""
    seen = {}

    def runner(argv, prompt):
        seen["prompt"] = prompt
        return {"result": "{}", "is_error": False, "subtype": "success"}

    p = _p()
    p._runner = runner
    p.timeout_s = 60.0
    big = universe_system(ANALYST_SYSTEM, MAX_CANDIDATES)
    p._invoke("BRIEF-BODY", system=big)
    assert seen["prompt"].index(big) < seen["prompt"].index("BRIEF-BODY")


def test_the_fallback_is_logged_every_time(caplog):
    """A transport that silently changes how the model is addressed is a change
    to the arm. It must be visible in the log, not inferred later from a shift
    in behaviour."""
    import logging

    def runner(argv, prompt):
        return {"result": "{}", "is_error": False, "subtype": "success"}

    p = _p()
    p._runner = runner
    p.timeout_s = 60.0
    with caplog.at_level(logging.WARNING):
        p._invoke("B", system=universe_system(ANALYST_SYSTEM, MAX_CANDIDATES))
    assert any("over the" in r.message and "argv budget" in r.message
               for r in caplog.records), caplog.text


def test_a_short_system_prompt_is_not_logged(caplog):
    """The warning must mean something. Firing on every ordinary call would make
    it noise within a day."""
    import logging

    def runner(argv, prompt):
        return {"result": "{}", "is_error": False, "subtype": "success"}

    p = _p()
    p._runner = runner
    p.timeout_s = 60.0
    with caplog.at_level(logging.WARNING):
        p._invoke("B", system="short system")
    assert not [r for r in caplog.records if "argv budget" in r.message]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
