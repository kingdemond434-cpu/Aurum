"""A second brain, and the discipline that keeps it from becoming a lie.

    python3 -m pytest test_failover.py -q

WHAT IS BEING PROVEN, in the order it matters:

  IT FAILS OVER      when the primary is out of allowance, the second brain
                     answers on the SAME frozen brief and the read is stamped
                     with which brain produced it and why.

  IT COMES BACK      the moment the primary answers again the desk is on it,
                     and the row that marks the return says how long it was
                     away. This is the half that was missing everywhere else:
                     falling back is loud, coming back was silent, so "which
                     brain produced this week's signals" was unanswerable.

  IT NEVER INVENTS   with every brain unavailable the chain FAILS. It does not
                     reach for something weaker to keep the message cadence up.
                     The standing order is maximum frequency and it is a real
                     one, but it means "do not refuse trades out of timidity",
                     never "produce a signal from an unvalidated brain so the
                     hour has a message in it".

  IT DOES NOT LIE    a provider that cannot see charts refuses them rather than
                     reading a subset while the ledger records it as the same
                     kind of read. And the primary's own successful reads are
                     stamped exactly as they were before this existed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import AnalystRead, Setup
from golddesk.failover import (COOLOFF_SECONDS, Attempt, ChainAnalyst,
                               build_chain, classify)
from golddesk.providers import AnalystError, AnalystProvider, ProviderRead


def a_read(mech="m") -> AnalystRead:
    return AnalystRead(setup=Setup.NOVEL, direction="LONG", confidence=3,
                       mechanism_name=mech, entry_ref="E", stop_ref="S",
                       tp1_ref="T1", tp2_ref="T2", read="r", why="w",
                       why_not="n", invalidation="i")


class Fake(AnalystProvider):
    def __init__(self, name, model="m", fail=None):
        self.name, self.model = name, model
        self.fail = fail                      # None, or an exception to raise
        self.calls = 0
        self.saw = []

    def read(self, brief, charts=()):
        self.calls += 1
        self.saw.append((brief, tuple(charts)))
        if self.fail is not None:
            raise self.fail
        return ProviderRead(a_read(self.name), self.name, self.model, 1.0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


# --------------------------------------------------------------- classification

@pytest.mark.parametrize("msg,kind", [
    ("subscription session limit reached", "quota"),
    ("5-hour usage limit reached", "quota"),
    ("OAuth token expired, please login", "auth"),
    ("'codex' is not on PATH", "absent"),
    ("timed out after 300s", "timeout"),
    ("something else entirely", "error"),
])
def test_failures_are_classified_into_the_buckets_the_ledger_supports(msg, kind):
    assert classify(RuntimeError(msg)) == kind


# ---------------------------------------------------------------- it fails over

def test_the_second_brain_answers_when_the_first_cannot():
    a = Fake("primary", fail=AnalystError("session limit reached"))
    b = Fake("second")
    pr = ChainAnalyst([a, b]).read("BRIEF")
    assert pr.provider == "second"
    assert pr.failover["chain_position"] == 1
    assert pr.failover["fallback_from"] == "primary"
    assert pr.failover["fallback_class"] == "quota"
    assert "session limit" in pr.failover["fallback_reason"]


def test_both_brains_are_given_the_identical_frozen_brief():
    """A fallback reading different evidence is a different experiment."""
    a = Fake("primary", fail=AnalystError("outage"))
    b = Fake("second")
    ChainAnalyst([a, b]).read("THE-SAME-BRIEF", ("chart1",))
    assert a.saw == b.saw == [("THE-SAME-BRIEF", ("chart1",))]


def test_a_primary_success_is_stamped_exactly_as_it_always_was():
    a, b = Fake("primary"), Fake("second")
    pr = ChainAnalyst([a, b]).read("BRIEF")
    assert pr.provider == "primary" and pr.failover == {}
    assert "failover" not in pr.stamp()
    assert b.calls == 0, "the second brain was asked when the first answered"


def test_every_attempt_is_recorded_including_the_one_that_worked():
    a = Fake("primary", fail=AnalystError("quota exhausted"))
    b = Fake("second")
    pr = ChainAnalyst([a, b]).read("BRIEF")
    got = [(x["provider"], x["ok"]) for x in pr.failover["attempts"]]
    assert got == [("primary", False), ("second", True)]


# ------------------------------------------------------------- it never invents

def test_an_exhausted_chain_fails_rather_than_fabricating():
    a = Fake("primary", fail=AnalystError("quota"))
    b = Fake("second", fail=AnalystError("not on PATH"))
    with pytest.raises(AnalystError) as e:
        ChainAnalyst([a, b]).read("BRIEF")
    assert "every analyst in the chain" in str(e.value)
    assert "primary" in str(e.value) and "second" in str(e.value)
    assert "fabricate" in str(e.value)


def test_a_bug_in_the_desk_is_not_treated_as_an_outage():
    """Falling over because the FIRST brain exposed a defect here would hide the
    defect and then produce a signal from it."""
    class Broken(Fake):
        def read(self, brief, charts=()):
            raise KeyError("a real bug")
    b = Fake("second")
    with pytest.raises(KeyError):
        ChainAnalyst([Broken("primary"), b]).read("BRIEF")
    assert b.calls == 0


def test_an_empty_chain_is_refused_at_construction():
    with pytest.raises(ValueError):
        ChainAnalyst([])


# --------------------------------------------------------------- it comes back

def test_the_desk_returns_to_the_primary_the_moment_it_answers():
    a = Fake("primary", fail=AnalystError("timed out"))
    b = Fake("second")
    ch = ChainAnalyst([a, b])
    assert ch.read("B").provider == "second"
    a.fail = None
    assert ch.read("B").provider == "primary"


def test_the_row_that_marks_the_return_says_how_long_it_was_away():
    clock = Clock()
    a = Fake("primary", fail=AnalystError("timed out"))
    b = Fake("second")
    ch = ChainAnalyst([a, b])
    ch.clock = clock
    ch.read("B")
    clock.advance(3600)
    a.fail = None
    pr = ch.read("B")
    assert pr.provider == "primary"
    assert pr.failover["recovered"] is True
    assert pr.failover["degraded_seconds"] >= 3600


def test_the_recovery_is_stamped_once_and_not_on_every_row_after():
    a = Fake("primary", fail=AnalystError("timed out"))
    ch = ChainAnalyst([a, Fake("second")])
    ch.read("B")
    a.fail = None
    assert ch.read("B").failover.get("recovered") is True
    assert ch.read("B").failover == {}


# ------------------------------------------------------------------- cool-offs

def test_a_quota_failure_is_not_retried_on_the_very_next_wake():
    """Quota is the one failure providers.py says gets WORSE when retried."""
    clock = Clock()
    a = Fake("primary", fail=AnalystError("usage limit reached"))
    ch = ChainAnalyst([a, Fake("second")])
    ch.clock = clock
    ch.read("B")
    assert a.calls == 1
    ch.read("B")
    assert a.calls == 1, "the exhausted brain was hammered again immediately"


def test_the_cooloff_is_short_enough_to_find_the_recovery_quickly():
    """A long cool-off leaves the desk on the wrong brain for hours after the
    right one came back — the same failure, pointed the other way."""
    assert COOLOFF_SECONDS["quota"] <= 900
    assert COOLOFF_SECONDS["auth"] <= 600
    assert COOLOFF_SECONDS["timeout"] == 0.0
    assert COOLOFF_SECONDS["error"] == 0.0


def test_the_primary_is_probed_again_once_the_cooloff_expires():
    clock = Clock()
    a = Fake("primary", fail=AnalystError("usage limit reached"))
    ch = ChainAnalyst([a, Fake("second")])
    ch.clock = clock
    ch.read("B")
    clock.advance(COOLOFF_SECONDS["quota"] + 1)
    a.fail = None
    assert ch.read("B").provider == "primary"
    assert a.calls == 2


def test_a_timeout_is_retried_on_the_next_wake():
    a = Fake("primary", fail=AnalystError("timed out after 300s"))
    ch = ChainAnalyst([a, Fake("second")])
    ch.read("B")
    ch.read("B")
    assert a.calls == 2


def test_a_skipped_brain_is_recorded_rather_than_leaving_a_gap():
    clock = Clock()
    a = Fake("primary", fail=AnalystError("usage limit reached"))
    ch = ChainAnalyst([a, Fake("second")])
    ch.clock = clock
    ch.read("B")
    pr = ch.read("B")
    first = pr.failover["attempts"][0]
    assert first["provider"] == "primary" and "cooling off" in first["reason"]


def test_a_brain_that_answers_stops_cooling_off_immediately():
    clock = Clock()
    a = Fake("primary", fail=AnalystError("usage limit reached"))
    ch = ChainAnalyst([a, Fake("second")])
    ch.clock = clock
    ch.read("B")
    clock.advance(COOLOFF_SECONDS["quota"] + 1)
    a.fail = None
    ch.read("B")
    assert ch._cool == {}


# ------------------------------------------------------- the codex provider

def test_the_codex_provider_refuses_charts_rather_than_dropping_them():
    from golddesk.codex_provider import CodexLocalAnalyst
    p = CodexLocalAnalyst(runner=lambda argv, prompt: (0, "{}", ""))
    with pytest.raises(AnalystError) as e:
        p.read(_Brief(), charts=("c",))
    assert "Refusing rather than dropping" in str(e.value)


def test_the_codex_provider_invents_no_model_name():
    """A model id hardcoded from memory is a claim about a vendor's catalogue
    that goes stale silently and is wrong in a way nothing tests."""
    import inspect

    from golddesk import codex_provider
    src = inspect.getsource(codex_provider)
    assert "gpt-" not in src.lower(), "a model name was hardcoded"
    p = codex_provider.CodexLocalAnalyst(runner=lambda a, p_: (0, "{}", ""))
    assert p.model == ""
    assert "--model" not in p._argv()


def test_the_codex_provider_runs_read_only():
    from golddesk.codex_provider import CodexLocalAnalyst
    argv = CodexLocalAnalyst(runner=lambda a, p: (0, "", ""))._argv()
    assert "exec" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_the_codex_provider_reports_its_own_absence_honestly():
    from golddesk.codex_provider import CodexLocalAnalyst
    p = CodexLocalAnalyst(binary="definitely-not-installed-anywhere")
    ok, why = p.available()
    assert ok is False and "not on PATH" in why
    with pytest.raises(AnalystError):
        p.read(_Brief())


class _Brief:
    def render(self):
        return "BRIEF TEXT"


def test_the_codex_provider_parses_a_clean_read():
    from golddesk.codex_provider import CodexLocalAnalyst
    payload = a_read("codex-mech").model_dump_json()
    p = CodexLocalAnalyst(runner=lambda argv, prompt: (0, payload, ""))
    pr = p.read(_Brief())
    assert pr.provider == "codex_local"
    assert pr.read.mechanism_name == "codex-mech"


def test_the_codex_provider_unfences_a_code_block():
    from golddesk.codex_provider import CodexLocalAnalyst
    payload = "```json\n" + a_read().model_dump_json() + "\n```"
    p = CodexLocalAnalyst(runner=lambda argv, prompt: (0, payload, ""))
    assert p.read(_Brief()).read.direction == "LONG"


def test_the_codex_provider_raises_rather_than_returning_junk():
    from golddesk.codex_provider import CodexLocalAnalyst
    p = CodexLocalAnalyst(runner=lambda argv, prompt: (0, "I think we go long", ""))
    with pytest.raises(AnalystError):
        p.read(_Brief())


def test_a_nonzero_exit_is_an_analyst_error_so_the_chain_moves_on():
    from golddesk.codex_provider import CodexLocalAnalyst
    p = CodexLocalAnalyst(runner=lambda argv, prompt: (1, "", "rate limit"))
    with pytest.raises(AnalystError) as e:
        p.read(_Brief())
    assert classify(e.value) == "quota"


def test_the_codex_provider_claims_no_token_accounting():
    """A fabricated zero would let budget.py report no spend for real work."""
    from golddesk.codex_provider import CodexLocalAnalyst
    p = CodexLocalAnalyst(runner=lambda a, pr: (0, a_read().model_dump_json(), ""))
    u = p.read(_Brief()).usage
    assert u["billing_basis"] == "unmeasured_cli"
    assert "cost_usd" not in u and "in" not in u


# ------------------------------------------------------------------- the wiring

def test_a_chain_can_be_built_from_a_spec_string():
    from golddesk.providers import build_provider
    ch = build_provider("chain:deterministic+codexlocal:")
    assert isinstance(ch, ChainAnalyst)
    assert [p.name for p in ch.providers] == ["deterministic", "codex_local"]


def test_the_chain_describes_which_brains_are_actually_installed():
    ch = build_chain(["deterministic", "codexlocal:"])
    d = ch.describe()
    assert d["provider"] == "chain" and len(d["chain"]) == 2
    assert "available" in d["chain"][1]


def test_survey_fails_over_too():
    """Universe mode is the live path; a failover that only covered read()
    would leave the desk blind exactly where it actually reads."""
    a = Fake("primary", fail=AnalystError("quota"))
    b = Fake("second")
    stamp, uni = ChainAnalyst([a, b]).survey("BRIEF")
    assert stamp.provider == "second"
    assert stamp.failover["chain_position"] == 1
    assert len(uni.candidates) == 1


# --------------------------------------------------- measuring the two brains

def _sig(t0, provider, model="m", failover=None):
    d = {"provider": provider, "model": model}
    if failover:
        d["failover"] = failover
    return {"kind": "SIGNAL", "t0": t0, "decision": d}


def _closed(t0, r):
    return {"kind": "TRADE_CLOSED", "entry_t0": t0, "realised_r": r}


def test_signals_are_counted_per_brain():
    from golddesk.brain_compare import build
    rows = [_sig("a", "claudecode"), _sig("b", "codex_local",
                                          failover={"chain_position": 1})]
    rep = build(rows)
    assert rep.get("claudecode").signals == 1
    assert rep.get("codex_local").fallback_signals == 1


def test_an_expectancy_is_refused_below_the_sample_floor():
    from golddesk.brain_compare import build
    rows = []
    for k in range(3):
        rows += [_sig(f"t{k}", "codex_local"), _closed(f"t{k}", 1.0)]
    rep = build(rows)
    assert rep.get("codex_local").expectancy is None
    assert "UNMEASURED" in rep.render()


def test_a_measured_brain_reports_an_interval_not_a_point():
    from golddesk.brain_compare import build
    rows = []
    for k in range(10):
        rows += [_sig(f"t{k}", "claudecode"), _closed(f"t{k}", 1.0 if k % 2 else -0.5)]
    b = build(rows).get("claudecode")
    assert b.expectancy is not None and b.interval is not None


def test_the_comparison_refuses_to_be_read_as_controlled():
    from golddesk.brain_compare import build
    rows = []
    for k in range(10):
        rows += [_sig(f"a{k}", "claudecode"), _closed(f"a{k}", 0.5)]
        rows += [_sig(f"b{k}", "codex_local"), _closed(f"b{k}", 1.5)]
    text = build(rows).render()
    assert "NOT A CONTROLLED COMPARISON" in text
    assert "SAME frozen snapshots" in text


def test_unstamped_rows_are_not_assigned_to_a_brain_by_guessing():
    from golddesk.brain_compare import build
    rep = build([{"kind": "SIGNAL", "t0": "x", "decision": {}}])
    assert rep.get("unstamped").signals == 1


def test_quarantined_outcomes_are_excluded():
    from golddesk.brain_compare import build
    rows = [_sig("t", "claudecode"),
            {"kind": "TRADE_CLOSED", "entry_t0": "t", "realised_r": -1.0,
             "evidence_valid": False}]
    assert build(rows).get("claudecode").resolved == 0


def test_time_spent_on_the_fallback_is_totalled():
    from golddesk.brain_compare import build
    rows = [_sig("a", "claudecode",
                 failover={"chain_position": 0, "recovered": True,
                           "degraded_seconds": 7200})]
    rep = build(rows)
    assert rep.recoveries == 1 and rep.degraded_seconds == 7200


def test_the_brains_report_runs_daily():
    import aurum_cycle
    assert any(n == "brains" for n, _ in aurum_cycle.STEPS)


def test_the_universe_path_carries_the_failover_stamp():
    """The live path re-wraps the read; dropping the stamp there would make
    every fallback signal look like a primary one in the ledger."""
    import inspect

    from golddesk import live
    src = inspect.getsource(live.LiveDesk._decide_universe)
    assert "failover" in src


def test_preflight_understands_a_chain(monkeypatch):
    import run_desk
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = run_desk.check_analyst_backend("chain:deterministic+codexlocal:")
    assert c.ok is True and "brain(s) usable" in c.detail
    bad = run_desk.check_analyst_backend("chain:codexlocal:+deterministic")
    assert bad.ok is False, "a dead primary must not pass preflight"


def test_auto_keeps_only_the_brains_this_box_has():
    """'the fallback is configured' and 'the fallback can run' are different
    claims, and only the second one helps at 3am."""
    from golddesk.failover import AUTO_CHAIN, resolve_auto
    kept, skipped = resolve_auto()
    assert kept and kept[0] == AUTO_CHAIN[0], "the primary is never dropped"
    for s in skipped:
        assert ":" in s, "a skipped brain must carry its reason"
    assert len(kept) + len(skipped) == len(AUTO_CHAIN)


def test_auto_builds_a_usable_provider():
    from golddesk.failover import ChainAnalyst
    from golddesk.providers import build_provider
    p = build_provider("auto")
    assert isinstance(p, ChainAnalyst) and p.providers


def test_preflight_says_what_auto_resolved_to(monkeypatch):
    import run_desk
    c = run_desk.check_analyst_backend("auto")
    assert "brain(s) usable" in c.detail
    assert "NOT AVAILABLE" in c.detail or "every configured brain" in c.detail
