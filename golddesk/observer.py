"""Continuous local observation — every tick seen, model woken only when paying.

The desk was blind between M15 closes. Fifteen minutes is a long time in gold:
a position can run +2R and give it all back inside one candle while the code
that manages it is asleep waiting for a bar to finish.

This layer consumes ticks (or M1 bars) continuously and costs nothing per
update — it is arithmetic, not inference. It maintains live excursion, distance
to stop and target, velocity and acceleration, and it decides WHEN reconsidering
the position is worth a model call.

The wake rule is economic, not a timer. Each trigger carries an estimated value
of reconsideration in R: roughly, how much is at stake in the decision that
would be revisited. A trigger only fires when that exceeds the cost of thinking
about it. No fixed cadence, no minimum gap, no quota.

NOTHING HERE CALLS A MODEL. It emits events; the caller decides what to pay for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Optional, Sequence

log = logging.getLogger(__name__)

OBSERVER_VERSION = "obs-2026-08-14-a"


class Trigger(str, Enum):
    MFE_EXTENSION = "MFE_EXTENSION"        # trade reached new favourable ground
    GIVEBACK = "GIVEBACK"                  # surrendered a material share of MFE
    STOP_PROXIMITY = "STOP_PROXIMITY"      # price closing on the stop
    TARGET_PROXIMITY = "TARGET_PROXIMITY"  # objective within reach
    ACCELERATION = "ACCELERATION"          # move speeding up in our favour
    DECELERATION = "DECELERATION"          # momentum dying
    ADVERSE_IMPULSE = "ADVERSE_IMPULSE"    # fast move against
    LEVEL_BREACH = "LEVEL_BREACH"          # structural level crossed
    VOLATILITY_SHOCK = "VOLATILITY_SHOCK"
    BAR_CLOSE = "BAR_CLOSE"
    HEARTBEAT = "HEARTBEAT"


@dataclass(frozen=True)
class WakePolicy:
    """When the expensive brain is allowed to think. This is ECONOMIC POLICY.

    Every value here decides whether a reconsideration happens at all. Raise
    `giveback_fraction` and the desk stops noticing that a runner is bleeding
    out; raise `reconsider_cost_r` and it stops reconsidering cheap-looking but
    decisive moments. Either way the change alters realised capture without
    changing a single line of trading logic, which is exactly the kind of silent
    objective redefinition the constitution forbids.

    They were previously bare defaults on TradeObserver and bare literals inside
    observe(). They are now named, versioned, stamped onto every wake, and
    registered as a discretionary restriction so the ablation ladder and the
    anti-drift auditor can both see them.

    NONE of these is a selectivity dial to be tuned upward for tidiness. They
    are a cost model, and the only evidence that may move them is measured
    forgone capture during unwoken periods.
    """
    version: str = "wake-2026-08-14-a"
    # Cost of one reconsideration in R: model call cost / account risk per trade.
    reconsider_cost_r: float = 0.01
    # Share of MFE surrendered before giveback is material.
    giveback_fraction: float = 0.33
    # Re-arm the giveback trigger once price recovers to this share of the band.
    giveback_rearm: float = 0.5
    # How close to stop/target counts as "close", in R.
    proximity_r: float = 0.25
    # New favourable ground must exceed this, in R, to be more than noise.
    mfe_step_r: float = 0.25
    # Velocity multiples that count as acceleration / deceleration.
    accel_multiple: float = 2.0
    decel_multiple: float = 0.25
    # Adverse velocity, in R per minute, that counts as an impulse against.
    adverse_r_per_min: float = -1.0

    def stamp(self) -> dict:
        return {"wake_policy": self.version}


@dataclass(frozen=True)
class Wake:
    ts: datetime
    triggers: tuple[Trigger, ...]
    value_at_stake_r: float     # what the revisited decision is worth
    detail: str

    def render(self) -> str:
        return (f"{self.ts:%H:%M:%S} {'+'.join(t.value for t in self.triggers)} "
                f"({self.value_at_stake_r:.2f}R at stake) {self.detail}")


@dataclass
class TradeObserver:
    """Live, tick-resolution view of one open position."""

    direction: Literal["LONG", "SHORT"]
    entry: float
    stop: float
    target: float
    risk_price: float
    opened: datetime

    wake: "WakePolicy" = field(default_factory=lambda: WakePolicy())

    mfe_r: float = 0.0
    mae_r: float = 0.0
    t_mfe: Optional[datetime] = None
    t_mae: Optional[datetime] = None
    last_price: Optional[float] = None
    last_ts: Optional[datetime] = None
    velocity_r_per_min: float = 0.0
    prev_velocity: float = 0.0
    ticks: int = 0
    last_wake: Optional[datetime] = None
    _fired: set = field(default_factory=set)
    path: list = field(default_factory=list)     # (ts, r) — full excursion path

    @property
    def long(self) -> bool:
        return self.direction == "LONG"

    def r_at(self, price: float) -> float:
        d = (price - self.entry) if self.long else (self.entry - price)
        return d / self.risk_price

    def note_extremes(self, low: float, high: float, ts: datetime) -> None:
        """Fold a completed bar's range into MFE/MAE without inventing a path.

        Used when no tick stream is attached. Deliberately does NOT touch
        velocity: the two extremes carry no ordering, and feeding them as if
        they were consecutive observations would fabricate an acceleration
        reading out of an unknown intrabar sequence.
        """
        for px in (low, high):
            r = self.r_at(px)
            if r > self.mfe_r:
                self.mfe_r, self.t_mfe = r, ts
            if r < self.mae_r:
                self.mae_r, self.t_mae = r, ts

    # ------------------------------------------------------------------
    def observe(self, price: float, ts: datetime,
                heartbeat: timedelta = timedelta(minutes=30),
                bar_closed: bool = False) -> Optional[Wake]:
        """One tick. Cheap. Returns a Wake only when thinking is worth paying for."""
        w = self.wake
        self.ticks += 1
        r = self.r_at(price)
        self.path.append((ts, round(r, 4)))

        if self.last_ts is not None:
            dt_min = max((ts - self.last_ts).total_seconds() / 60.0, 1e-6)
            self.prev_velocity = self.velocity_r_per_min
            self.velocity_r_per_min = (r - self.r_at(self.last_price)) / dt_min
        self.last_price, self.last_ts = price, ts

        triggers: list[Trigger] = []
        detail: list[str] = []

        if r > self.mfe_r + 1e-9:
            prev = self.mfe_r
            self.mfe_r, self.t_mfe = r, ts
            if r >= prev + w.mfe_step_r:         # material new ground, not noise
                triggers.append(Trigger.MFE_EXTENSION)
                detail.append(f"MFE {prev:+.2f}->{r:+.2f}R")
        if r < self.mae_r - 1e-9:
            self.mae_r, self.t_mae = r, ts

        # surrendered a material share of the best it achieved
        if self.mfe_r > 0.5:
            given = self.mfe_r - r
            if given >= self.mfe_r * w.giveback_fraction and "gb" not in self._fired:
                triggers.append(Trigger.GIVEBACK)
                self._fired.add("gb")
                detail.append(f"gave back {given:+.2f}R of {self.mfe_r:+.2f}R MFE")
            elif given < self.mfe_r * w.giveback_fraction * w.giveback_rearm:
                self._fired.discard("gb")        # re-arm after recovery

        to_stop = abs(r - self.r_at(self.stop))
        to_target = abs(self.r_at(self.target) - r)
        if to_stop <= w.proximity_r:
            triggers.append(Trigger.STOP_PROXIMITY)
            detail.append(f"{to_stop:.2f}R from stop")
        if to_target <= w.proximity_r:
            triggers.append(Trigger.TARGET_PROXIMITY)
            detail.append(f"{to_target:.2f}R from target")

        if self.prev_velocity and self.velocity_r_per_min > 0:
            if self.velocity_r_per_min > w.accel_multiple * abs(self.prev_velocity):
                triggers.append(Trigger.ACCELERATION)
                detail.append(f"{self.velocity_r_per_min:.2f}R/min")
            elif abs(self.velocity_r_per_min) < w.decel_multiple * abs(self.prev_velocity):
                triggers.append(Trigger.DECELERATION)
        if self.velocity_r_per_min < w.adverse_r_per_min:
            triggers.append(Trigger.ADVERSE_IMPULSE)
            detail.append(f"{self.velocity_r_per_min:.2f}R/min against")

        if bar_closed:
            triggers.append(Trigger.BAR_CLOSE)
        stale = self.last_wake is None or (ts - self.last_wake) >= heartbeat
        if stale:
            triggers.append(Trigger.HEARTBEAT)

        if not triggers:
            return None

        # Economic gate: what is the decision worth revisiting?
        stake = self._value_at_stake(r)
        if stake < w.reconsider_cost_r:
            return None
        self.last_wake = ts
        return Wake(ts, tuple(triggers), stake, "; ".join(detail) or "state change")

    def _value_at_stake_components(self, r: float) -> tuple[float, float]:
        at_risk = max(0.0, r - self.r_at(self.stop))        # unrealised that could vanish
        remaining = max(0.0, self.r_at(self.target) - r)     # upside still on the table
        return at_risk, remaining

    def _value_at_stake(self, r: float) -> float:
        at_risk, remaining = self._value_at_stake_components(r)
        return max(at_risk, remaining)

    def snapshot(self) -> dict:
        return {"observer_version": OBSERVER_VERSION,
                "wake_policy": self.wake.version, "ticks": self.ticks,
                "mfe_r": round(self.mfe_r, 4), "mae_r": round(self.mae_r, 4),
                "t_mfe": self.t_mfe.isoformat() if self.t_mfe else None,
                "t_mae": self.t_mae.isoformat() if self.t_mae else None,
                "path_points": len(self.path)}


# --------------------------------------------------------------------------
# Intrabar execution — resolve a trade on the finest series available
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FillEvent:
    ts: datetime
    kind: Literal["STOP", "TARGET", "PARTIAL", "TIMEOUT"]
    price: float
    r: float


def resolve_intrabar(prices: Sequence[tuple[datetime, float]], entry: float,
                     stop: float, target: float, direction: str,
                     risk_price: float) -> Optional[FillEvent]:
    """First touch on an ORDERED price series — no intrabar guessing.

    M15 OHLC cannot say whether the stop or the target came first, so the
    coarse resolver assumes the stop (pessimistic, and wrong roughly half the
    time). Given M1 closes or ticks, the ordering is observed rather than
    assumed, which is the difference between simulating management and
    pretending to.
    """
    long = direction == "LONG"
    for ts, p in prices:
        if (p <= stop) if long else (p >= stop):
            return FillEvent(ts, "STOP", stop, (stop - entry) / risk_price
                             * (1 if long else -1))
        if (p >= target) if long else (p <= target):
            return FillEvent(ts, "TARGET", target, abs(target - entry) / risk_price)
    return None
