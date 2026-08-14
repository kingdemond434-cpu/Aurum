"""Missed-money accounting that only counts money that was actually gettable.

A refusal's forward path says what price did next. It does NOT say whether the
desk could have participated. Those are different questions, and conflating them
inflates every "this gate cost you R" number in the same direction — upward —
which is the direction that argues for removing gates.

THREE WAYS A REFUSAL'S FORGONE VALUE CAN BE FICTIONAL

  1. NO REACHABLE ENTRY. The level never traded after the decision, so the
     trade could not have been opened at all. Its excursion belongs to somebody
     else's fill.

  2. NO PLACEABLE STOP. The structural stop sat inside the broker's minimum
     distance or through the market. A trade you cannot protect is not a trade
     you were denied.

  3. COST EXCEEDS THE MOVE. On a small stop the spread can be a large fraction
     of R. A move that looks like +0.4R gross can be flat or negative net, and
     counting it as forgone opportunity is counting the broker's income as
     yours.

This module re-prices the refusal ledger with feasibility enforced, and reports
the difference against the naive number. The difference is the amount by which
under-trading was being overstated — which matters, because that overstatement
argues for loosening exactly the gates that were holding.

It reads the ledger and writes nothing. No module in golddesk imports it.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from golddesk.costs import CostModel, round_trip_cost


@dataclass
class Feasibility:
    """Could this refused trade have been taken, and was it worth taking?"""
    decision_id: str
    reason: str
    direction: str
    reachable: bool
    stop_placeable: bool
    cost_r: Optional[float]
    gross_r: Optional[float]
    net_r: Optional[float]
    verdict: str

    @property
    def counted(self) -> bool:
        return self.reachable and self.stop_placeable and self.net_r is not None


@dataclass
class MissedMoney:
    restriction: str
    n_refusals: int
    n_feasible: int
    naive_forgone_r: float          # what the old accounting claimed
    feasible_forgone_r: float       # what was actually gettable
    avoided_loss_r: float
    net_contribution_r: float
    overstatement_r: float
    verdict: str

    def render(self) -> str:
        pct = (self.n_feasible / self.n_refusals) if self.n_refusals else 0.0
        return (f"  {self.restriction:<26} refusals={self.n_refusals:<5} "
                f"feasible={self.n_feasible:<5} ({pct:.0%})\n"
                f"  {'':<26} naive forgone {self.naive_forgone_r:>+8.1f}R  ->  "
                f"gettable {self.feasible_forgone_r:>+8.1f}R  "
                f"(overstated by {self.overstatement_r:>+7.1f}R)\n"
                f"  {'':<26} net contribution {self.net_contribution_r:>+7.1f}R — "
                f"{self.verdict}")


def assess(row: dict, *, min_stop_distance: float = 0.0,
           cost_model: CostModel = CostModel(),
           target_r: float = 2.0, stop_r: float = -1.0) -> Feasibility:
    """Decide whether one refused decision was a real opportunity.

    Uses only what the ledger already stores: the forward excursion, its timing,
    the risk unit the compiler would have used, and the spread at the time.
    """
    out = row.get("outcome") or {}
    dec = row.get("decision") or {}
    ctx = row.get("context") or {}
    direction = str(dec.get("declined") or dec.get("direction") or "LONG")
    mfe, mae = out.get("mfe_r"), out.get("mae_r")
    t_mfe, t_mae = out.get("time_to_mfe_s"), out.get("time_to_mae_s")
    rid = str(row.get("decision_id") or row.get("t0") or "?")
    reason = str(row.get("reason", ""))

    if mfe is None or mae is None:
        return Feasibility(rid, reason, direction, False, False, None, None, None,
                           "no forward path — cannot be assessed")

    # 1. reachable. A MARKET-referenced refusal is reachable by definition; a
    #    level-referenced one is reachable only if price returned to it. The
    #    ledger records excursion from the reference price, so any nonzero
    #    adverse OR favourable travel means the market traded around it.
    reachable = not (mfe == 0.0 and mae == 0.0)

    # 2. stop placeable. The risk unit is the compiler's; a stop closer to the
    #    market than the venue minimum could not have been placed.
    risk_price = dec.get("risk") or ctx.get("atr")
    stop_placeable = True
    if risk_price and min_stop_distance:
        stop_placeable = float(risk_price) >= float(min_stop_distance)

    # 3. cost. Spread over the risk unit, charged once for the round trip.
    spread = ctx.get("spread")
    cost_r = None
    if spread is not None and risk_price:
        try:
            cost_r = round_trip_cost(float(spread), cost_model) / float(risk_price)
        except (TypeError, ZeroDivisionError):
            cost_r = None

    # first-touch, same rule a taken trade gets
    hit_stop, hit_tp = mae <= stop_r, mfe >= target_r
    if hit_stop and hit_tp:
        gross = stop_r if (t_mae is None or t_mfe is None or t_mae <= t_mfe) else target_r
    elif hit_stop:
        gross = stop_r
    elif hit_tp:
        gross = target_r
    else:
        gross = 0.0
    net = gross - (cost_r or 0.0)

    if not reachable:
        v = "NOT REACHABLE — price never traded around the reference"
    elif not stop_placeable:
        v = "NO PLACEABLE STOP — inside the venue minimum"
    elif net <= 0 < gross:
        v = f"COST KILLS IT — gross {gross:+.2f}R becomes {net:+.2f}R net"
    elif net > 0:
        v = f"GENUINELY FORGONE — {net:+.2f}R was available"
    else:
        v = f"correctly refused — {net:+.2f}R"
    return Feasibility(rid, reason, direction, reachable, stop_placeable,
                       cost_r, gross, net, v)


def price_restrictions(rows: Sequence[dict], reason_map: Optional[dict] = None,
                       **kw) -> list[MissedMoney]:
    """Re-price every restriction with feasibility enforced."""
    from golddesk.constitution import BY_ID, DEFAULT_REASON_MAP
    mapping = reason_map or DEFAULT_REASON_MAP
    naive: dict[str, list] = {}
    feas: dict[str, list] = {}
    counts: dict[str, int] = {}
    for r in rows:
        if not str(r.get("kind", "")).startswith("REFUSAL"):
            continue
        reason = str(r.get("reason", ""))
        rid = next((v for k, v in mapping.items() if k in reason), None)
        if rid is None:
            continue
        counts[rid] = counts.get(rid, 0) + 1
        f = assess(r, **kw)
        # the naive number: gross first-touch, no feasibility, no cost
        if f.gross_r is not None:
            naive.setdefault(rid, []).append(f.gross_r)
        if f.counted:
            feas.setdefault(rid, []).append(f.net_r)

    out: list[MissedMoney] = []
    for rid in sorted(counts):
        nv = naive.get(rid, [])
        fv = feas.get(rid, [])
        naive_forgone = sum(v for v in nv if v > 0)
        got = sum(v for v in fv if v > 0)
        avoided = -sum(v for v in fv if v < 0)
        net = avoided - got
        rest = BY_ID.get(rid)
        if rest and rest.exempt:
            verdict = "EXEMPT (hard risk)"
        elif len(fv) < 30:
            verdict = f"UNDETERMINED (n={len(fv)} feasible)"
        elif net > 0:
            verdict = "EARNS ITS KEEP"
        else:
            verdict = "COSTS MORE THAN IT SAVES"
        out.append(MissedMoney(rid, counts[rid], len(fv), naive_forgone, got,
                               avoided, net, naive_forgone - got, verdict))
    return out


def coverage(rows: Sequence[dict], min_stop_distance: float = 0.0) -> list[str]:
    """Which feasibility tests could actually bite on THIS ledger?

    A test that cannot fail reports 100% feasible and 0% overstatement, which
    reads exactly like a clean bill of health. It is not one. Every gate below
    states the condition under which it is informative, so a vacuous pass is
    labelled vacuous instead of being quietly banked as evidence.
    """
    notes: list[str] = []
    refusals = [r for r in rows if str(r.get("kind", "")).startswith("REFUSAL")]
    if not refusals:
        return ["no refusals to assess"]
    ctx_has_spread = sum(1 for r in refusals if (r.get("context") or {}).get("spread") is not None)
    if not ctx_has_spread:
        notes.append("COST TEST INERT — no `spread` in any refusal context, so cost "
                     "is never subtracted and every gross move counts as gettable")
    if not min_stop_distance:
        notes.append("STOP-PLACEABILITY TEST INERT — no broker minimum supplied; "
                     "pass min_stop_distance from BrokerLimits to exercise it")
    zero = sum(1 for r in refusals
               if ((r.get("outcome") or {}).get("mfe_r"), (r.get("outcome") or {}).get("mae_r")) == (0.0, 0.0))
    if zero == 0:
        notes.append("REACHABILITY TEST INERT — no refusal has a flat forward path. "
                     "Over a long forward window price trades through everything, so "
                     "this only discriminates on intraday data with a bounded hold")
    return notes or ["all three feasibility tests are live on this ledger"]


def report(rows: Sequence[dict], **kw) -> str:
    items = price_restrictions(rows, **kw)
    if not items:
        return "no mapped refusals in this ledger"
    tot_naive = sum(i.naive_forgone_r for i in items)
    tot_real = sum(i.feasible_forgone_r for i in items)
    lines = ["MISSED-MONEY LEDGER (feasibility enforced)", ""]
    for i in items:
        lines.append(i.render())
    lines += ["",
              f"  TOTAL claimed forgone : {tot_naive:>+9.1f}R",
              f"  TOTAL actually gettable: {tot_real:>+9.1f}R",
              f"  OVERSTATEMENT         : {tot_naive - tot_real:>+9.1f}R "
              f"({(1 - tot_real / tot_naive) if tot_naive else 0:.0%} of the claim)",
              "",
              "  Overstated forgone value argues for loosening gates that may be",
              "  holding correctly. It is the one accounting error that pushes a",
              "  desk toward over-trading while looking like rigour.",
              "",
              "TEST COVERAGE — is the number above worth anything?"]
    cov = coverage(rows, kw.get("min_stop_distance", 0.0))
    lines += [f"  {c}" for c in cov]
    if any("INERT" in c for c in cov):
        lines += ["",
                  "  An overstatement of 0% here means the tests did not bite, NOT that",
                  "  the forgone numbers are clean. Re-run on M15/M1 with a bounded hold,",
                  "  spread in the context and BrokerLimits supplied before believing it."]
    return "\n".join(lines)


def load(paths: Iterable[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if Path(p).exists():
            rows += [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]
    return rows


if __name__ == "__main__":
    import glob
    import sys
    files = sys.argv[1:] or sorted(glob.glob("backtest_out/ledger-A-test*.jsonl"))
    rows = load(Path(f) for f in files)
    print(f"read {len(rows)} ledger rows from {len(files)} file(s)\n")
    print(report(rows))
