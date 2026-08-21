#!/usr/bin/env python3
"""No quota, anywhere, ever. Opportunities are opportunities.

THE RULE

Nothing in the executable path may limit ACTION BY COUNT. Not signals per day,
not concurrent positions, not candidates per read, not a cool-off between
trades. A count has no economics in it: the fourth positive-expectancy
opportunity of the day is worth exactly its expected value, and it is not worth
less for being fourth.

WHAT IS ALLOWED, AND WHY IT IS NOT A QUOTA

  RISK limits          max_open_risk_r, max_daily_loss_r. Denominated in R, not
                       in trades. Ruin control — the objective is LONG-RUN
                       growth and a ruined desk has no long run. They scale with
                       what is at stake instead of counting events.

  EVIDENCE floors      MIN_COHORT, MIN_PAIRED, MIN_COVERAGE. These do not stop
                       the desk trading. They stop it CLAIMING something, which
                       is the opposite of a quota on opportunity.

  COST controls        wake cadence, poll intervals. Registered, measured, and
                       they govern spend rather than permission.

THE ONE THAT ALMOST HID

The forward RESOLUTION window is not a trading limit, so it looks exempt. It is
not: a refusal resolved over 60 bars scores zero for anything that paid off
later, and since the constitution prices a restriction by what refusing cost, a
short window makes every gate look cheap and every gate therefore gets kept. A
cap on how far forward we look is a cap on how expensive a gate may appear.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

OK, BAD = 0, 0
ROOT = Path(__file__).parent


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


def concurrency() -> None:
    from golddesk.constitution import BY_ID, Status
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.notify import build_sink
    from golddesk.providers import AnalystProvider
    import tempfile

    print("1. concurrency is limited by RISK, never by a count")

    class P(AnalystProvider):
        name, model = "stub", "none"

        def read(self, brief, charts=()):
            raise NotImplementedError

    out = Path(tempfile.mkdtemp())
    desk = LiveDesk(P(), Ledger(out / "l.jsonl"), build_sink(None),
                    shadow=True, vision=Vision.NUMERIC_ONLY)
    check("concurrency_ceiling defaults to None (no count)",
          desk.concurrency_ceiling is None)

    r = BY_ID["risk.one_position"]
    was = r.status
    try:
        r.status = Status.ADVISORY
        n = desk.max_concurrent()
        check("with one_position demoted there is NO count limit",
              n > 1000, f"max_concurrent() = {n:,} — effectively unlimited")
    finally:
        r.status = was
    check("with one_position ENFORCING the limit is 1, as registered",
          desk.max_concurrent() == 1)

    check("the count restriction is registered and REMOVED",
          "entry.concurrency_count" in BY_ID
          and BY_ID["entry.concurrency_count"].status is Status.REMOVED,
          BY_ID["entry.concurrency_count"].status.value
          if "entry.concurrency_count" in BY_ID else "MISSING")

    # heat must still bound it — removing the count must not remove the limit
    from golddesk.opportunity import Heat
    h = Heat(max_open_risk_r=2.0, correlation_haircut=0.65)
    ok, why = h.room_for([1.0], 1, 1.0, 0.0)
    check("heat still refuses a second correlated thesis", not ok, why)
    ok2, why2 = h.room_for([], 0, 1.0, 0.0)
    check("and permits the first", ok2, why2)


def forward_window() -> None:
    print("\n2. missed money is measured over a window long enough to be honest")
    from golddesk.constitution import BY_ID
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.notify import build_sink
    from golddesk.providers import AnalystProvider
    from golddesk.runner import ShadowRunner
    import inspect
    import tempfile

    class P(AnalystProvider):
        name, model = "stub", "none"

        def read(self, brief, charts=()):
            raise NotImplementedError

    out = Path(tempfile.mkdtemp())
    desk = LiveDesk(P(), Ledger(out / "l.jsonl"), build_sink(None),
                    shadow=True, vision=Vision.NUMERIC_ONLY)
    check("the live path resolves refusals over days, not hours",
          desk.forward_bars >= 300,
          f"{desk.forward_bars} M15 bars = {desk.forward_bars * 15 / 60 / 24:.1f} days")

    sig = inspect.signature(ShadowRunner.__init__)
    fb = sig.parameters["forward_bars"].default
    check("and so does the research path", fb >= 300,
          f"{fb} bars (was 40 = ten hours)")

    src = (ROOT / "golddesk" / "live.py").read_text(encoding='utf-8')
    check("the hardcoded 61-bar window is gone",
          "bars[i:i + 61]" not in src and "bars[i:i+61]" not in src)
    check("a truncated window is recorded as a LOWER BOUND, not a zero",
          "LOWER BOUND" in src)
    check("and the window is registered as something that distorts other gates",
          "measurement.forward_window" in BY_ID)


def candidates() -> None:
    print("\n3. the candidate cap is an output bound and announces itself")
    from golddesk.universe import (MAX_CANDIDATES, AnalystUniverse, Selection,
                                   select)
    check("the cap is generous enough to rarely bind", MAX_CANDIDATES >= 10,
          f"{MAX_CANDIDATES} slots")
    check("the schema lets the analyst SAY it had more",
          "had_more" in AnalystUniverse.model_fields,
          "an opportunity never stated leaves no trace; detection has to happen "
          "at the moment of truncation")
    sel = select([], __import__("golddesk.opportunity", fromlist=["Heat"]).Heat(),
                 analyst_had_more=True)
    check("and that propagates to the selection", sel.analyst_had_more
          and sel.truncated)
    check("and is visible in the rendered report",
          "TRUNCATED" in sel.render(), "raise MAX_CANDIDATES when this appears")
    check("and in the journal", sel.to_journal()["analyst_had_more"] is True)


def ttl() -> None:
    print("\n4. a signal is not killed by a stopwatch")
    from golddesk.analyst import Thresholds
    from golddesk.constitution import BY_ID, Status
    t = Thresholds()
    check("the TTL is not two bars long", t.default_ttl_minutes >= 120,
          f"{t.default_ttl_minutes}m (was 30 = two M15 bars)")
    check("it is registered and ADVISORY, not enforcing",
          "entry.signal_ttl" in BY_ID
          and BY_ID["entry.signal_ttl"].status is Status.ADVISORY)
    check("the analyst's stated invalidation travels with the signal",
          "invalidation" in
          __import__("golddesk.analyst", fromlist=["AnalystRead"]).AnalystRead.model_fields,
          "structure kills a setup; a timer kills whatever is open when it fires")


def source_sweep() -> None:
    """Grep the executable path for count-shaped limits that nobody declared."""
    print("\n5. no undeclared count limit anywhere in golddesk/")

    # AST, not grep. The package DISCUSSES the quotas it removed — "max_signals
    # _per_day and max_concurrent are gone" is a comment, not a quota — and a
    # text sweep flags the documentation of a fix as the bug. Only real
    # identifiers count: assignment targets, parameter names, keyword arguments,
    # dataclass fields.
    SUSPECT = re.compile(
        r"^(max_(signals|trades|entries|positions|per_day|daily_signals)"
        r"|signals_per_day|trades_per_day|cooldown|cool_off|throttle"
        r"|min_gap|concurrency_ceiling)\w*$", re.I)

    # Allowed, each with the reason it is not a quota on opportunity.
    ALLOWED = {
        "min_gap": "Watcher throttle; defaults to ZERO, asserted separately below",
        "concurrency_ceiling": "defaults to None (no count); asserted in section 1",
    }

    def names(tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                yield node.arg, node.lineno
            elif isinstance(node, ast.keyword) and node.arg:
                yield node.arg, getattr(node, "lineno", 0)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                yield node.target.id, node.lineno
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        yield t.id, node.lineno
                    elif isinstance(t, ast.Attribute):
                        yield t.attr, node.lineno

    hits = []
    for p in sorted((ROOT / "golddesk").glob("*.py")):
        tree = ast.parse(p.read_text(encoding='utf-8'))
        for name, ln in names(tree):
            if SUSPECT.match(name) and name not in ALLOWED:
                hits.append(f"{p.name}:{ln} {name}")
    check("no undeclared count-based limit in the package", not hits,
          "; ".join(sorted(set(hits))[:5]) if hits
          else f"AST-swept {len(list((ROOT / 'golddesk').glob('*.py')))} modules")

    # min_gap must actually still be zero
    from golddesk.watcher import Watcher
    import inspect
    d = inspect.signature(Watcher.__init__).parameters["min_gap"].default
    check("the wake throttle is still ZERO", d.total_seconds() == 0,
          f"{d} — a minimum gap between reads is a quota on thinking")

    # risk_check must contain no count
    from golddesk.runner import risk_check
    src = inspect.getsource(risk_check)
    check("risk_check counts nothing", "day_signals" not in src,
          "it enforces R, not events")

    # day_signals must be reporting only
    live = (ROOT / "golddesk" / "live.py").read_text(encoding='utf-8')
    m = [l for l in live.splitlines() if "day_signals" in l]
    check("day_signals is reporting only, never enforced",
          all("+=" in l or "never enforced" in l for l in m),
          "; ".join(x.strip()[:60] for x in m))


def objective_intact() -> None:
    print("\n6. the objective is still the objective")
    from golddesk.constitution import BY_ID, Kind, REGISTRY
    hard = [r for r in REGISTRY if r.kind is Kind.HARD_RISK]
    check("hard-risk exemptions stay few and risk-denominated", len(hard) <= 8,
          ", ".join(r.id for r in hard))
    check("none of them is a count",
          not any(k in r.id for r in hard
                  for k in ("count", "per_day", "quota", "concurrency")),
          "every exemption is about solvency or data integrity")
    disc = [r for r in REGISTRY if r.kind is Kind.DISCRETIONARY]
    check("every discretionary restriction has a review clock",
          all(r.review_days > 0 for r in disc), f"{len(disc)} discretionary")
    check("and every one states what it blocks",
          all(r.blocks and r.rationale for r in REGISTRY))


def main() -> int:
    print("NO QUOTAS — opportunities are opportunities\n")
    concurrency()
    forward_window()
    candidates()
    ttl()
    source_sweep()
    objective_intact()
    print(f"\n{OK} ok, {BAD} failed")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
