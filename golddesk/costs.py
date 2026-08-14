"""Canonical execution cost. One function, one definition, used everywhere.

This module exists because the desk had cost logic in more than one place and
they disagreed. The previous analyst.py charged `spread * 2.0` on top of an
entry already quoted at the ask — roughly 2.5 spreads for a round trip that
costs about one. R:R came out understated, so the desk refused trades it should
have taken. That is the same class of defect as a research harness using one
cost model while production uses another.

RULE: research/backtest.py and analyst.py must both import round_trip_cost().
If you find a second cost calculation anywhere in the codebase, delete it.

Convention: all prices passed in are MID. The round trip is modelled as
buy-at-ask / sell-at-bid (or the mirror), so the full quoted spread is charged
exactly once, plus commission converted to price terms.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """Per-venue. XAUUSD: 1 lot = 100 oz, so $/lot commission / 100 = $/oz."""
    contract_size: float = 100.0
    commission_per_lot_round_turn: float = 0.0   # account currency, both sides
    slippage_price: float = 0.0                  # add measured slippage here

    def commission_in_price(self) -> float:
        return self.commission_per_lot_round_turn / self.contract_size


def round_trip_cost(spread: float, model: CostModel = CostModel()) -> float:
    """Total round-trip cost in PRICE terms, charged once.

    Entry crosses half the spread, exit crosses the other half — one full
    spread across the round trip, not two. Commission and any measured
    slippage are added on top.
    """
    if spread < 0:
        raise ValueError("negative spread")
    return spread + model.commission_in_price() + model.slippage_price


def cost_in_r(spread: float, risk_price: float, model: CostModel = CostModel()) -> float:
    """Round-trip cost expressed in R. Compare this against measured medians."""
    if risk_price <= 0:
        raise ValueError("non-positive risk distance")
    return round_trip_cost(spread, model) / risk_price


def net_rr(entry: float, stop: float, target: float, spread: float,
           model: CostModel = CostModel()) -> tuple[float, float, float]:
    """(risk_price, net_rr, cost_r) for a proposal. All prices are MID."""
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("non-positive risk distance")
    cost = round_trip_cost(spread, model)
    return risk, (abs(target - entry) - cost) / risk, cost / risk


def breakeven_win_rate(rr: float, cost_r: float) -> float:
    """The hurdle, computed — never hard-coded into a prompt.

    p*rr - (1-p)*1 - cost_r = 0  ->  p = (1 + cost_r) / (1 + rr)

    At rr=1.8, cost=0    -> 0.357  (the textbook figure)
    At rr=1.8, cost=0.10 -> 0.393
    """
    if rr <= -1:
        raise ValueError("degenerate reward-to-risk")
    return (1.0 + cost_r) / (1.0 + rr)
