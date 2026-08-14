"""Sizing by marginal portfolio value, and how to get in. Items #3 and #5.

TWO DECISIONS THAT WERE PREVIOUSLY CONSTANTS

  How much risk does this opportunity get?   Answer: 1R. Always. Regardless of
  edge, confidence, correlation with what is already open, or drawdown state.

  How do we get in?                          Answer: at market. Always.
  Regardless of spread, of how far price has run, or of whether a limit would
  fill at a materially better level.

Both are defensible defaults and neither is a decision. This module makes them
decisions — and, per the constitution, makes them decisions that must earn their
place, because a sizing model that is wrong is more dangerous than a flat 1R.

WHY MARGINAL, NOT ABSOLUTE

The question is never "is this a good trade". It is "what does adding this to
what I already hold do to the portfolio". A second highly-correlated gold long
at full size is not diversification, it is leverage with extra steps. So size
scales with the CORRELATION-ADJUSTED contribution, and the adjustment uses the
haircut the risk engine already applies rather than inventing a second one.

THE HONEST LIMIT, UP FRONT

Kelly-style sizing needs an edge estimate, and this desk does not have one. The
baseline is -7.8R over 20 trades. So `kelly_fraction` is available and is NOT
wired into the default path: with an unmeasured edge it would size confidently
off noise, which is worse than sizing flat. `default_size()` returns 1R until
`cohort_n` clears a bar, and says so on every call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence


# --------------------------------------------------------------------------
# #3 Sizing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Allocation:
    risk_r: float
    basis: str
    capped_by: Optional[str] = None

    def render(self) -> str:
        cap = f" (capped by {self.capped_by})" if self.capped_by else ""
        return f"  risk {self.risk_r:.2f}R{cap} — {self.basis}"


def kelly_fraction(win_rate: float, rr: float, cost_r: float = 0.0,
                   fraction: float = 0.25) -> float:
    """Fractional Kelly on a two-outcome bet. Correct, and rarely applicable.

    b is the net payoff on a win after costs. Full Kelly is famously too
    aggressive when p is estimated rather than known, and p here is always
    estimated, so the default is quarter-Kelly. Even that assumes the estimate
    is unbiased, which for a cohort of thirty trades it is not.
    """
    b = rr - cost_r
    if b <= 0:
        return 0.0
    p, q = min(max(win_rate, 0.0), 1.0), 1.0 - min(max(win_rate, 0.0), 1.0)
    f = (b * p - q) / b
    return max(0.0, f * fraction)


def correlation_adjusted_room(open_risk_r: float, max_open_risk_r: float,
                              same_direction: int,
                              haircut: float = 0.65) -> float:
    """How much NEW risk the portfolio can take, discounting correlated copies.

    Each existing position in the same direction counts for more than its
    nominal risk, because in gold they are close to the same bet. The haircut is
    the one the risk engine already uses — a second, different number here would
    mean two answers to one question.
    """
    effective = open_risk_r * (1.0 + haircut * max(0, same_direction - 1))
    return max(0.0, max_open_risk_r - effective)


def drawdown_scalar(day_loss_r: float, max_daily_loss_r: float) -> float:
    """Shrink as the day's loss approaches the hard limit.

    Not a superstition about "bad days": it is the arithmetic of a hard floor.
    Approaching a limit that stops trading entirely, smaller size buys more
    attempts, and attempts are what the objective needs.
    """
    if max_daily_loss_r <= 0:
        return 1.0
    used = min(1.0, max(0.0, -day_loss_r) / max_daily_loss_r)
    return max(0.25, 1.0 - used)


def default_size(*, cohort_n: int = 0, win_rate: Optional[float] = None,
                 rr: float = 0.0, cost_r: float = 0.0,
                 open_risk_r: float = 0.0, max_open_risk_r: float = 2.0,
                 same_direction: int = 0, haircut: float = 0.65,
                 day_loss_r: float = 0.0, max_daily_loss_r: float = 3.0,
                 min_cohort_for_kelly: int = 100) -> Allocation:
    """The size this opportunity gets. Flat 1R until the edge is measured.

    `min_cohort_for_kelly` is high on purpose. Sizing off a thirty-trade hit
    rate is how a system turns an unlucky streak into a permanent capital
    reduction, and this desk currently has twenty trades in total.
    """
    room = correlation_adjusted_room(open_risk_r, max_open_risk_r,
                                     same_direction, haircut)
    dd = drawdown_scalar(day_loss_r, max_daily_loss_r)

    if cohort_n < min_cohort_for_kelly or win_rate is None:
        base = 1.0
        basis = (f"flat 1R — cohort has {cohort_n} resolved trades, below the "
                 f"{min_cohort_for_kelly} required before an edge estimate may "
                 f"size anything")
    else:
        f = kelly_fraction(win_rate, rr, cost_r)
        # Kelly is expressed as a fraction of capital; 1R is max_risk_per_trade.
        # At a 58% hit rate and 2R, quarter-Kelly still wants ~9% of capital,
        # which against a 0.5% risk unit is eighteen R. The cap is not a
        # formality — it binds on essentially every positive edge, and that is
        # the honest reading: Kelly sizes for a known p, and p here is estimated
        # from a few hundred trades at best.
        raw_r = f / 0.005
        base = max(0.25, min(2.0, raw_r))
        basis = (f"quarter-Kelly on a measured {win_rate:.0%} over {cohort_n} "
                 f"trades at {rr:.2f}R wants {raw_r:.1f}R"
                 + (f", capped at 2.0R" if raw_r > 2.0 else ""))

    sized = base * dd
    capped = None
    if sized > room:
        sized, capped = room, "portfolio heat"
    elif dd < 1.0:
        capped = "drawdown state"
    return Allocation(round(max(0.0, sized), 3), basis, capped)


# --------------------------------------------------------------------------
# #5 Execution
# --------------------------------------------------------------------------

Style = Literal["MARKET", "LIMIT", "WAIT_FOR_RETEST", "STAND_ASIDE"]


@dataclass(frozen=True)
class ExecutionPlan:
    style: Style
    price: Optional[float]
    expected_cost_r: float
    fill_probability: float
    basis: str

    def render(self) -> str:
        p = f" at {self.price:.2f}" if self.price else ""
        return (f"  {self.style}{p}  cost {self.expected_cost_r:.3f}R  "
                f"P(fill) {self.fill_probability:.0%} — {self.basis}")


def plan_entry(*, spread: float, risk_price: float, drift_r: float,
               atr: float, edge_r: float, trigger_price: Optional[float] = None,
               mid: Optional[float] = None,
               urgency: float = 0.5,
               slippage_price: float = 0.0) -> ExecutionPlan:
    """MARKET, LIMIT, retest or stand aside — decided by cost against edge decay.

    `drift_r` is how far price has already travelled from the structural trigger,
    in R. It is the same number the anti-chase gate uses, and it drives this
    decision because it captures both halves of the trade-off: a large drift
    means a market order pays up AND that a limit back at the trigger is less
    likely to fill before the move is over.

    `edge_r` is the expected value of the trade in R. It is REQUIRED, and the
    reason is the same principle the rest of the desk runs on: without it, not
    filling costs nothing, so a limit order always wins the expected-value
    comparison and the desk quietly stops entering. Missing a positive-EV trade
    is a cost, and it has to appear in the arithmetic or the arithmetic argues
    for never trading.

    `urgency` (0..1) is how quickly the edge decays. At 1 the opportunity is
    gone in moments and a worse fill beats no fill; at 0 there is time to be
    patient. It is an INPUT, not a constant — the caller supplies it from the
    mechanism, and it is stamped on the plan so its value can be audited later.
    """
    if risk_price <= 0:
        return ExecutionPlan("STAND_ASIDE", None, 0.0, 0.0, "no risk unit")
    market_cost = (spread + slippage_price) / risk_price

    # Cost of paying up is already in market_cost. The question is whether
    # waiting is cheaper in expectation, and it only is when a fill is likely.
    if drift_r <= 0.1:
        return ExecutionPlan("MARKET", mid, round(market_cost, 4), 1.0,
                             "price is at the trigger; nothing to gain by waiting")

    # A limit back at the trigger saves the drift but risks never filling.
    # Fill probability falls with drift and with urgency.
    p_fill = max(0.05, min(0.95, math.exp(-1.2 * drift_r) * (1.0 - 0.5 * urgency)))
    limit_cost = market_cost - drift_r          # saving the drift is the gain
    # NOT FILLING FORFEITS THE EDGE. Modelling the no-fill branch as zero treats
    # a missed positive-EV trade as free, which is the exact accounting error
    # the objective forbids — and it makes LIMIT dominate at any drift, so the
    # desk would patiently miss everything.
    ev_market = edge_r - market_cost
    ev_limit = p_fill * (edge_r - limit_cost) + (1 - p_fill) * 0.0

    if drift_r > 0.6 and p_fill < 0.3:
        return ExecutionPlan("STAND_ASIDE", None, round(market_cost, 4), 0.0,
                             f"price ran {drift_r:.2f}R from the trigger and a "
                             f"retest is unlikely ({p_fill:.0%}) — paying up here "
                             f"is the chase the anti-chase gate exists to stop")
    if ev_limit > ev_market and trigger_price:
        return ExecutionPlan("LIMIT", trigger_price, round(limit_cost, 4),
                             round(p_fill, 3),
                             f"waiting at the trigger saves {drift_r:.2f}R and "
                             f"fills {p_fill:.0%} of the time")
    if urgency < 0.3 and trigger_price:
        return ExecutionPlan("WAIT_FOR_RETEST", trigger_price,
                             round(limit_cost, 4), round(p_fill, 3),
                             "edge decays slowly; a retest is worth waiting for")
    return ExecutionPlan("MARKET", mid, round(market_cost, 4), 1.0,
                         f"drift {drift_r:.2f}R but the edge decays too fast to wait "
                         f"(urgency {urgency:.1f})")
