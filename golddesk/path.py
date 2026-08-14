"""The SHAPE of a trade, not just whether it won. Item #14.

WHY SHAPE IS THE THING WORTH PREDICTING

Direction is one bit and it is the bit everyone models. The ledger says the
desk's problem is not direction: 15 of 20 trades reached +1R and 2 survived.
Entry was fine. What killed it was the PATH — how far a trade ran before it
came back, whether it came back through the entry, how long it took, and how
much of the excursion was still there when it closed.

Those are the facts management acts on, and management is where the R went. A
model that says "this is a 58% long" tells the manager nothing. A model that
says "trades from this state typically reach +1.4R within 40 minutes and give
back 80% of it if not banked" tells the manager exactly what to do.

WHAT IS PREDICTED, AND WHY EACH ONE

  reach_1r        P(the trade ever touches +1R). Decides whether a
                  break-even move is even reachable.
  mfe_median      how far it typically runs. The realistic target, as
                  distinct from the structural one.
  giveback        fraction of MFE surrendered by close under the incumbent
                  policy. This is the desk's largest measured leak.
  minutes_to_mfe  when the peak arrives. A trade that peaks in 20 minutes
                  and one that peaks in 6 hours need different management.
  adverse_first   P(MAE is reached before MFE). Whether to expect heat first.

THIS IS A REFERENCE CLASS, NOT A MODEL

It does not fit anything. It conditions on the discrete Context the desk
already computes, finds resolved trades in a matching state, and reports what
they did — with the cohort size attached to every single number. That is a
deliberate ceiling: with twenty resolved trades, a fitted model would be
fitting noise, and the honest tool at this sample size is a lookup with its own
sample size printed next to it.

When the sample supports more, prediction_layer.py's admissibility test is the
gate that says so. This module refuses to pretend that day has arrived.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

PATH_VERSION = "path-2026-08-14-a"

# Below this, a conditional estimate is a story about a handful of trades.
MIN_COHORT = 20
# Below this it is worth reporting but must never be acted on unquestioned.
THIN_COHORT = 60


@dataclass(frozen=True)
class Estimate:
    """One predicted quantity, never without its sample size."""
    name: str
    value: Optional[float]
    n: int
    unit: str = ""
    spread: Optional[tuple] = None          # (p25, p75) where meaningful

    @property
    def usable(self) -> bool:
        return self.value is not None and self.n >= MIN_COHORT

    def render(self) -> str:
        if self.value is None:
            return f"    {self.name:<16} unmeasurable (n={self.n})"
        iq = ""
        if self.spread:
            iq = f"  [p25 {self.spread[0]:.2f}, p75 {self.spread[1]:.2f}]"
        flag = ""
        if self.n < MIN_COHORT:
            flag = "   <- BELOW THE FLOOR, do not act on this"
        elif self.n < THIN_COHORT:
            flag = "   <- thin"
        return (f"    {self.name:<16} {self.value:>7.2f}{self.unit}  "
                f"n={self.n}{iq}{flag}")


@dataclass
class PathForecast:
    matched: int
    total: int
    conditions: dict
    estimates: list = field(default_factory=list)

    def get(self, name: str) -> Optional[Estimate]:
        return next((e for e in self.estimates if e.name == name), None)

    @property
    def usable(self) -> bool:
        return self.matched >= MIN_COHORT

    def render(self) -> str:
        cond = ", ".join(f"{k}={v}" for k, v in self.conditions.items()) or "none"
        out = [f"  PATH FORECAST ({PATH_VERSION})",
               f"    reference class: {cond}",
               f"    matched {self.matched} of {self.total} resolved trades"]
        out += [e.render() for e in self.estimates]
        if not self.usable:
            out += [f"    NOT USABLE — {self.matched} matching trades is below the "
                    f"{MIN_COHORT} floor.",
                    "    Reported so the gap is visible, not so it can be acted on."]
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {"version": PATH_VERSION, "matched": self.matched,
                "total": self.total, "conditions": self.conditions,
                "usable": self.usable,
                "estimates": {e.name: {"value": e.value, "n": e.n}
                              for e in self.estimates}}


def _q(vals: Sequence[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


def _matches(outcome: dict, conditions: dict) -> bool:
    ctx = outcome.get("context") or {}
    for k, v in conditions.items():
        if k == "direction":
            if outcome.get("direction") != v:
                return False
        elif k == "setup":
            if outcome.get("setup") != v:
                return False
        elif k == "mechanism_name":
            if outcome.get("mechanism_name") != v:
                return False
        elif ctx.get(k) != v:
            return False
    return True


def forecast(history: Sequence[dict], conditions: Optional[dict] = None
             ) -> PathForecast:
    """What trades in this state have historically DONE, step by step.

    `history` is opportunity.resolved_outcomes() output. `conditions` are exact
    matches against Context fields, direction, setup or mechanism_name.
    """
    conditions = dict(conditions or {})
    pool = [o for o in history if _matches(o, conditions)]
    f = PathForecast(len(pool), len(history), conditions)

    mfes = [float(o["mfe_r"]) for o in pool if o.get("mfe_r") is not None]
    maes = [float(o["mae_r"]) for o in pool if o.get("mae_r") is not None]
    reals = [float(o["realised_r"]) for o in pool if o.get("realised_r") is not None]

    n = len(pool)
    # P(ever touches +1R). The precondition for a break-even move meaning
    # anything at all, and the desk's own ledger says it is high while the win
    # rate is not — which is the whole management problem in one number.
    f.estimates.append(Estimate(
        "reach_1r",
        (sum(1 for m in mfes if m >= 1.0) / len(mfes)) if mfes else None,
        len(mfes), unit=""))

    f.estimates.append(Estimate(
        "mfe_median", statistics.median(mfes) if mfes else None, len(mfes),
        unit="R", spread=((_q(mfes, .25), _q(mfes, .75)) if len(mfes) >= 4 else None)))

    f.estimates.append(Estimate(
        "mae_median", statistics.median(maes) if maes else None, len(maes),
        unit="R", spread=((_q(maes, .25), _q(maes, .75)) if len(maes) >= 4 else None)))

    # GIVEBACK — the fraction of the run that was handed back by close. Computed
    # only on trades that actually ran somewhere; a trade whose MFE was 0.05R
    # has no meaningful giveback ratio and averaging it in manufactures one.
    gb = [(m - r) / m for m, r in zip(mfes, reals) if m >= 0.5]
    f.estimates.append(Estimate(
        "giveback", statistics.fmean(gb) if gb else None, len(gb), unit=""))

    f.estimates.append(Estimate(
        "realised_median", statistics.median(reals) if reals else None,
        len(reals), unit="R"))

    # WHEN the peak arrives. Recorded per trade as t_mfe seconds; absent on
    # older rows, and reported as its own n rather than silently dropped.
    tm = [float(o["t_mfe"]) / 60.0 for o in pool
          if o.get("t_mfe") not in (None, 0)]
    f.estimates.append(Estimate(
        "minutes_to_mfe", statistics.median(tm) if tm else None, len(tm),
        unit="m"))

    # Did the pain come first? Needs both timings on the same trade.
    both = [(float(o["t_mae"]), float(o["t_mfe"])) for o in pool
            if o.get("t_mae") not in (None, 0) and o.get("t_mfe") not in (None, 0)]
    f.estimates.append(Estimate(
        "adverse_first",
        (sum(1 for a, m in both if a < m) / len(both)) if both else None,
        len(both), unit=""))
    return f


def management_implication(f: PathForecast) -> str:
    """Turn the shape into the thing a manager would actually do.

    Deliberately conservative about saying anything: an implication drawn from
    an unusable reference class is worse than silence, because it is phrased
    like advice.
    """
    if not f.usable:
        return ("    No implication drawn — the reference class is below the "
                "floor, and advice from it would read like evidence.")
    reach = f.get("reach_1r")
    gb = f.get("giveback")
    tmfe = f.get("minutes_to_mfe")
    bits = []
    if reach and reach.usable and reach.value is not None and reach.value >= 0.6:
        bits.append(f"{reach.value:.0%} of these reach +1R, so a protective move "
                    f"at +1R is reachable rather than theoretical")
    if gb and gb.usable and gb.value is not None and gb.value >= 0.6:
        bits.append(f"but {gb.value:.0%} of the run is handed back by close — "
                    f"the leak is management, not entry")
    if tmfe and tmfe.usable and tmfe.value is not None:
        bits.append(f"the peak typically arrives {tmfe.value:.0f} minutes in, so a "
                    f"policy that only reconsiders on bar close sees it late")
    if not bits:
        return "    Nothing stands out in this reference class."
    return "    " + "; ".join(bits) + "."


def report(history: Sequence[dict], conditions: Optional[dict] = None) -> str:
    f = forecast(history, conditions)
    return (f"PATH PREDICTION (#14)\n\n{f.render()}\n\n"
            f"  WHAT IT IMPLIES\n{management_implication(f)}\n\n"
            "  This is a REFERENCE CLASS, not a fitted model. It conditions on\n"
            "  the discrete state the desk already computes and reports what\n"
            "  matching trades did, with n attached to every number. At this\n"
            "  sample size a fitted model would be fitting noise, and the\n"
            "  admissibility test in prediction_layer.py is the gate that says\n"
            "  when that stops being true.")
