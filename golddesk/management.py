"""Adaptive management — code enumerates legal moves, intelligence chooses one.

Same contract as the entry analyst, applied after the fill:

    code   -> enumerates structurally valid management options
    model  -> picks one BY ID (it cannot invent a stop price)
    code   -> resolves the numbers and enforces hard risk invariants

NO FIXED-R THRESHOLDS. There is no "move to breakeven at +1R", no "bank 50% at
+2R". Every option below is derived from a structural anchor, normalised by
current volatility, and sized from the position's own excursion. What survives
as a parameter lives in ManagementPolicy — named, versioned, and stamped onto
every decision so OOS evaluation can attribute outcomes to it rather than to
folklore.

The invariants are not negotiable and the model has no vote on them:

    I1  the stop ratchets — it never moves against the position
    I2  open risk never increases
    I3  banked profit is never re-risked
    I4  remaining size only decreases
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional, Sequence

from .costs import CostModel, round_trip_cost

log = logging.getLogger(__name__)

POLICY_VERSION = "mgmt-2026-08-14-a"


# --------------------------------------------------------------------------
# Live position state
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Excursion:
    """Continuous MFE/MAE. The management engine's primary sense organ."""
    mfe_r: float
    mae_r: float
    time_to_mfe_s: float
    time_to_mae_s: float
    r_open_now: float          # unrealised, in R, net of round-trip cost
    bars_held: int


@dataclass(frozen=True)
class Position:
    direction: Literal["LONG", "SHORT"]
    entry: float
    initial_stop: float
    current_stop: float
    risk_price: float          # entry -> initial_stop, the R unit. FROZEN.
    remaining_fraction: float  # 1.0 at fill, decreases with partials
    banked_r: float            # realised R already taken off the table
    opened_utc: datetime
    setup: str

    @property
    def long(self) -> bool:
        return self.direction == "LONG"

    def r_at(self, price: float) -> float:
        d = (price - self.entry) if self.long else (self.entry - price)
        return d / self.risk_price

    @property
    def open_risk_r(self) -> float:
        """What the remaining size still stands to lose from here."""
        return max(0.0, -self.r_at(self.current_stop)) * self.remaining_fraction

    @property
    def locked_r(self) -> float:
        """Guaranteed R if the stop is hit now: banked + stop-locked."""
        return self.banked_r + self.r_at(self.current_stop) * self.remaining_fraction


@dataclass(frozen=True)
class ThesisState:
    """Is the reason for the trade still true? Deterministic, from structure."""
    structure_intact: bool
    trend_health: Literal["STRONG", "MODERATE", "WEAK"]
    volatility_state: Literal["LOW", "NORMAL", "ELEVATED", "EXTREME"]
    displacement_against: bool        # opposing displacement printed
    target_liquidity_taken: bool      # the objective's liquidity already swept
    invalidation_touched: bool


# --------------------------------------------------------------------------
# Policy — every remaining parameter, named and versioned
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ManagementPolicy:
    """The shape of the adaptive response. Not thresholds on R.

    These are stamped onto every ManagementDecision so that when the ledger is
    replayed OOS you can ask "did policy version X beat version Y" instead of
    guessing which constant mattered.
    """
    version: str = POLICY_VERSION
    # Breathing room: how far beyond a structural anchor the stop must sit,
    # in ATR, scaled by volatility state. Not a fixed price or a fixed R.
    breathing_atr: dict = field(default_factory=lambda: {
        "LOW": 0.20, "NORMAL": 0.30, "ELEVATED": 0.45, "EXTREME": 0.70,
    })
    # A partial is only offered when it can make the residual risk-free or
    # better — the fraction is then SOLVED, never chosen.
    #
    # There is NO max_partial_fraction. Capping how much may be banked was a
    # quota: if the solved fraction says 80%, refusing to bank it discards
    # realised value to satisfy an aesthetic about runners.
    #
    # min_runner_fraction is not a quota either — it is the point below which a
    # residual is smaller than the broker will let you trade. Derive it from
    # min lot size, do not pick it.
    min_runner_fraction: float = 0.10

    def breathing_room(self, atr: float, vol: str) -> float:
        return atr * self.breathing_atr.get(vol, 0.30)


# --------------------------------------------------------------------------
# Options — enumerated from structure, never invented
# --------------------------------------------------------------------------

class Action(str, Enum):
    HOLD = "HOLD"
    PROTECT = "PROTECT"      # ratchet the stop to a structural anchor
    PARTIAL = "PARTIAL"      # bank a solved fraction, keep a runner
    TRAIL = "TRAIL"          # follow structure as it builds
    EXIT = "EXIT"            # thesis dead, close the remainder


@dataclass(frozen=True)
class Anchor:
    """A structural level behind price that a stop could legally rest on."""
    id: str
    kind: str                # SWING_LOW, RECLAIM, DISPLACEMENT_OPEN, ...
    price: float
    timeframe: str
    confirmed: bool


@dataclass(frozen=True)
class ManagementOption:
    id: str                          # "M1" — the model picks this, nothing else
    action: Action
    new_stop: Optional[float]
    partial_fraction: Optional[float]
    anchor_id: Optional[str]
    facts: str                       # deterministic description, no advocacy

    def render(self) -> str:
        return f"  {self.id}  {self.action.value:<8} {self.facts}"


@dataclass(frozen=True)
class BrokerLimits:
    """Venue constraints on where a stop may legally sit, in PRICE units.

    These are not policy and never appear in the constitution's discretionary
    registry: they are facts about the venue. Offering a model an option the
    broker will reject is worse than offering none, because the desk then
    believes it is protected when the order was never accepted.

    min_stop_distance : broker SYMBOL_TRADE_STOPS_LEVEL, converted to price.
                        A stop closer than this to the market is rejected.
    freeze_distance   : broker SYMBOL_TRADE_FREEZE_LEVEL, converted to price.
                        Inside this band the order cannot be modified at all.
    """
    min_stop_distance: float = 0.0
    freeze_distance: float = 0.0

    @classmethod
    def from_symbol_info(cls, info: Any, point: Optional[float] = None) -> "BrokerLimits":
        p = point if point is not None else getattr(info, "point", 0.01)
        return cls(min_stop_distance=getattr(info, "trade_stops_level", 0) * p,
                   freeze_distance=getattr(info, "trade_freeze_level", 0) * p)


def stop_is_legal(pos: Position, cand: float, bid: float, ask: float,
                  limits: BrokerLimits) -> tuple[bool, str]:
    """Can this stop actually be placed, right now, at this quote?

    A LONG stop is triggered on the BID and must sit below it; a SHORT stop is
    triggered on the ASK and must sit above it. Both must clear the venue's
    minimum distance, and neither may sit inside the freeze band. Checked
    against the CURRENT quote rather than the entry price, because the market
    has moved since the entry and legality is a property of now.
    """
    trigger = bid if pos.long else ask
    gap = (trigger - cand) if pos.long else (cand - trigger)
    if gap <= 0:
        return False, (f"stop {cand:.2f} is through the market "
                       f"({'bid' if pos.long else 'ask'} {trigger:.2f}) — "
                       f"that is an exit, not a stop")
    if limits.min_stop_distance and gap < limits.min_stop_distance:
        return False, (f"stop {cand:.2f} is {gap:.2f} from {trigger:.2f}, inside the "
                       f"broker minimum {limits.min_stop_distance:.2f}")
    if limits.freeze_distance and gap < limits.freeze_distance:
        return False, (f"stop {cand:.2f} is inside the freeze band "
                       f"{limits.freeze_distance:.2f} — the order cannot be modified")
    return True, "placeable"


def enumerate_options(
    pos: Position,
    thesis: ThesisState,
    exc: Excursion,
    anchors: Sequence[Anchor],
    atr: float,
    spread: float,
    policy: ManagementPolicy = ManagementPolicy(),
    cost_model: CostModel = CostModel(),
    *,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    broker: BrokerLimits = BrokerLimits(),
) -> list[ManagementOption]:
    """Build the legal move set. Deterministic — no model involvement.

    Anything not in this list cannot happen. If the list is just [HOLD], then
    HOLD is the only outcome regardless of what any intelligence prefers.

    `bid`/`ask`/`broker` make legality a property of the CURRENT market rather
    than of the entry: a stop candidate is only offered if it can actually be
    placed at this quote under the venue's minimum-distance and freeze rules.
    When no quote is supplied the mid is reconstructed from the spread, so the
    check degrades to "not through the market" rather than disappearing.
    """
    if bid is None or ask is None:
        # Reconstruct a quote around the stop's reference price so the
        # through-the-market test still applies. Never silently skip legality.
        mid = pos.entry + exc.r_open_now * pos.risk_price * (1 if pos.long else -1)
        half = (spread or 0.0) / 2.0
        bid, ask = mid - half, mid + half
    opts: list[ManagementOption] = [
        ManagementOption("M1", Action.HOLD, None, None, None,
                         f"leave stop at {pos.current_stop:.2f}; "
                         f"open risk {pos.open_risk_r:.2f}R, locked {pos.locked_r:+.2f}R")
    ]
    n = 2
    room = policy.breathing_room(atr, thesis.volatility_state)

    # --- Ratchet candidates: one per confirmed anchor that improves the stop.
    for a in anchors:
        if not a.confirmed:
            continue
        cand = a.price - room if pos.long else a.price + room
        # I1: ratchet only.
        improves = cand > pos.current_stop if pos.long else cand < pos.current_stop
        if not improves:
            continue
        # VENUE LEGALITY, checked against the CURRENT quote. Previously this was
        # a no-op conditional guarding a placeholder that returned its argument,
        # so structurally sensible but unplaceable stops reached the model.
        ok, why = stop_is_legal(pos, cand, bid, ask, broker)
        if not ok:
            log.debug("stop candidate from %s rejected: %s", a.id, why)
            continue
        locked_after = pos.banked_r + pos.r_at(cand) * pos.remaining_fraction
        action = Action.PROTECT if locked_after >= 0 else Action.TRAIL
        opts.append(ManagementOption(
            f"M{n}", action, round(cand, 2), None, a.id,
            f"stop -> {cand:.2f} ({room:.2f} beyond {a.kind} {a.id} @ {a.price:.2f}, "
            f"{a.timeframe}); locks {locked_after:+.2f}R, "
            f"open risk {max(0.0, -pos.r_at(cand)) * pos.remaining_fraction:.2f}R"
        ))
        n += 1

    # --- Partial: the fraction is SOLVED, not chosen, so the WHOLE POSITION
    # becomes risk-free — accounting for what is already banked.
    #
    # The previous solve was f = risk / (r_open + risk), which ignored
    # pos.banked_r entirely. After one partial the position already carries
    # realised profit, so a smaller fraction suffices to reach risk-free; solving
    # as if banked_r were zero over-banks on every subsequent wake and
    # progressively suffocates the runner. With repeated wakes that compounds.
    #
    # Let  B = banked_r, M = remaining_fraction, O = r_open, S = r_at(stop) (<=0).
    # Guaranteed R after banking fraction f of the remainder:
    #     L(f) = B + M*f*O + M*(1-f)*S
    # Risk-free means L(f) >= 0, so:
    #     f >= -(B + M*S) / (M*(O - S))
    # If B + M*S >= 0 the position is ALREADY risk-free and f = 0 — no partial is
    # offered, because banking more would only shrink the runner for nothing.
    r_open = exc.r_open_now
    s_r = pos.r_at(pos.current_stop)                 # <= 0 while risk remains
    m = pos.remaining_fraction
    guaranteed_now = pos.banked_r + m * s_r          # == pos.locked_r
    if r_open > 0 and (r_open - s_r) > 0 and m > 0:
        if guaranteed_now >= -1e-9:
            pass                                     # already risk-free
        else:
            f = -guaranteed_now / (m * (r_open - s_r))
            f = min(max(f, 0.0), 1.0)
            runner_after = m * (1.0 - f)
            if 0.0 < f < 1.0 and runner_after >= policy.min_runner_fraction:
                banked_now = f * m * r_open
                opts.append(ManagementOption(
                    f"M{n}", Action.PARTIAL, None, round(f, 4), None,
                    f"bank {f:.1%} of the remaining {m:.0%} at {r_open:+.2f}R open "
                    f"-> guaranteed {guaranteed_now:+.2f}R becomes "
                    f"{guaranteed_now + banked_now - f * m * s_r:+.2f}R "
                    f"(already banked {pos.banked_r:+.2f}R accounted); "
                    f"runner {runner_after:.0%} survives"
                ))
                n += 1

    # --- Exit: only offered when the thesis is measurably dead.
    dead = []
    if not thesis.structure_intact:
        dead.append("structure broken")
    if thesis.invalidation_touched:
        dead.append("invalidation touched")
    if thesis.displacement_against:
        dead.append("opposing displacement")
    if thesis.target_liquidity_taken:
        dead.append("objective liquidity already taken")
    if dead:
        opts.append(ManagementOption(
            f"M{n}", Action.EXIT, None, None, None,
            f"close remainder — {', '.join(dead)}; "
            f"realises {pos.banked_r + r_open * pos.remaining_fraction:+.2f}R"
        ))

    return opts


# --------------------------------------------------------------------------
# Applying a choice — invariants enforced here, not requested politely
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ManagementDecision:
    option_id: str
    action: Action
    position_after: Position
    banked_now_r: float
    policy_version: str
    rejected_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.rejected_reason is None


def apply_option(
    pos: Position,
    opt: ManagementOption,
    exc: Excursion,
    policy: ManagementPolicy = ManagementPolicy(),
) -> ManagementDecision:
    """Resolve the chosen option and enforce I1-I4. Rejection is a valid result."""

    def reject(why: str) -> ManagementDecision:
        return ManagementDecision(opt.id, opt.action, pos, 0.0, policy.version, why)

    if opt.action is Action.HOLD:
        return ManagementDecision(opt.id, opt.action, pos, 0.0, policy.version)

    if opt.action is Action.EXIT:
        realised = pos.banked_r + exc.r_open_now * pos.remaining_fraction
        closed = Position(**{**pos.__dict__, "remaining_fraction": 0.0,
                             "banked_r": realised})
        return ManagementDecision(opt.id, opt.action, closed,
                                  exc.r_open_now * pos.remaining_fraction, policy.version)

    if opt.action in (Action.PROTECT, Action.TRAIL):
        if opt.new_stop is None:
            return reject("no stop supplied for a stop-moving action")
        # I1 — ratchet only.
        moved_against = (opt.new_stop < pos.current_stop) if pos.long else (opt.new_stop > pos.current_stop)
        if moved_against:
            return reject(f"I1 violated: stop would move against the position "
                          f"({pos.current_stop:.2f} -> {opt.new_stop:.2f})")
        after = Position(**{**pos.__dict__, "current_stop": opt.new_stop})
        # I2 — open risk must not increase.
        if after.open_risk_r > pos.open_risk_r + 1e-9:
            return reject(f"I2 violated: open risk {pos.open_risk_r:.3f}R -> "
                          f"{after.open_risk_r:.3f}R")
        # I3 — locked profit must not fall.
        if after.locked_r < pos.locked_r - 1e-9:
            return reject(f"I3 violated: locked {pos.locked_r:+.3f}R -> {after.locked_r:+.3f}R")
        return ManagementDecision(opt.id, opt.action, after, 0.0, policy.version)

    if opt.action is Action.PARTIAL:
        f = opt.partial_fraction or 0.0
        if not (0.0 < f < 1.0):
            return reject(f"invalid partial fraction {f}")
        # I4 — size only decreases, and a runner must survive.
        banked_now = f * pos.remaining_fraction * exc.r_open_now
        new_frac = pos.remaining_fraction * (1.0 - f)
        if new_frac < policy.min_runner_fraction - 1e-9:
            return reject(f"I4 violated: runner would fall to {new_frac:.2%}, "
                          f"below {policy.min_runner_fraction:.0%}")
        after = Position(**{**pos.__dict__, "remaining_fraction": new_frac,
                            "banked_r": pos.banked_r + banked_now})
        if after.locked_r < pos.locked_r - 1e-9:
            return reject("I3 violated: banking would reduce guaranteed R")
        return ManagementDecision(opt.id, opt.action, after, banked_now, policy.version)

    return reject(f"unhandled action {opt.action}")


# --------------------------------------------------------------------------
# The model's choice surface — an id, and nothing else
# --------------------------------------------------------------------------

MANAGEMENT_SYSTEM = """\
You are managing an open XAUUSD position. You do not set prices, sizes, or \
stops. Code has enumerated every legal move; you choose exactly one by id.

The options are already filtered for legality — each one respects the ratchet, \
cannot increase risk, and cannot re-risk banked profit. So the question is not \
"is this allowed", it is "which of these is right given how the trade is \
actually behaving".

Weigh: whether the thesis that opened the trade is still true, whether the \
excursion so far suggests the move is working or stalling, how much room the \
current volatility demands, and whether banking now protects a result or kills \
a runner that has further to go.

Two failure modes to avoid in both directions. Taking profit early on a trade \
that was working is the desk's most expensive habit and the reason the runner \
question exists at all. Holding a position whose reason has evaporated because \
it is "still green" is the other. Neither is fixed by a rule about R.

Choose the option id, state what you observed, and state the strongest case \
against your choice. If HOLD is right, choose HOLD — doing nothing is a \
decision and it is frequently the correct one.
"""
