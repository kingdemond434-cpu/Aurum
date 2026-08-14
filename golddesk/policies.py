"""Competing policies — nothing here is asserted to be optimal.

Two decisions were previously hardcoded as if they were truths: which legal
management action to take, and when a prior trade may be repeated. Both are now
named, versioned policy objects that compete in the factorial. The legality and
invariant layer is unchanged and still owns safety.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Protocol, Sequence

from .features import StructureState
from .management import Action, Excursion, ManagementOption, Position, ThesisState
from .reentry import PriorTrade, ReentryVerdict

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Management choosers
# --------------------------------------------------------------------------

class ManagementChooser(Protocol):
    name: str
    def choose(self, opts: Sequence[ManagementOption], pos: Position,
               thesis: ThesisState, exc: Excursion) -> Optional[ManagementOption]: ...


class HeuristicChooser:
    """ARM D. A hand-written preference order — a CANDIDATE, not a finding.

    Recorded here so the factorial can beat it. It has never been shown to
    maximise economic capture; it is what one person thought looked sensible.
    """
    name = "heuristic-v1"

    def choose(self, opts, pos, thesis, exc):
        by = {o.action: o for o in opts}
        if Action.EXIT in by:
            return by[Action.EXIT]
        if Action.PROTECT in by and exc.r_open_now > 0:
            return by[Action.PROTECT]
        if Action.PARTIAL in by and exc.mfe_r > exc.r_open_now and exc.r_open_now > 0:
            return by[Action.PARTIAL]
        if Action.TRAIL in by and thesis.trend_health == "STRONG":
            return by[Action.TRAIL]
        return by.get(Action.HOLD)


class PassiveChooser:
    """The null policy: never intervene. The floor every other arm must beat."""
    name = "passive"

    def choose(self, opts, pos, thesis, exc):
        return next((o for o in opts if o.action is Action.HOLD), None)


MANAGEMENT_SYSTEM = """\
You are managing an open XAUUSD position. Code has enumerated every legal move \
and already enforced the invariants — the stop cannot move backwards, risk \
cannot increase, banked profit cannot be re-risked. You choose exactly one \
option by id and nothing else.

The economic question, and it runs in both directions: protecting a gain that \
was about to evaporate is worth real money, and strangling a runner that had \
much further to go costs exactly as much. There is no rule about R that \
resolves this. Read what the trade is actually doing.

Weigh whether the reason for the trade still holds, whether the move is \
accelerating or stalling, how much room this volatility demands, how much of \
the favourable excursion has already been surrendered, and what remains \
available if it continues.

HOLD is a decision and is frequently correct. Choose it when intervening would \
cost more in surrendered upside than it protects.
"""


class ContextualChooser:
    """ARM E. The model chooses among legal options. Cannot break an invariant.

    Same contract as the entry analyst: it selects an id, code resolves the
    numbers. A wake here costs a model call, so the caller decides when
    reconsideration is worth paying for.
    """
    name = "contextual-v1"

    def __init__(self, provider, max_tokens: int = 2000):
        self.provider, self.max_tokens = provider, max_tokens

    def choose(self, opts, pos, thesis, exc):
        if len(opts) <= 1:
            return opts[0] if opts else None
        prompt = self._render(opts, pos, thesis, exc)
        try:
            oid = self.provider.choose_option(MANAGEMENT_SYSTEM, prompt,
                                              [o.id for o in opts])
        except Exception as e:
            log.warning("contextual chooser unavailable (%s) — holding", e)
            return next((o for o in opts if o.action is Action.HOLD), None)
        return next((o for o in opts if o.id == oid), None)

    @staticmethod
    def _render(opts, pos, thesis, exc) -> str:
        return "\n".join([
            f"POSITION {pos.direction} entry {pos.entry:.2f} stop {pos.current_stop:.2f}",
            f"  remaining {pos.remaining_fraction:.0%}  banked {pos.banked_r:+.2f}R"
            f"  locked {pos.locked_r:+.2f}R  open risk {pos.open_risk_r:.2f}R",
            f"PATH  open {exc.r_open_now:+.2f}R   MFE {exc.mfe_r:+.2f}R at "
            f"{exc.time_to_mfe_s/60:.0f}m   MAE {exc.mae_r:+.2f}R at "
            f"{exc.time_to_mae_s/60:.0f}m   held {exc.bars_held} bars",
            f"  surrendered since MFE: {exc.mfe_r - exc.r_open_now:+.2f}R",
            f"THESIS structure_intact={thesis.structure_intact} "
            f"health={thesis.trend_health} vol={thesis.volatility_state} "
            f"opposing_displacement={thesis.displacement_against}",
            "", "LEGAL OPTIONS (choose one id):",
            *[o.render() for o in opts],
        ])


# --------------------------------------------------------------------------
# Re-entry policy — the constants were hypotheses all along
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReentryPolicy:
    """Every value here is a HYPOTHESIS to be validated by cohort and regime.

    The previous code buried a 20-minute cool-off and an MFE>=0.5R condition
    inside a function and treated them as facts. They may well be wrong. They
    are now versioned and stamped onto every verdict so the ledger can answer
    whether they earned their place — including the possibility that the
    correct cool-off is zero.
    """
    version: str = "reentry-2026-08-14-a"
    cooloff: timedelta = timedelta(0)          # default: NO suppression
    require_prior_worked_mfe_r: Optional[float] = None   # None = no condition
    require_fresh_confirmation: bool = True    # structural, not a quota
    require_thesis_intact: bool = True

    def evaluate(self, prior: PriorTrade, st: StructureState,
                 now: datetime) -> ReentryVerdict:
        if self.cooloff and now - prior.exited_utc < self.cooloff:
            return ReentryVerdict(False, f"cool-off {self.cooloff} [{self.version}]")
        if self.require_thesis_intact and not prior.thesis_still_intact:
            return ReentryVerdict(False, "prior thesis no longer intact")
        want = "UP" if prior.direction == "LONG" else "DOWN"
        if st.trend_direction != want:
            return ReentryVerdict(False, f"structure is {st.trend_direction}, not {want}")
        if (self.require_prior_worked_mfe_r is not None
                and prior.exit_reason == "STOP"
                and prior.mfe_r < self.require_prior_worked_mfe_r):
            return ReentryVerdict(False, f"prior never worked (MFE {prior.mfe_r:+.2f}R) "
                                         f"[{self.version}]")
        if self.require_fresh_confirmation and not (
                st.reclaim_state == "CONFIRMED"
                or st.displacement_state in ("CONFIRMED", "EXCEPTIONAL")):
            return ReentryVerdict(False, "no fresh confirmation since exit")
        return ReentryVerdict(True, f"fresh {st.trend_health} {st.trend_direction} "
                                    f"confirmation [{self.version}]")
