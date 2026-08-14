"""Anti-drift auditor (§14) — did this change serve the objective or a proxy?

Every edit to Aurum is a change to a trading policy, whether or not it was meant
as one. The failure mode this guards against is not a bug: it is a long series
of individually reasonable commits, each adding one more condition, until the
model's judgment is decoration on a rule tree.

TWO CHECKS, RUN ON EVERY MEANINGFUL CHANGE

  1. BEHAVIOURAL DIFF ON FROZEN STATES
     A fixed corpus of real market states is replayed through the old and new
     code. Every state where the decision moved is reported, and — critically —
     the report separates states that became TRADEABLE from states that became
     REFUSED. A change that only ever removes opportunity is drifting toward
     conservatism regardless of how principled each individual gate sounded.

  2. NEW CONSTANT DETECTION
     The AST is scanned for numeric literals that participate in a comparison
     inside a function that can refuse. Every such literal is a threshold. If it
     is not registered in the constitution, it is an undeclared economic rule
     that appeared without evidence, and the audit fails.

WHAT THIS DELIBERATELY DOES NOT DO

It does not judge whether the new behaviour is better. It cannot: that requires
forward outcomes. It reports the DIRECTION and SIZE of the behavioural change so
that a human or an evaluation cycle can ask for evidence proportional to it. A
change that alters 2 states out of 900 needs little; one that refuses 300 more
needs a great deal.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger(__name__)

DRIFT_VERSION = "drift-2026-08-14-a"


# --------------------------------------------------------------------------
# 1. Behavioural diff on frozen states
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StateDecision:
    """What the desk decided at one frozen state, reduced to what matters."""
    state_id: str
    acted: bool
    direction: Optional[str]
    entry: Optional[float]
    stop: Optional[float]
    tp2: Optional[float]
    reason: str

    def same_as(self, other: "StateDecision") -> bool:
        if self.acted != other.acted:
            return False
        if not self.acted:
            return True          # both refused; the wording of why is not behaviour
        return (self.direction == other.direction
                and _close(self.entry, other.entry)
                and _close(self.stop, other.stop)
                and _close(self.tp2, other.tp2))


def _close(a: Optional[float], b: Optional[float], tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


@dataclass
class DriftReport:
    ts: str
    n_states: int
    unchanged: int
    newly_refused: list[str] = field(default_factory=list)
    newly_tradeable: list[str] = field(default_factory=list)
    altered: list[str] = field(default_factory=list)
    new_constants: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.newly_refused) + len(self.newly_tradeable) + len(self.altered)

    @property
    def conservatism_drift(self) -> int:
        """Net opportunity removed. Positive means the change trades less."""
        return len(self.newly_refused) - len(self.newly_tradeable)

    def verdict(self) -> str:
        if self.new_constants:
            return ("FAIL — undeclared threshold(s) introduced; register them in "
                    "constitution.REGISTRY or remove them")
        if self.changed == 0:
            return "NO BEHAVIOURAL CHANGE — safe to merge without new evidence"
        if self.conservatism_drift > 0:
            return (f"REVIEW — net {self.conservatism_drift} state(s) lost. A change "
                    f"that only removes opportunity must show the opportunity was "
                    f"negative-EV, out of sample, or it is proxy optimisation")
        if self.conservatism_drift < 0:
            return (f"REVIEW — net {-self.conservatism_drift} state(s) gained. More "
                    f"activity is not self-justifying; show the added states carry "
                    f"positive expectancy after costs")
        return "REVIEW — behaviour moved without changing net activity"

    def render(self) -> str:
        out = [f"ANTI-DRIFT AUDIT {self.ts}  ({DRIFT_VERSION})",
               f"  frozen states replayed : {self.n_states}",
               f"  unchanged              : {self.unchanged} "
               f"({self.unchanged / max(self.n_states, 1):.1%})",
               f"  newly REFUSED          : {len(self.newly_refused)}",
               f"  newly TRADEABLE        : {len(self.newly_tradeable)}",
               f"  altered (same verdict) : {len(self.altered)}",
               f"  net conservatism drift : {self.conservatism_drift:+d} states"]
        if self.new_constants:
            out.append("  UNDECLARED THRESHOLDS INTRODUCED:")
            out += [f"    {c}" for c in self.new_constants]
        for label, ids in (("newly refused", self.newly_refused),
                           ("newly tradeable", self.newly_tradeable)):
            if ids:
                out.append(f"  {label}: {', '.join(ids[:6])}"
                           + (f" … +{len(ids) - 6} more" if len(ids) > 6 else ""))
        out.append(f"  VERDICT: {self.verdict()}")
        return "\n".join(out)


class FrozenStates:
    """A corpus of real decision inputs, stored once and never regenerated.

    Regenerating the corpus alongside a change defeats the purpose: the states
    themselves would move, and the diff would compare two different questions.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.path.exists()

    def write(self, states: Sequence[dict]) -> int:
        if self.path.exists():
            raise FileExistsError(
                f"{self.path} already frozen — a corpus that moves with the code "
                f"cannot detect the code moving")
        self.path.write_text(json.dumps({"version": DRIFT_VERSION,
                                         "frozen_at": datetime.now(timezone.utc).isoformat(),
                                         "states": list(states)}, indent=2, default=str))
        return len(states)

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text()).get("states", [])


def baseline_path(root: Path) -> Path:
    return Path(root) / "drift_baseline.json"


def record_baseline(decisions: Sequence[StateDecision], root: Path) -> Path:
    p = baseline_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": DRIFT_VERSION,
                             "recorded_at": datetime.now(timezone.utc).isoformat(),
                             "decisions": [asdict(d) for d in decisions]},
                            indent=2, default=str))
    return p


def audit(current: Sequence[StateDecision], root: Path,
          pkg_dir: Optional[Path] = None) -> DriftReport:
    """Compare current behaviour to the recorded baseline."""
    p = baseline_path(root)
    rep = DriftReport(datetime.now(timezone.utc).isoformat(), len(current), 0)
    if not p.exists():
        rep.unchanged = len(current)
        rep.new_constants = (undeclared_thresholds(pkg_dir) if pkg_dir else [])
        return rep
    prev = {d["state_id"]: StateDecision(**d)
            for d in json.loads(p.read_text()).get("decisions", [])}
    for d in current:
        old = prev.get(d.state_id)
        if old is None:
            continue
        if old.same_as(d):
            rep.unchanged += 1
        elif old.acted and not d.acted:
            rep.newly_refused.append(d.state_id)
        elif not old.acted and d.acted:
            rep.newly_tradeable.append(d.state_id)
        else:
            rep.altered.append(d.state_id)
    if pkg_dir:
        rep.new_constants = undeclared_thresholds(pkg_dir)
    return rep


# --------------------------------------------------------------------------
# 2. Undeclared threshold detection
# --------------------------------------------------------------------------

# Literals that are structural rather than economic: array indices, halves,
# percentages of a whole, and the identity/zero elements.
BENIGN = {0, 1, 2, -1, 0.0, 1.0, 0.5, 100, 100.0, 60, 24, 4, 1e-6, 1e-9}

# HTTP status codes are protocol constants, not trading thresholds. Comparing a
# response against 200 is not a policy about gold. Listed explicitly so the
# exemption is narrow and visible rather than a loosened tolerance.
HTTP_STATUS = {200, 201, 202, 204, 301, 302, 400, 401, 403, 404, 409, 422,
               429, 500, 502, 503, 504}


def undeclared_thresholds(pkg_dir: Path) -> list[str]:
    """Numeric literals used in comparisons inside functions that can refuse.

    Every one of these is a trading threshold whether or not it was intended as
    one. Declared restriction sites are exempt because their thresholds live in
    the registry with a rationale and a review date; anywhere else, a magic
    number in a comparison is an economic rule nobody voted for.
    """
    from .constitution import BY_ID, DECLARED_SITES
    declared_values = _registry_values()
    problems: list[str] = []
    for path in sorted(Path(pkg_dir).glob("*.py")):
        if path.name in ("__init__.py", "constitution.py", "drift_audit.py",
                         "backtest.py", "evaluation.py"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if fn.name in DECLARED_SITES:
                continue
            src = ast.unparse(fn)
            if not any(k in src for k in ("Refusal(", "refuse(", "return False",
                                          "ReentryVerdict(False")):
                continue
            for cmp_node in [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]:
                for operand in [cmp_node.left, *cmp_node.comparators]:
                    if not isinstance(operand, ast.Constant):
                        continue
                    v = operand.value
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        continue
                    if v in BENIGN or v in HTTP_STATUS or v in declared_values:
                        continue
                    problems.append(
                        f"{path.name}:{operand.lineno} {fn.name}() compares against "
                        f"{v!r} — an undeclared threshold in a refusal path")
    return problems


def _registry_values() -> set:
    """Threshold values that the constitution already knows about."""
    vals: set = set()
    try:
        from . import analyst, hypothesis, management, opportunity
        for mod in (analyst, opportunity, management, hypothesis):
            for name in dir(mod):
                if name.isupper():
                    v = getattr(mod, name)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        vals.add(v)
        for f in analyst.Thresholds.__dataclass_fields__.values():
            if isinstance(f.default, (int, float)):
                vals.add(f.default)
    except Exception as e:                       # never let the auditor crash a build
        log.debug("registry value scan incomplete: %s", e)
    return vals
