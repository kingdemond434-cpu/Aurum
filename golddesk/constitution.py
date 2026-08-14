"""The restriction registry — no gate may silently redefine the objective.

Aurum's objective is maximum robust net economic value. Every line of code that
can prevent the desk from acting is, in effect, an amendment to that objective.
This module makes each one declare itself and then pay rent.

TWO CLASSES, AND ONLY TWO

  HARD_RISK       Solvency and ruin control. Exempt from the value test because
                  the objective is *long-run* growth and a ruined desk has no
                  long run. Small in number, and listed explicitly so the
                  exemption can never be quietly widened.

  DISCRETIONARY   Everything else — every gate, threshold, cooldown, prompt
                  instruction, wake rule and learned policy. Each must
                  periodically demonstrate that the system WITH it beats the
                  counterfactual system WITHOUT it, out of sample, in net
                  economic value. A restriction that cannot show its keep is
                  demoted to ADVISORY and then removed.

THE TWO COSTS THAT WERE INVISIBLE

  Missed positive-EV opportunity, and MFE surrendered unnecessarily. Both are
  economic costs and are accounted here as such. A gate that avoids losses is
  not automatically good: it must avoid more than it forgoes.

SILENT RESTRICTIONS ARE A BUILD FAILURE

  verify_no_silent_restrictions() scans the executable path for refusal sites
  and asserts each maps to a declared entry. Adding a gate without registering
  it fails the check. That is the mechanism that keeps this honest — without
  it, the registry decays into documentation.
"""

from __future__ import annotations

import ast
import json
import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

log = logging.getLogger(__name__)

CONSTITUTION_VERSION = "const-2026-08-14-a"


class Kind(str, Enum):
    HARD_RISK = "HARD_RISK"
    DISCRETIONARY = "DISCRETIONARY"


class Status(str, Enum):
    ENFORCING = "ENFORCING"
    ADVISORY = "ADVISORY"      # measured, logged, does not block
    REMOVED = "REMOVED"


@dataclass
class Restriction:
    id: str
    site: str                      # module:function — where it actually lives
    kind: Kind
    blocks: str                    # what action it prevents, in plain words
    rationale: str
    status: Status = Status.ENFORCING
    review_days: int = 30
    last_reviewed: Optional[str] = None
    # Populated by measure(); None until the desk has evidence either way.
    blocked_n: Optional[int] = None
    counterfactual_net_r: Optional[float] = None
    verdict: Optional[str] = None

    @property
    def exempt(self) -> bool:
        return self.kind is Kind.HARD_RISK

    def due_for_review(self, now: datetime) -> bool:
        if self.exempt:
            return False
        if self.last_reviewed is None:
            return True
        return (now - datetime.fromisoformat(self.last_reviewed)).days >= self.review_days


# --------------------------------------------------------------------------
# THE REGISTRY — every restriction in the executable path
# --------------------------------------------------------------------------

REGISTRY: list[Restriction] = [
    # ---- hard risk: exempt, and deliberately few ----
    Restriction("risk.daily_loss", "runner:risk_check", Kind.HARD_RISK,
                "new entries once the day's realised loss reaches the limit",
                "ruin control; the objective is long-run growth"),
    Restriction("risk.portfolio_heat", "runner:risk_check", Kind.HARD_RISK,
                "entries that would push total open risk past the ceiling",
                "solvency; correlated gold exposure compounds"),
    Restriction("risk.stop_ratchet", "management:apply_option", Kind.HARD_RISK,
                "any stop move that increases risk",
                "invariant I1/I2; prevents a managed trade becoming unmanaged"),
    Restriction("risk.banked_profit", "management:apply_option", Kind.HARD_RISK,
                "actions that re-risk already-realised profit",
                "invariant I3; realised is realised"),
    Restriction("risk.stale_tick", "analyst:compile_signal", Kind.HARD_RISK,
                "trading on a quote that stopped advancing",
                "acting on a fossil price is not a trade, it is a guess"),

    # ---- discretionary: every one must prove itself ----
    Restriction("entry.expectancy_gate", "analyst:compile_signal", Kind.DISCRETIONARY,
                "proposals whose estimated EV is not positive after costs",
                "the objective itself, operationalised — but the ESTIMATE may be "
                "wrong, so it is measured like anything else"),
    Restriction("entry.fallback_min_rr", "opportunity:ev_gate", Kind.DISCRETIONARY,
                "unknown-mechanism proposals below the cold-start R:R prior",
                "a response to ignorance, not a quality standard: it blocks on a "
                "guess, and what it blocks is disproportionately the NOVEL "
                "mechanisms the desk most needs history for. Demote as soon as "
                "measured false-negative cost exceeds avoided loss",
                review_days=14),
    Restriction("entry.geometry", "analyst:compile_signal", Kind.DISCRETIONARY,
                "structurally inverted or unconfirmed-level proposals",
                "arithmetic validity, not selectivity"),
    Restriction("entry.spread_fraction", "analyst:compile_signal", Kind.DISCRETIONARY,
                "trades whose stop is small relative to spread",
                "cost drag; but the threshold is arbitrary and must be tested"),
    Restriction("entry.drift_anti_chase", "analyst:compile_signal", Kind.DISCRETIONARY,
                "entries after price has run from the structural trigger",
                "chasing hypothesis; unproven"),
    Restriction("entry.cohort_router", "router:route", Kind.DISCRETIONARY,
                "setups in cohorts with recorded negative evidence",
                "evidence-based, and demotes itself when evidence weakens"),
    Restriction("entry.novel_shadow", "analyst:ANALYST_SYSTEM", Kind.DISCRETIONARY,
                "sizing of NOVEL mechanisms before they have history",
                "exploration/validation boundary; its forgone value MUST be "
                "measured or it becomes permanent conservatism"),
    Restriction("reentry.policy", "policies:ReentryPolicy.evaluate", Kind.DISCRETIONARY,
                "repeat entries in the prior direction while its context lives",
                "prevents revenge re-entry; every parameter is a hypothesis"),
    Restriction("wake.watcher", "watcher:Watcher.observe", Kind.DISCRETIONARY,
                "analyst reads when deterministic state has not changed",
                "inference cost control; can hide opportunity and must be "
                "audited against what happened during unwoken periods"),
    Restriction("wake.observer_stake", "observer:TradeObserver.observe", Kind.DISCRETIONARY,
                "management reconsideration when little is at stake",
                "cost control; the stake estimate may be wrong"),
    Restriction("manage.chooser", "policies:ManagementChooser.choose", Kind.DISCRETIONARY,
                "management actions not selected by the active policy",
                "a preference order is a hypothesis about capture, nothing more"),

    # ---- integrity guards: refuse to act on data that cannot be trusted ----
    Restriction("integrity.path_stable", "ledger:verify_path_stable", Kind.HARD_RISK,
                "evaluation against a forward path whose content hash moved",
                "a mutating cache silently rewrites history; refusing is the "
                "only safe response and it costs no opportunity"),
    Restriction("evidence.standard", "hypothesis:clears_standard", Kind.HARD_RISK,
                "granting veto authority on evidence below the frozen standard",
                "the standard of proof is the one thing adaptation may never "
                "relax; a system that can lower its own bar has no bar"),
]

BY_ID = {r.id: r for r in REGISTRY}


# --------------------------------------------------------------------------
# No silent restrictions
# --------------------------------------------------------------------------

# Refusal sites that are declared. A refusal in code outside this map is silent.
DECLARED_SITES = {
    "compile_signal", "risk_check", "route", "apply_option", "observe",
    "evaluate", "choose", "ev_gate", "veto", "clears_standard",
    "adjudicate", "choose_option", "_render_charts", "active",
    # delegates to compile_signal and adds no gate of its own
    "analyse",
    # constructs a Refusal; it is the reporting mechanism, not a restriction
    "refuse",
    # portfolio heat — the executable half of risk.portfolio_heat
    "room_for",
    # integrity and evidence guards, registered above as HARD_RISK
    "verify_path_stable", "sufficient_to_enforce",
    # `may_reenter` is deliberately ABSENT. The free function it named carried
    # an undeclared 20-minute cool-off and an MFE>=0.5R ban; leaving its name
    # whitelisted here would let it back onto the live path unannounced. It is
    # superseded by policies:ReentryPolicy.evaluate, which is versioned.
}


def verify_no_silent_restrictions(pkg_dir: Path) -> tuple[bool, list[str]]:
    """Scan the executable path for refusal points and match them to the registry.

    Heuristic but load-bearing: any function that can return a refusal/False
    verdict and is not a declared site is flagged. False positives are cheap to
    whitelist; a missed silent gate is exactly the failure this prevents.
    """
    problems: list[str] = []
    for path in sorted(Path(pkg_dir).glob("*.py")):
        # The auditors are excluded from their own scan: their source contains
        # the very refusal patterns they search for, so including them detects
        # the detector. This is the only exemption and it is not extensible to
        # anything that can refuse a trade.
        if path.name in ("__init__.py", "constitution.py", "drift_audit.py"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            problems.append(f"{path.name}: unparseable ({e})")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            src = ast.get_source_segment(path.read_text(), node) or ""
            refuses = ("refuse(" in src or "Refusal(" in src
                       or "ReentryVerdict(False" in src or "return False," in src)
            if refuses and node.name not in DECLARED_SITES:
                problems.append(f"{path.name}:{node.name} can refuse but is not "
                                f"a declared restriction site")
    return (not problems), problems


# --------------------------------------------------------------------------
# Counterfactual measurement
# --------------------------------------------------------------------------

@dataclass
class RestrictionCost:
    restriction_id: str
    blocked_n: int
    forgone_r: float          # net R the blocked trades would have produced
    avoided_loss_r: float     # losses genuinely avoided (negative outcomes)
    net_contribution_r: float # avoided_loss - forgone. Positive = earns its keep.
    verdict: str

    def render(self) -> str:
        return (f"  {self.restriction_id:<26} blocked {self.blocked_n:>4}  "
                f"forgone {self.forgone_r:>+8.2f}R  avoided {self.avoided_loss_r:>+8.2f}R  "
                f"net {self.net_contribution_r:>+8.2f}R   {self.verdict}")


def measure(rows: Sequence[dict], reason_to_id: Optional[dict] = None
            ) -> list[RestrictionCost]:
    """What did each restriction cost, and what did it save?

    Uses the refusal ledger: every blocked decision carries a forward path, so
    the counterfactual is observed rather than modelled. A restriction earns
    its keep only if the losses it avoided exceed the value it forgave.
    """
    mapping = reason_to_id or DEFAULT_REASON_MAP
    agg: dict[str, list[float]] = {}
    for r in rows:
        if not str(r.get("kind", "")).startswith("REFUSAL"):
            continue
        reason = str(r.get("reason", ""))
        rid = next((v for k, v in mapping.items() if k in reason), None)
        if rid is None:
            continue
        out = r.get("outcome") or {}
        # What the blocked trade would have produced, using the same downstream
        # management assumption for every arm: first-touch to the objective.
        best = out.get("best_achievable_r")
        if best is None:
            continue
        agg.setdefault(rid, []).append(float(best))

    costs: list[RestrictionCost] = []
    for rid, vals in sorted(agg.items()):
        rest = BY_ID.get(rid)
        forgone = sum(v for v in vals if v > 0)
        avoided = -sum(v for v in vals if v < 0)
        net = avoided - forgone
        if rest and rest.exempt:
            verdict = "EXEMPT (hard risk)"
        elif net > 0:
            verdict = "EARNS ITS KEEP"
        elif len(vals) < 30:
            verdict = f"UNDETERMINED (n={len(vals)})"
        else:
            verdict = "COSTS MORE THAN IT SAVES -> DEMOTE"
        costs.append(RestrictionCost(rid, len(vals), forgone, avoided, net, verdict))
    return costs


DEFAULT_REASON_MAP = {
    "expectancy gate": "entry.expectancy_gate",
    "no resolved history": "entry.fallback_min_rr",
    "inverted": "entry.geometry",
    "unconfirmed level": "entry.geometry",
    "spread": "entry.spread_fraction",
    "do not chase": "entry.drift_anti_chase",
    "edge router": "entry.cohort_router",
    "re-entry blocked": "reentry.policy",
    "risk:": "risk.portfolio_heat",
    "stale tick": "risk.stale_tick",
    "NO_SETUP": "entry.novel_shadow",
}


# --------------------------------------------------------------------------
# Review — restrictions that fail lose their authority automatically
# --------------------------------------------------------------------------

@dataclass
class ReviewResult:
    ts: str
    demoted: list[str]
    retained: list[str]
    exempt: list[str]
    undetermined: list[str]
    silent: list[str]

    def render(self) -> str:
        out = [f"CONSTITUTIONAL REVIEW {self.ts}  ({CONSTITUTION_VERSION})"]
        if self.silent:
            out.append(f"  BUILD FAILURE — {len(self.silent)} undeclared restriction(s):")
            out += [f"    {s}" for s in self.silent]
        out.append(f"  exempt (hard risk) : {len(self.exempt)}  {self.exempt}")
        out.append(f"  retained           : {len(self.retained)}  {self.retained}")
        out.append(f"  undetermined       : {len(self.undetermined)}  {self.undetermined}")
        out.append(f"  DEMOTED            : {len(self.demoted)}  {self.demoted}")
        return "\n".join(out)


def review(rows: Sequence[dict], pkg_dir: Optional[Path] = None,
           now: Optional[datetime] = None, apply: bool = True) -> ReviewResult:
    """Periodic audit. Discretionary restrictions that cost more than they save
    are demoted to ADVISORY — they keep measuring, they stop blocking."""
    now = now or datetime.now(timezone.utc)
    costs = {c.restriction_id: c for c in measure(rows)}
    demoted, retained, exempt, undetermined = [], [], [], []

    for r in REGISTRY:
        if r.exempt:
            exempt.append(r.id)
            continue
        c = costs.get(r.id)
        if c is None or c.verdict.startswith("UNDETERMINED"):
            undetermined.append(r.id)
            continue
        r.last_reviewed = now.isoformat()
        r.blocked_n, r.counterfactual_net_r, r.verdict = (
            c.blocked_n, c.net_contribution_r, c.verdict)
        if c.net_contribution_r <= 0:
            if apply:
                r.status = Status.ADVISORY
            demoted.append(r.id)
        else:
            retained.append(r.id)

    silent: list[str] = []
    if pkg_dir is not None:
        ok, problems = verify_no_silent_restrictions(pkg_dir)
        if not ok:
            silent = problems
    return ReviewResult(now.isoformat(), demoted, retained, exempt, undetermined, silent)


def is_enforcing(rid: str) -> bool:
    """Call sites consult this. A demoted restriction stops blocking."""
    r = BY_ID.get(rid)
    return r is None or r.status is Status.ENFORCING
