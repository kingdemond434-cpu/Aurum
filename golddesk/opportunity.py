"""Opportunity capture — the half of the objective the desk was blind to.

Aurum's objective is maximum realised net value, not maximum selectivity. Two
consequences, both implemented here:

1. GATE ON EXPECTED VALUE, NOT ON R:R. A fixed `min_rr >= 1.8` throws away a
   1.2R trade that hits 70% of the time (EV +0.32R) and admits a 3.0R trade
   that hits 20% (EV -0.20R). R:R is half of an expectancy calculation and the
   desk was using it as if it were the whole thing. When a mechanism's cohort
   has resolved history, the gate uses its measured hit rate; when it does not,
   it falls back to a conservative R:R floor and says so.

2. MEASURE WHAT WAS NOT TAKEN. Every decision moment resolves BOTH directions
   and the best achievable excursion, so a refusal produces a number rather
   than a silence. Without this the desk can only ever discover that its
   filters were too loose, never that they were too tight.

Nothing here is a threshold chosen for its looks. The one free parameter is the
prior strength used to shrink a thin cohort toward its fallback, and it is
named, versioned and journalled.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional, Sequence

log = logging.getLogger(__name__)

PRIOR_STRENGTH = 20.0     # pseudo-observations pulling a thin cohort to prior
POLICY_VERSION = "opp-2026-08-14-a"


# --------------------------------------------------------------------------
# Cohort statistics — learned from the desk's own resolved history
# --------------------------------------------------------------------------

@dataclass
class CohortStat:
    key: str
    n: int
    wins: int
    mean_r: float
    hit_rate_raw: float
    hit_rate_shrunk: float
    informative: bool

    def expected_r(self, rr: float, cost_r: float) -> float:
        """EV in R for a proposal at this reward-to-risk, using this cohort."""
        p = self.hit_rate_shrunk
        return p * rr - (1.0 - p) * 1.0 - cost_r


def resolved_outcomes(rows: Sequence[dict]) -> list[dict]:
    """The ledger's resolved trades, normalised. ONE reader for the whole desk.

    This exists because the learning loop was silently reading nothing. Cohort
    statistics, hypothesis discovery and hypothesis confirmation each looked for
    rows with kind=="SIGNAL" carrying decision["realised_r"] — and no such row
    is ever written. Entry rows are journalled before the outcome exists, and
    the outcome lands later on a TRADE_CLOSED row. Every consumer therefore
    aggregated an empty set: cohorts stayed permanently empty, the expectancy
    gate fell back to its cold-start prior forever, and no hypothesis could
    accrue a single observation.

    A close row is self-describing (it carries the entry context, mechanism and
    setup), so the join happens at write time and this is a filter, not a
    correlation. Entry rows that were later resolved in place are still honoured
    so older ledgers keep working.
    """
    out: list[dict] = []
    for r in rows:
        kind = r.get("kind")
        dec = r.get("decision") or {}
        if kind == "TRADE_CLOSED" and r.get("realised_r") is not None:
            out.append({
                "t0": r.get("entry_t0") or r.get("ts"),
                "closed_ts": r.get("ts"),
                "context": r.get("context") or {},
                "mechanism_name": r.get("mechanism_name") or "unnamed",
                "setup": r.get("setup"), "direction": r.get("direction"),
                "realised_r": float(r["realised_r"]),
                "mfe_r": r.get("mfe_r"), "mae_r": r.get("mae_r"),
                "forgone_r": r.get("forgone_r"),
                # Timing of the extremes, the arm that produced it, and the size
                # it was given. Absent on older rows and passed through as None
                # rather than defaulted, so a consumer can tell "did not happen"
                # from "was not recorded".
                "t_mfe": r.get("t_mfe"), "t_mae": r.get("t_mae"),
                "vision": r.get("vision"),
                "risk_r": r.get("risk_r"), "account_r": r.get("account_r"),
                "resolution": r.get("resolution"),
                "management_policy": r.get("management_policy"),
                "management_authority": r.get("management_authority"),
                "reentry_policy": r.get("reentry_policy")})
        elif kind == "SIGNAL" and dec.get("realised_r") is not None:
            out.append({
                "t0": r.get("t0"), "closed_ts": r.get("t0"),
                "context": r.get("context") or {},
                "mechanism_name": dec.get("mechanism_name") or "unnamed",
                "setup": dec.get("setup"), "direction": dec.get("direction"),
                "realised_r": float(dec["realised_r"]),
                "mfe_r": dec.get("mfe_r"), "mae_r": dec.get("mae_r"),
                "forgone_r": dec.get("forgone_r"),
                "resolution": dec.get("resolution"),
                "management_policy": dec.get("management_policy"),
                "reentry_policy": dec.get("reentry_policy")})
    return out


def build_cohorts(rows: Sequence[dict], prior_hit: float = 0.40,
                  prior_strength: float = PRIOR_STRENGTH) -> dict[str, CohortStat]:
    """Hit rate per mechanism, shrunk toward a prior so thin cohorts cannot
    manufacture confidence. A mechanism with 3 wins from 3 is not 100%.
    """
    agg: dict[str, list[float]] = defaultdict(list)
    for o in resolved_outcomes(rows):
        agg[o["mechanism_name"]].append(o["realised_r"])

    out: dict[str, CohortStat] = {}
    for k, rs in agg.items():
        n = len(rs)
        wins = sum(1 for x in rs if x > 0)
        raw = wins / n
        shrunk = (wins + prior_hit * prior_strength) / (n + prior_strength)
        out[k] = CohortStat(k, n, wins, statistics.fmean(rs), raw, shrunk,
                            informative=n >= 30)
    return out


# --------------------------------------------------------------------------
# The EV gate — replaces the fixed R:R floor
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EVVerdict:
    take: bool
    ev_r: float
    hit_rate_used: float
    basis: Literal["COHORT", "FALLBACK"]
    reason: str


def ev_gate(rr: float, cost_r: float, mechanism: str,
            cohorts: Optional[dict[str, CohortStat]] = None,
            *, fallback_min_rr: float = 1.5,
            min_ev_r: float = 0.0,
            shadow: bool = False) -> EVVerdict:
    """Take when expected value is positive. Frequency follows from that.

    `min_ev_r` is the only economic bar: a trade must add expected value after
    costs. It is NOT a selectivity dial — raising it to look disciplined is
    exactly the behaviour the objective forbids.
    """
    stat = (cohorts or {}).get(mechanism)
    if stat is not None and stat.n > 0:
        ev = stat.expected_r(rr, cost_r)
        take = ev > min_ev_r
        note = ("" if stat.informative else
                f" (thin cohort n={stat.n}, shrunk toward prior)")
        return EVVerdict(take, ev, stat.hit_rate_shrunk, "COHORT",
                         f"EV {ev:+.3f}R at measured hit {stat.hit_rate_shrunk:.0%} "
                         f"on {stat.n} resolved{note}")

    # COLD-START UNCERTAINTY PRIOR — not a quality standard.
    #
    # With no history for this mechanism the hit rate is unknown, so the gate
    # falls back to requiring enough asymmetry that a below-average hit rate
    # still pays. That is a defensible response to ignorance and an indefensible
    # permanent rule: it blocks real trades on the strength of a guess, and the
    # trades it blocks are exactly the NOVEL mechanisms the desk needs history
    # for. It is therefore a DISCRETIONARY restriction like any other, it is
    # registered, its false-negative cost is measured from the refusal ledger's
    # forward paths, and it stops blocking the moment that measurement says it
    # costs more than it saves.
    #
    # THE DEADLOCK THIS BREAKS. The rationale above promises the prior "stops
    # blocking the moment that measurement says it costs more than it saves" --
    # and that measurement can never be made. Demotion runs off the review,
    # the review needs resolved outcomes, and this gate blocks the trades that
    # would produce them. Prior blocks unknown mechanisms -> no history accrues
    # -> the review returns UNDETERMINED -> the prior never demotes -> it blocks
    # forever. A gate holding its own escape hatch shut.
    #
    # IN SHADOW THERE IS NO LOSS TO AVOID. This restriction exists to protect
    # capital from mechanisms whose hit rate is unknown. Advisory-only mode puts
    # no capital at risk, so the entire BENEFIT side of the trade-off is zero,
    # while the COST side -- evidence never gathered -- is the whole reason the
    # desk is running at all. Blocking here buys nothing and pays the full
    # learning rate for it.
    #
    # NOT a general loosening. The moment the desk is armed, `shadow` is False
    # and the prior enforces exactly as before. Live risk is unchanged; what
    # changed is that measuring is no longer forbidden.
    from .constitution import is_enforcing
    breakeven = (1.0 + cost_r) / (1.0 + rr)
    meets = rr >= fallback_min_rr
    registered = is_enforcing("entry.fallback_min_rr")
    enforcing = registered and not shadow
    take = meets or not enforcing
    if not registered:
        stance = "prior DEMOTED (advisory only)"
    elif shadow:
        stance = ("prior NOT ENFORCED IN SHADOW — no capital at risk, and "
                  "blocking here is what starves its own demotion review")
    else:
        stance = "prior ENFORCING"
    return EVVerdict(take, float("nan"), breakeven, "COLD_START_PRIOR",
                     f"no resolved history for {mechanism!r}; "
                     f"R:R {rr:.2f} vs cold-start prior {fallback_min_rr:.2f} "
                     f"(breakeven hit {breakeven:.0%}) — {stance}")


# --------------------------------------------------------------------------
# Portfolio heat — replaces the arbitrary count caps
# --------------------------------------------------------------------------

@dataclass
class Heat:
    """Concurrency limited by RISK, not by a number of positions.

    max_concurrent=1 threw away every simultaneous opportunity regardless of
    its value. On one instrument, positions are correlated, so the honest
    constraint is total open risk plus a correlation haircut — not a count.
    """
    max_open_risk_r: float = 2.0          # total R at risk across open trades
    correlation_haircut: float = 0.65     # same-symbol same-direction overlap
    max_daily_loss_r: float = 3.0

    def room_for(self, open_risks: Sequence[float], same_direction: int,
                 new_risk_r: float, day_loss_r: float) -> tuple[bool, str]:
        if day_loss_r <= -self.max_daily_loss_r:
            return False, f"daily loss {day_loss_r:.2f}R at limit"
        effective = sum(open_risks) + new_risk_r * (
            1.0 + self.correlation_haircut * same_direction)
        if effective > self.max_open_risk_r:
            return False, (f"portfolio heat {effective:.2f}R would exceed "
                           f"{self.max_open_risk_r:.2f}R "
                           f"({len(open_risks)} open, {same_direction} same-direction)")
        return True, f"heat {effective:.2f}R of {self.max_open_risk_r:.2f}R"


# --------------------------------------------------------------------------
# What was NOT taken — the missed-opportunity ledger
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OpportunitySnapshot:
    """Both directions resolved at every decision moment, taken or not."""
    ts: datetime
    long_mfe_r: float
    long_mae_r: float
    short_mfe_r: float
    short_mae_r: float
    best_direction: Literal["LONG", "SHORT", "NONE"]
    best_r: float
    acted: bool
    acted_direction: Optional[str]
    captured_r: Optional[float]

    @property
    def capture_ratio(self) -> Optional[float]:
        if not self.acted or self.captured_r is None or self.best_r <= 0:
            return None
        return self.captured_r / self.best_r

    @property
    def forgone_r(self) -> float:
        return 0.0 if self.acted else max(0.0, self.best_r)


def snapshot(bars, t0: datetime, ref: float, risk_price: float,
             acted: bool = False, acted_direction: Optional[str] = None,
             captured_r: Optional[float] = None) -> OpportunitySnapshot:
    """Resolve BOTH directions forward. A refusal now yields a number."""
    fwd = [b for b in bars if b.ts > t0]
    if not fwd or risk_price <= 0:
        return OpportunitySnapshot(t0, 0, 0, 0, 0, "NONE", 0.0, acted,
                                   acted_direction, captured_r)
    hi = max(b.high for b in fwd)
    lo = min(b.low for b in fwd)
    l_mfe, l_mae = (hi - ref) / risk_price, (lo - ref) / risk_price
    s_mfe, s_mae = (ref - lo) / risk_price, (ref - hi) / risk_price
    if l_mfe >= s_mfe:
        best_d, best_r = "LONG", l_mfe
    else:
        best_d, best_r = "SHORT", s_mfe
    return OpportunitySnapshot(t0, l_mfe, l_mae, s_mfe, s_mae, best_d, best_r,
                               acted, acted_direction, captured_r)


@dataclass
class CaptureReport:
    moments: int
    acted: int
    action_rate: float
    realised_r: float
    best_available_r: float
    capture_of_available: float
    forgone_r: float
    mean_capture_ratio: Optional[float]
    wrong_direction: int

    def render(self) -> str:
        mc = "n/a" if self.mean_capture_ratio is None else f"{self.mean_capture_ratio:.1%}"
        return (f"  moments {self.moments}   acted {self.acted} "
                f"({self.action_rate:.1%})\n"
                f"  realised {self.realised_r:+.1f}R   best available "
                f"{self.best_available_r:+.1f}R   captured "
                f"{self.capture_of_available:.2%}\n"
                f"  forgone on refusals {self.forgone_r:+.1f}R   "
                f"mean capture ratio {mc}   wrong-direction {self.wrong_direction}")


def capture_report(snaps: Sequence[OpportunitySnapshot]) -> CaptureReport:
    """The objective's own scoreboard: how much of what was there did we get?"""
    acted = [s for s in snaps if s.acted]
    realised = sum(s.captured_r or 0.0 for s in acted)
    best_total = sum(max(0.0, s.best_r) for s in snaps)
    ratios = [s.capture_ratio for s in acted if s.capture_ratio is not None]
    wrong = sum(1 for s in acted
                if s.acted_direction and s.best_direction != "NONE"
                and s.acted_direction != s.best_direction)
    return CaptureReport(
        moments=len(snaps), acted=len(acted),
        action_rate=len(acted) / len(snaps) if snaps else 0.0,
        realised_r=realised, best_available_r=best_total,
        capture_of_available=(realised / best_total) if best_total > 0 else 0.0,
        forgone_r=sum(s.forgone_r for s in snaps),
        mean_capture_ratio=statistics.fmean(ratios) if ratios else None,
        wrong_direction=wrong)
