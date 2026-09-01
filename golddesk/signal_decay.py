"""Decision expiry + latency model (#3).

A signal is a decaying asset, and the analyst states its half-life. The desk
learns nothing from a +EV idea that arrives after the moment that made it
valuable, so:

  - every compiled signal carries `expires_at_utc` derived from the read's own
    `expected_half_life_minutes`;
  - the LIVE path reprices the raw edge by the demand curve before entry —
    `decay_multiplier(age_minutes, half_life_minutes)` — and refuses to fill
    an idea whose value has decayed below a fillable fraction;
  - latency (analysis time, chance to fill) is journaled with every decision so
    the desk learns empirically how edge decays at ITS latency, not a guess.

The curve is a pure half-life exponential. The empirics — how edge actually
decayed across booked decisions — are measured separately in the ledger.
"""

from __future__ import annotations

import math
from typing import Optional

#: an idea this old (measured in half-lives) is not worth filling
MIN_FILLABLE_DECAY = 0.25                # two half-lives


def decay_multiplier(age_minutes: float, half_life_minutes: float) -> float:
    """Fraction of raw edge that remains after `age_minutes`.

    value(age) = 0.5 ** (age / half_life). Pure, deterministic, monotone.
    """
    if half_life_minutes <= 0:
        return 0.0
    if age_minutes <= 0:
        return 1.0
    return 0.5 ** (age_minutes / half_life_minutes)


def is_fillable(age_minutes: float, half_life_minutes: Optional[float],
                floor: float = MIN_FILLABLE_DECAY) -> tuple[bool, float]:
    """Whether a signal is still worth the desk's time.

    A missing half-life uses the desk default (90 min) — the compiler stamps
    `expires_at_utc` from the same rule. Returns (fillable, remaining_fraction).
    """
    hl = half_life_minutes if half_life_minutes and half_life_minutes > 0 else 90.0
    frac = decay_multiplier(age_minutes, hl)
    return frac >= floor, frac


def repriced_ev(raw_r: float, age_minutes: float,
                half_life_minutes: Optional[float]) -> float:
    """The idea's EV after decay, in the same units as `raw_r`.
    Used for NOTIFY only — the desk never re-sizes an aged idea into an order.
    """
    hl = half_life_minutes if half_life_minutes and half_life_minutes > 0 else 90.0
    return raw_r * decay_multiplier(age_minutes, hl)


def half_lives_to(minutes: float, half_life_minutes: float) -> float:
    if half_life_minutes <= 0:
        return 0.0
    return minutes / half_life_minutes