#!/usr/bin/env python3
"""Sizing, execution, uncertainty and the management counterfactual. #3 #5 #10 #15.

Each of these replaces a constant with a decision, and each is checked against
the specific way it could be wrong:

  #3  a Kelly map that saturates its cap on every positive edge is not sizing,
      it is a constant wearing a formula. The raw figure must be visible.
  #5  modelling a missed fill as costing zero makes LIMIT dominate at any drift
      and the desk patiently misses everything. Missing must cost the edge.
  #10 six components must NOT collapse to a scalar, and absence must read
      UNKNOWN rather than quietly reading LOW.
  #15 a replay that never diverges from the incumbent is not an alternative,
      and a win on the incumbent's own paths is a hypothesis, not a promotion.
"""

from __future__ import annotations

import sys

OK, BAD = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


def sizing() -> None:
    from golddesk.allocation import (correlation_adjusted_room, default_size,
                                     drawdown_scalar, kelly_fraction)
    print("#3 SIZING — a decision, and an honest one about its own limits")

    cold = default_size(cohort_n=20)
    check("a 20-trade cohort gets flat 1R, not a modelled size",
          cold.risk_r == 1.0 and "flat 1R" in cold.basis, cold.render().strip())

    hot = default_size(cohort_n=400, win_rate=0.58, rr=2.0, cost_r=0.02)
    check("a measured edge sizes above the cold-start default", hot.risk_r > 1.0,
          hot.render().strip())
    check("the UNCAPPED Kelly figure is surfaced, not hidden by the cap",
          "wants" in hot.basis and "capped at" in hot.basis,
          "a cap that binds silently is a constant pretending to be a model")

    check("a negative edge sizes to zero",
          kelly_fraction(0.30, 1.0, 0.05) == 0.0)
    check("correlated copies consume more room than their nominal risk",
          correlation_adjusted_room(1.0, 3.0, 3) < correlation_adjusted_room(1.0, 3.0, 1),
          f"{correlation_adjusted_room(1.0, 3.0, 3):.2f}R vs "
          f"{correlation_adjusted_room(1.0, 3.0, 1):.2f}R")
    check("size shrinks toward the daily limit but never to zero",
          0.25 <= drawdown_scalar(-2.9, 3.0) < 0.35, f"{drawdown_scalar(-2.9, 3.0):.3f}")

    heavy = default_size(cohort_n=400, win_rate=0.58, rr=2.0,
                         open_risk_r=1.8, max_open_risk_r=2.0, same_direction=1)
    check("portfolio heat caps the size and says which cap bound it",
          heavy.capped_by == "portfolio heat", heavy.render().strip())


def execution() -> None:
    from golddesk.allocation import plan_entry
    print("\n#5 EXECUTION — missing a fill costs the edge")

    at = plan_entry(spread=0.30, risk_price=18.0, drift_r=0.0, atr=4.0,
                    edge_r=0.40, trigger_price=2000.0, mid=2000.0)
    check("at the trigger, go to market", at.style == "MARKET", at.render().strip())

    patient = plan_entry(spread=0.30, risk_price=18.0, drift_r=0.30, atr=4.0,
                         edge_r=0.40, trigger_price=2000.0, mid=2003.0, urgency=0.1)
    urgent = plan_entry(spread=0.30, risk_price=18.0, drift_r=0.30, atr=4.0,
                        edge_r=0.40, trigger_price=2000.0, mid=2003.0, urgency=0.95)
    check("urgency changes the answer on identical geometry",
          patient.style != urgent.style,
          f"{patient.style} at urgency 0.1 vs {urgent.style} at 0.95")

    chase = plan_entry(spread=0.30, risk_price=18.0, drift_r=0.9, atr=4.0,
                       edge_r=0.40, trigger_price=2000.0, mid=2016.0)
    check("a long run from the trigger stands aside rather than chasing",
          chase.style == "STAND_ASIDE", chase.render().strip())

    # The defect this module was written around: with the no-fill branch valued
    # at zero, a limit wins at every drift and the desk stops entering.
    tiny = plan_entry(spread=0.30, risk_price=18.0, drift_r=0.25, atr=4.0,
                      edge_r=0.02, trigger_price=2000.0, mid=2002.5, urgency=0.5)
    big = plan_entry(spread=0.30, risk_price=18.0, drift_r=0.25, atr=4.0,
                     edge_r=3.00, trigger_price=2000.0, mid=2002.5, urgency=0.5)
    check("a large edge is more willing to pay up than a small one",
          not (tiny.style == "MARKET" and big.style != "MARKET"),
          f"edge 0.02R -> {tiny.style}; edge 3.00R -> {big.style}")
    check("every plan states its fill probability",
          all(0.0 <= p.fill_probability <= 1.0
              for p in (at, patient, urgent, chase, tiny, big)))


def uncertainty() -> None:
    from golddesk.uncertainty import assess
    print("\n#10 UNCERTAINTY — six sources, deliberately not combined")

    u = assess(n_resolved=0, similarity=None, tick_age_s=90.0, max_age_s=30.0,
               views={"a": "LONG", "b": "SHORT"}, spread=0.5, risk_price=4.0,
               minutes_to_event=15, event_name="CPI")
    names = [c.name for c in u.components]
    check("all six components are present", len(u.components) == 6, ", ".join(names))
    check("no scalar confidence is produced",
          not hasattr(u, "score") and not hasattr(u, "confidence"))
    check("a stale quote reads HIGH on data, not on everything",
          next(c.level for c in u.components if c.name == "data") == "HIGH")
    check("conflicting views read HIGH on model",
          next(c.level for c in u.components if c.name == "model") == "HIGH")
    check("an absent regime comparison reads UNKNOWN, never LOW",
          next(c.level for c in u.components if c.name == "regime") == "UNKNOWN")

    clean = assess(n_resolved=400, similarity=0.85, tick_age_s=1.0,
                   views={"a": "LONG", "b": "LONG"}, spread=0.2, risk_price=18.0,
                   minutes_to_event=600, event_name="NFP")
    check("a genuinely well-supported state has no HIGH component",
          clean.highest == [], f"{len(clean.components)} components, none HIGH")
    check("every component carries the fact behind its label",
          all(c.basis for c in u.components + clean.components))
    print(u.render())


def counterfactual() -> None:
    from mgmt_counterfactual import compare_policies, replay_policy, report
    print("\n#15 MANAGEMENT COUNTERFACTUAL — replayed on the recorded path")

    def trade(tid, realised, chosen, shadow, r_open, path):
        return {"kind": "TRADE_CLOSED", "entry_t0": tid, "realised_r": realised,
                "cost_r": 0.05, "path": path,
                "management": [{"options": ["HOLD", "PROTECT", "TRAIL"],
                                "chosen": chosen, "shadow": shadow,
                                "r_open": r_open}]}

    rows = [trade(f"t{k}", -1.0, "HOLD",
                  {"eager": "PROTECT", "twin": "HOLD"}, 1.4,
                  [["a", 0.0], ["b", 1.4], ["c", 0.2], ["d", -1.0]])
            for k in range(8)]

    eager = replay_policy(rows[0], "eager")
    twin = replay_policy(rows[0], "twin")
    check("an alternative that acts differently is recorded as diverging",
          eager is not None and eager.diverged_at is not None,
          f"diverged at step {eager.diverged_at}" if eager else "no replay")
    check("an alternative that made the same choice never diverges",
          twin is not None and twin.diverged_at is None)
    check("and a non-divergent replay has a delta of EXACTLY zero",
          twin is not None and twin.delta_r == 0.0,
          f"delta {twin.delta_r:+.4f}R — anything else is a fabricated difference"
          if twin else "")
    check("protecting on this path beats the incumbent's -1.00R",
          eager is not None and eager.realised_r > eager.actual_r,
          eager.render().strip() if eager else "")

    comps = {c.policy: c for c in compare_policies(rows)}
    check("a non-diverging policy is called out as not an alternative",
          "not an alternative" in comps["twin"].verdict, comps["twin"].verdict[:70])
    check("a winning policy on 8 trades is UNDETERMINED, not promoted",
          "UNDETERMINED" in comps["eager"].verdict, comps["eager"].verdict[:70])

    many = rows * 5                                   # 40 replayed trades
    c40 = {c.policy: c for c in compare_policies(many)}
    check("even at 40 trades the verdict is 'seal it', never 'use it'",
          "seal it as a hypothesis" in c40["eager"].verdict,
          c40["eager"].verdict[:80])

    empty = report([{"kind": "TRADE_CLOSED", "realised_r": -1.0}])
    check("a ledger with no recorded path says so instead of inventing one",
          "No trade carries a path" in empty)
    print(report(rows))


def main() -> int:
    sizing()
    execution()
    uncertainty()
    counterfactual()
    print(f"\n{OK} ok, {BAD} failed")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
