"""What the thinking cost, and what it returned. Item #9.

THE QUESTION NOBODY WAS ASKING

Aurum spends money to think. Every wake is an analyst call, every management
step under the contextual arm is another, and every chart attached to a brief is
input tokens. None of that appeared anywhere in the evaluation. The desk could
measure R per trade and could not measure R per dollar, which means it could not
answer the two questions that actually decide whether an expensive component
stays:

  Does the chart arm return more than the extra tokens cost?
  Does contextual management return more than calling the model 40x per trade?

Both are answerable from data the ledger ALREADY carries — every read is stamped
with input, cache-read and output tokens — and nothing read it.

WHY R PER DOLLAR IS THE RIGHT DENOMINATOR AND ALSO NOT ENOUGH

Inference cost is a real cost and belongs in net value. But it is a FIXED-ish
cost per decision, while R scales with account size. At a $500 account, an
opus-per-wake desk can spend more on thinking than it makes; at $500k the same
spend is a rounding error and optimising it would be optimising a proxy. So this
module reports cost in dollars AND as basis points of the risk unit, and asks
the caller for the account's R value rather than assuming one.

CACHE READS ARE NOT FREE BUT ARE NEARLY SO

The analyst prompt is a stable cached prefix, so most input tokens are cache
reads at a fraction of the price. A budget that prices all input the same
overstates the cost of the very design decision that made it cheap. Priced
separately, from a table that is versioned and stated.

WHAT THIS MUST NOT BECOME

A reason to think less. Refusing to wake is already a registered discretionary
restriction (wake.watcher) whose forgone value is measured; cost is one input to
that review, not a licence to raise the bar. A desk that saves $4 of inference
and misses one 2R trade has lost money, and this module exists partly to make
that arithmetic possible rather than to encourage the saving.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

BUDGET_VERSION = "budget-2026-08-14-a"

# USD per million tokens. Stated here, versioned, and overridable — a hardcoded
# price that silently goes stale turns every cost figure into fiction.
@dataclass(frozen=True)
class Pricing:
    input_per_mtok: float = 15.00
    cache_read_per_mtok: float = 1.50
    output_per_mtok: float = 75.00
    label: str = "opus-class list price, 2026-08"

    def cost(self, usage: dict) -> float:
        cache = float(usage.get("cache_read") or 0)
        # `in` counts ALL input tokens including cache reads on this API, so
        # charging both at full rate would double-bill the cached prefix.
        fresh = max(0.0, float(usage.get("in") or 0) - cache)
        out = float(usage.get("out") or 0)
        return (fresh * self.input_per_mtok
                + cache * self.cache_read_per_mtok
                + out * self.output_per_mtok) / 1e6


@dataclass
class Line:
    """One spending category and what came back."""
    label: str
    calls: int
    usd: float
    tokens_in: int
    tokens_out: int
    cache_hit_rate: Optional[float]
    realised_r: Optional[float] = None
    trades: int = 0

    @property
    def usd_per_call(self) -> float:
        return self.usd / self.calls if self.calls else 0.0

    def render(self, r_value_usd: Optional[float] = None) -> str:
        ch = "n/a" if self.cache_hit_rate is None else f"{self.cache_hit_rate:.0%}"
        base = (f"  {self.label:<22} {self.calls:>6} calls  ${self.usd:>8.2f}  "
                f"${self.usd_per_call:.4f}/call  cache {ch}")
        if self.realised_r is None or r_value_usd is None:
            return base
        gross = self.realised_r * r_value_usd
        net = gross - self.usd
        return (base + f"\n  {'':<22} {self.realised_r:+.2f}R = ${gross:+.2f} "
                       f"gross, ${net:+.2f} AFTER inference")


def _usage_rows(rows: Sequence[dict]) -> list[dict]:
    """Every row that carries a usage stamp, normalised."""
    out = []
    for r in rows:
        dec = r.get("decision") or {}
        u = dec.get("usage")
        if not isinstance(u, dict) or not u:
            continue
        out.append({"usage": u,
                    "kind": r.get("kind"),
                    "vision": dec.get("vision"),
                    "charts": dec.get("charts_sent") or 0,
                    "model": dec.get("model"),
                    "provider": dec.get("provider"),
                    "t0": r.get("t0")})
    return out


@dataclass
class BudgetReport:
    lines: list
    total_usd: float
    total_calls: int
    realised_r: float
    resolved_trades: int
    r_value_usd: Optional[float]
    coverage: float
    #: Bars the analyst never answered on. Held OUT of the coverage denominator
    #: and reported on its own line — see `report()`.
    blind: int = 0

    def render(self) -> str:
        out = [f"INFORMATION BUDGET (#9, {BUDGET_VERSION})", "",
               f"  usage-stamped decisions : {self.total_calls}",
               f"  coverage                : {self.coverage:.0%} of decisions "
               f"carry a usage stamp"]
        if self.coverage < 0.9:
            out.append("  NOTE: incomplete coverage — older rows predate the stamp, "
                       "so totals are a LOWER BOUND")
        if self.blind:
            out.append(f"  BLIND BARS               : {self.blind} — the analyst "
                       f"never answered, so these cost nothing measurable and "
                       f"produced nothing. NOT in the coverage figure above.")
        out += ["", "BY CATEGORY"]
        out += [l.render(self.r_value_usd) for l in self.lines]
        out += ["", f"  TOTAL INFERENCE          ${self.total_usd:.2f} over "
                    f"{self.total_calls} calls"]
        if self.r_value_usd is None:
            out += ["", "  R VALUE NOT SUPPLIED, so net-of-inference value is not",
                    "  computed. Cost per decision is meaningful; cost per R is not",
                    "  until somebody says what an R is worth, and guessing that",
                    "  would make every conclusion here arbitrary."]
        else:
            gross = self.realised_r * self.r_value_usd
            net = gross - self.total_usd
            out += ["",
                    f"  realised {self.realised_r:+.2f}R over {self.resolved_trades} "
                    f"trades at ${self.r_value_usd:.0f}/R = ${gross:+.2f}",
                    f"  less ${self.total_usd:.2f} inference = ${net:+.2f} NET",
                    "",
                    "  Inference is a real cost and belongs in net value. It is also",
                    "  nearly fixed per decision while R scales with the account, so",
                    "  a spend that dominates at one size is a rounding error at",
                    "  another. Read this as arithmetic, not as an argument to think",
                    "  less: one missed 2R trade costs more than a month of tokens."]
        return "\n".join(out)


def report(rows: Sequence[dict], *, pricing: Optional[Pricing] = None,
           r_value_usd: Optional[float] = None) -> BudgetReport:
    """What was spent, on what, and what came back.

    `r_value_usd` is what one R is worth in this account. It is REQUIRED for any
    net figure and is deliberately not defaulted: an assumed R value silently
    decides whether every component in the desk looks profitable.
    """
    pricing = pricing or Pricing()
    stamped = _usage_rows(rows)
    # BLIND rows are held OUT of the coverage denominator. Coverage answers
    # "what fraction of decisions carry a cost stamp", and a bar the analyst
    # never answered on is not a decision — nothing decided anything, and there
    # is no completed call to stamp. Counting them would deflate coverage below
    # 0.9 during any outage and trip the NOTE above, which explains the shortfall
    # as "older rows predate the stamp". That explanation would be FALSE: the
    # cause would be an analyst that was down, and the report would be asserting
    # a diagnosis nobody measured. Counted separately instead.
    blind = [r for r in rows if str(r.get("kind", "")) == "BLIND"]
    decisions = [r for r in rows if r.get("kind") and str(r.get("kind")) != "BLIND"]
    coverage = len(stamped) / len(decisions) if decisions else 0.0

    groups: dict[str, list[dict]] = defaultdict(list)
    for s in stamped:
        if s["charts"]:
            key = f"analyst (charts x{s['charts']})"
        elif s["vision"]:
            key = "analyst (numeric only)"
        else:
            key = "analyst"
        groups[key].append(s)

    lines: list[Line] = []
    total = 0.0
    for key in sorted(groups):
        g = groups[key]
        usd = sum(pricing.cost(s["usage"]) for s in g)
        tin = sum(int(s["usage"].get("in") or 0) for s in g)
        tout = sum(int(s["usage"].get("out") or 0) for s in g)
        cread = sum(int(s["usage"].get("cache_read") or 0) for s in g)
        lines.append(Line(key, len(g), usd, tin, tout,
                          (cread / tin) if tin else None))
        total += usd

    from .opportunity import resolved_outcomes
    res = resolved_outcomes(list(rows))
    realised = sum(o["realised_r"] for o in res)
    return BudgetReport(lines, total, len(stamped), realised, len(res),
                        r_value_usd, coverage, len(blind))


# --------------------------------------------------------------------------
# The comparison that actually decides an arm's fate
# --------------------------------------------------------------------------

@dataclass
class ArmCost:
    arm: str
    calls: int
    usd: float
    usd_per_decision: float
    realised_r: Optional[float]
    trades: int

    def render(self) -> str:
        r = "no resolved trades" if self.realised_r is None \
            else f"{self.realised_r:+.2f}R over {self.trades}"
        return (f"  {self.arm:<24} {self.calls:>5} calls  ${self.usd:>7.2f}  "
                f"${self.usd_per_decision:.4f}/decision  {r}")


def compare_arms(rows: Sequence[dict], *, pricing: Optional[Pricing] = None,
                 r_value_usd: Optional[float] = None) -> str:
    """Cost and return per ARM, so an expensive arm has to justify itself.

    This is the number the vision factorial was missing. NUMERIC_PLUS_CHARTS
    sends three images per read; if it wins by 0.05R per trade and costs four
    times as much per decision, whether it is worth running depends entirely on
    the R value — and that comparison was not previously computable at all.
    """
    pricing = pricing or Pricing()
    by_arm: dict[str, dict] = defaultdict(lambda: {"calls": 0, "usd": 0.0,
                                                   "r": 0.0, "trades": 0})
    for r in rows:
        dec = r.get("decision") or {}
        arm = dec.get("vision") or r.get("vision")
        if not arm:
            continue
        u = dec.get("usage")
        if isinstance(u, dict) and u:
            by_arm[arm]["calls"] += 1
            by_arm[arm]["usd"] += pricing.cost(u)

    from .opportunity import resolved_outcomes
    for o in resolved_outcomes(list(rows)):
        arm = o.get("vision")
        if arm and arm in by_arm:
            by_arm[arm]["r"] += o["realised_r"]
            by_arm[arm]["trades"] += 1

    out = [f"COST BY ARM ({BUDGET_VERSION}, {pricing.label})", ""]
    arms = []
    for name in sorted(by_arm):
        d = by_arm[name]
        arms.append(ArmCost(name, d["calls"], d["usd"],
                            d["usd"] / d["calls"] if d["calls"] else 0.0,
                            d["r"] if d["trades"] else None, d["trades"]))
    out += [a.render() for a in arms]
    if len(arms) < 2:
        out += ["", "  Only one arm has run. A cost comparison needs at least two,",
                "  and comparing an arm against nothing is how a component keeps",
                "  its place by default."]
        return "\n".join(out)
    if r_value_usd is None:
        out += ["", "  Supply r_value_usd to see which arm wins NET of what it cost",
                "  to run. Without it these are spend figures, not a verdict."]
        return "\n".join(out)
    out += ["", "NET OF INFERENCE"]
    for a in arms:
        if a.realised_r is None:
            out.append(f"  {a.arm:<24} no resolved trades — cannot be judged")
            continue
        net = a.realised_r * r_value_usd - a.usd
        out.append(f"  {a.arm:<24} ${net:+9.2f} "
                   f"({a.realised_r:+.2f}R less ${a.usd:.2f})")
    out += ["", "  A difference here is NOT yet a result: these arms have not been",
            "  run on identical states in comparable numbers. Cost is the half",
            "  that was missing, not the whole comparison."]
    return "\n".join(out)


def load(paths: Iterable) -> list[dict]:
    import json
    from pathlib import Path
    rows: list[dict] = []
    for p in paths:
        p = Path(p)
        if p.exists():
            rows += [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
    return rows
