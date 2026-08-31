"""How much to bank at TP1, decided from live conditions rather than a constant.

WHY NOT A CONSTANT

Banking a fixed half of every position at TP1 treats a young, aligned, low-
volatility trend exactly like an exhausted one in an extreme-volatility tape.
Those want opposite treatment: the first has a runner worth protecting FROM the
bank, the second has a runner worth protecting AGAINST. A constant is a decision
not to look.

WHAT THIS IS AND IS NOT

It is a STATED HEURISTIC over MEASURED FIELDS, bounded, with every term carrying
its reason into the ledger. It is NOT optimal and must not be described as such:
there are zero resolved trades behind it, so nothing here is fitted to anything.
Each term is a hypothesis about giveback, written down so the ledger can price
it later -- `why` travels with every decision precisely so a future analysis can
ask "did banking more in EXHAUSTED actually beat banking less", per mechanism,
from evidence rather than from this file's opinion.

THE DIRECTION OF EACH TERM, since that is the falsifiable part:

  bank MORE when giveback is likelier -- an exhausted trend, extreme volatility,
  conflicted higher-timeframe structure, a runner with little left to earn.

  bank LESS when continuation is likelier -- a young trend, aligned structure,
  ordinary volatility, a TP2 far enough away that the runner is the trade.

BOUNDED ON BOTH SIDES. Never below MIN (a bank so small it does not change the
trade's character is theatre) and never above MAX (a bank so large the runner
cannot pay for the give-up defeats the point of having one). The band is the
part that keeps a wrong term from being catastrophic while it is still unpriced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PARTIAL_POLICY_VERSION = "partial-2026-08-28-a"

#: Neutral starting point. Half is the plainest answer to "some now, some later"
#: and it is where every adjustment below starts from, not where it ends.
BASE = 0.50

#: The band. Outside it the bank stops being a bank: too small and it does not
#: change the trade, too large and the runner cannot pay for what was given up.
MIN, MAX = 0.25, 0.75

#: Maturity is the strongest single signal about whether a move has more in it.
#: EXHAUSTED is where MFE most often becomes giveback.
_MATURITY = {"YOUNG": -0.15, "MID": -0.05, "MATURE": +0.05, "EXHAUSTED": +0.15}

#: Volatility cuts both ways and the asymmetry is deliberate: EXTREME vol gives
#: a runner more room AND takes it away faster, and the stop is what pays for
#: the second. LOW vol earns a smaller bank because there is less to give back.
_VOL = {"LOW": -0.05, "NORMAL": 0.0, "ELEVATED": +0.05, "EXTREME": +0.10}

#: Structure agreeing with the trade is the runner's best support.
_ALIGN = {"ALIGNED": -0.10, "NEUTRAL": 0.0, "CONFLICTED": +0.10}


@dataclass(frozen=True)
class PartialPlan:
    fraction: float
    why: str
    version: str = PARTIAL_POLICY_VERSION


def tp1_fraction(*, trend_maturity: str = "MID",
                 volatility_state: str = "NORMAL",
                 htf_alignment: str = "NEUTRAL",
                 with_trend: bool = True,
                 rr_tp1: Optional[float] = None,
                 rr_tp2: Optional[float] = None) -> PartialPlan:
    """Fraction of the remaining position to bank at TP1, and why.

    Every argument is a measured field or a compiled number. Nothing here reads
    the model's prose, and nothing here can refuse a trade.
    """
    f = BASE
    reasons: list[str] = []

    m = _MATURITY.get(trend_maturity)
    if m:
        f += m
        reasons.append(f"maturity {trend_maturity} {m:+.2f}")

    v = _VOL.get(volatility_state)
    if v:
        f += v
        reasons.append(f"volatility {volatility_state} {v:+.2f}")

    # Alignment only helps a runner that is going WITH the higher timeframe.
    # Counter-trend into an ALIGNED move is the desk's worst measured cohort, so
    # the same reading that protects a with-trend runner argues for banking more
    # against one -- the sign flips rather than the term disappearing.
    a = _ALIGN.get(htf_alignment, 0.0)
    if a:
        adj = a if with_trend else -a
        f += adj
        reasons.append(f"HTF {htf_alignment}"
                       f"{' with' if with_trend else ' AGAINST'} {adj:+.2f}")

    # HOW MUCH IS LEFT TO EARN. If TP2 is barely beyond TP1 the runner has
    # little upside and a lot of round trip, so bank more; if TP2 is far, the
    # runner IS the trade and banking heavily at the first objective throws away
    # the part that pays.
    if rr_tp1 and rr_tp2 and rr_tp1 > 0:
        headroom = (rr_tp2 - rr_tp1) / rr_tp1
        if headroom < 0.25:
            f += 0.10
            reasons.append(f"TP2 only {headroom:.0%} beyond TP1 +0.10")
        elif headroom > 1.0:
            f -= 0.10
            reasons.append(f"TP2 {headroom:.0%} beyond TP1 -0.10")

    clamped = min(max(f, MIN), MAX)
    if clamped != f:
        reasons.append(f"clamped {f:.2f}->{clamped:.2f} into [{MIN:.2f},{MAX:.2f}]")

    return PartialPlan(round(clamped, 4),
                       "; ".join(reasons) if reasons else "no adjustment from base")
