"""Empirical edge router — cohort permissions the model cannot argue with.

The analyst's prose is advisory. This is not. If measured evidence says a
cohort is negative, the compiler rejects it regardless of how convincing the
read was. A validated negative belongs in code, never only in a system prompt.

DESIGN RULE: the table below is DATA, and every row carries its provenance.
A cohort earns veto authority by having its evidence recorded, not by someone
being confident about it. Rows default to ADVISORY — they log and do nothing.
Promoting a row to ENFORCING is a human edit, made once, with the numbers
filled in. The model cannot add, edit, or promote a row.

Why so strict: a router populated from remembered results is just an overfit
with extra steps. The desk's own charter names this failure — "negative
controls are skipped for a finding that is obviously Gold-specific".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

log = logging.getLogger(__name__)


class Mode(str, Enum):
    ADVISORY = "ADVISORY"      # logs a match, permits the trade
    ENFORCING = "ENFORCING"    # rejects the trade


@dataclass(frozen=True)
class Evidence:
    """What is actually known about a cohort. All fields required to enforce."""
    discovery_r: float             # mean R, discovery sample
    confirmation_r: float          # mean R, independent confirmation sample
    net_r: float                   # after the canonical cost model
    ess: float                     # effective sample size, not raw row count
    quarters_agreeing: int          # quarters whose sign matched the claim.
                                    # For a REJECT rule that means quarters the
                                    # cohort lost in — NOT quarters it won in.
    quarters_total: int
    recorded_on: str               # ISO date the evidence was frozen
    note: str = ""

    def sufficient_to_enforce(self) -> tuple[bool, str]:
        """The bar a cohort must clear before it may veto anything."""
        if self.ess < 30:
            return False, f"ESS {self.ess:.0f} below 30"
        if self.quarters_total and self.quarters_agreeing < self.quarters_total * 0.75:
            return False, (f"only {self.quarters_agreeing}/{self.quarters_total} "
                           f"quarters agree")
        if (self.discovery_r > 0) != (self.confirmation_r > 0):
            return False, "discovery and confirmation disagree on sign"
        return True, "ok"


@dataclass(frozen=True)
class CohortRule:
    """Match on deterministic context fields. None = wildcard."""
    name: str
    verdict: str                            # "PERMIT" | "REJECT"
    mode: Mode = Mode.ADVISORY
    setup: Optional[str] = None
    trend_health: Optional[str] = None
    trend_direction_vs_trade: Optional[str] = None   # "WITH" | "AGAINST"
    session: Optional[str] = None
    volatility_state: Optional[str] = None
    evidence: Optional[Evidence] = None

    def matches(self, ctx: dict) -> bool:
        for fieldname in ("setup", "trend_health", "trend_direction_vs_trade",
                          "session", "volatility_state"):
            want = getattr(self, fieldname)
            if want is not None and ctx.get(fieldname) != want:
                return False
        return True


# --------------------------------------------------------------------------
# THE TABLE
# --------------------------------------------------------------------------
#
# Deliberately empty of enforcing rules. Fill these in from canonical/ with the
# real numbers, then flip mode to ENFORCING. Two candidates are stubbed below
# in ADVISORY so the wiring is exercised and the shape is obvious — neither
# rejects anything until a human completes its Evidence and promotes it.

COHORTS: list[CohortRule] = [
    CohortRule(
        name="countertrend-fade-into-strong-trend",
        verdict="REJECT",
        mode=Mode.ADVISORY,          # <- promote only when evidence is filled
        setup="SWING_REVERSAL",
        trend_health="STRONG",
        trend_direction_vs_trade="AGAINST",
        evidence=None,               # OPEN_QUESTIONS Q4 measured -0.1429 mean R
                                     # on n=383, but notes the thresholds were
                                     # TUNED ON THAT SAME DATA. In-sample. Needs
                                     # an out-of-sample window before enforcing.
    ),
    CohortRule(
        name="continuation-moderate-trend-health",
        verdict="REJECT",
        mode=Mode.ADVISORY,
        setup="TREND_CONTINUATION",
        trend_health="MODERATE",
        evidence=None,               # claimed generic continuation -0.0533R vs
                                     # STRONG +0.064R confirmation. NOT FOUND in
                                     # the knowledge base as of 2026-08-13 —
                                     # locate it in canonical/ and record ESS,
                                     # quarters and the discovery/confirmation
                                     # split here before enforcing.
    ),
]


@dataclass
class RouterVerdict:
    permitted: bool
    rule: Optional[str] = None
    reason: str = ""
    advisories: list[str] = field(default_factory=list)


def route(ctx: dict, cohorts: Optional[Sequence[CohortRule]] = None) -> RouterVerdict:
    """Check a proposal's cohort against recorded evidence.

    `ctx` carries the deterministic context fields — never the model's prose.

    NOTE: cohorts resolves at CALL time, not import time. Binding COHORTS as a
    default argument froze the table at import and made every later edit a
    silent no-op — a gate that looks configured and enforces nothing.
    """
    cohorts = COHORTS if cohorts is None else cohorts
    advisories: list[str] = []

    for rule in cohorts:
        if not rule.matches(ctx):
            continue

        if rule.mode is Mode.ADVISORY:
            advisories.append(f"{rule.name} would {rule.verdict} (advisory only)")
            continue

        if rule.verdict == "REJECT":
            if rule.evidence is None:
                log.error("rule %s is ENFORCING with no evidence — ignoring", rule.name)
                advisories.append(f"{rule.name} misconfigured: enforcing without evidence")
                continue
            ok, why = rule.evidence.sufficient_to_enforce()
            if not ok:
                log.warning("rule %s cannot enforce: %s", rule.name, why)
                advisories.append(f"{rule.name} evidence too weak to enforce: {why}")
                continue
            return RouterVerdict(
                permitted=False,
                rule=rule.name,
                reason=(f"cohort {rule.name} rejected: net {rule.evidence.net_r:+.4f}R "
                        f"ESS {rule.evidence.ess:.0f}, "
                        f"{rule.evidence.quarters_agreeing}/"
                        f"{rule.evidence.quarters_total} quarters agree"),
                advisories=advisories,
            )

    return RouterVerdict(permitted=True, advisories=advisories)
