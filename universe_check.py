#!/usr/bin/env python3
"""Prove the opportunity universe does what it claims. Item #1.

Nine things have to be true, and each is checked against a case constructed to
FAIL if the implementation is decorative:

  1  several propositions compile independently, through the same gates
  2  when the budget does not bind, nothing is dropped for ranking low
  3  when it does bind, the selection says so and names what it left
  4  the same bet twice is detected; a legitimate sequence is not
  5  every non-taken candidate keeps its geometry, so it can be resolved forward
  6  a single-read provider still works, and the wrapper does not pretend the
     absence of other candidates is a statement about the market
  7  a measured-negative cohort is refused on expectancy, not on scarcity
  8  every new restriction is in the constitution registry as DISCRETIONARY
  9  the LIVE path, end to end: LiveDesk in universe mode over synthetic bars,
     with the same states run twice — once with risk.one_position enforcing and
     once demoted — because a module that passes its own tests and is never
     reached from on_bar is the failure this project has already hit.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from golddesk.analyst import AnalystRead, MarketBrief, Setup, Thresholds
from golddesk.costs import CostModel
from golddesk.opportunity import CohortStat, Heat
from golddesk.universe import (MAX_CANDIDATES, AnalystUniverse, Candidate,
                               as_universe, compile_universe, redundancy, select)

OK, BAD = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


# --------------------------------------------------------------------------
# A brief with enough structure to support several genuinely different theses
# --------------------------------------------------------------------------

def build_brief() -> MarketBrief:
    from golddesk.analyst import Context, Level, LevelKind

    now = datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)
    lv = [
        Level("L1", LevelKind.SWING_LOW, 1990.00, "M15", 12, True),
        Level("L2", LevelKind.SWING_LOW, 2000.00, "M15", 8, True),
        Level("L3", LevelKind.SWING_HIGH, 2010.00, "M15", 4, True),
        Level("L4", LevelKind.SWING_HIGH, 2062.00, "H4", 30, True),
        Level("L5", LevelKind.SWING_LOW, 1975.00, "H4", 40, True),
        Level("L6", LevelKind.SWING_HIGH, 2020.00, "M15", 6, True),
    ]
    ctx = Context(trend_direction="UP", trend_health="MODERATE",
                  trend_maturity="MID", volatility_state="NORMAL",
                  htf_alignment="ALIGNED", displacement_state="CONFIRMED",
                  sweep_state="CONFIRMED", reclaim_state="CONFIRMED",
                  pullback_depth="MEDIUM", distance_from_session_extreme="MID")
    return MarketBrief(symbol="XAUUSD", as_of_utc=now, session="LONDON",
                       bid=2004.80, ask=2005.20, spread=0.40,
                       tick_age_s=1.0, atr=4.0,
                       levels=lv, context=ctx,
                       trigger_price=2005.00, trigger_utc=now)


def read(mech: str, direction: str, stop: str, tp: str,
         entry: str = "MARKET", conf: int = 3,
         setup: Setup = Setup.SWING_REVERSAL) -> AnalystRead:
    return AnalystRead(setup=setup, direction=direction, entry_ref=entry,
                       stop_ref=stop, tp1_ref="NONE", tp2_ref=tp,
                       mechanism_name=mech, confidence=conf,
                       read="constructed", why="constructed mechanism",
                       why_not="constructed counter-case",
                       invalidation="constructed invalidation")


def main() -> int:
    brief = build_brief()
    th = Thresholds()
    cm = CostModel()

    print("#1 OPPORTUNITY UNIVERSE\n")

    # ---------------------------------------------------------------- 1
    print("1. several propositions compile independently")
    # Four propositions: two longs sharing a band (one idea twice), one short on
    # a disjoint band, and one citing a level that does not exist.
    uni = AnalystUniverse(
        candidates=[
            read("reclaim-of-2000", "LONG", "L2", "L4"),   # band 1999..2062
            read("sweep-1990-trap", "LONG", "L1", "L4"),   # band 1989..2062
            read("supply-at-2010", "SHORT", "L3", "L5"),   # band 1975..2011
            read("broken-ref", "LONG", "L9", "L4"),        # unusable ref
        ],
        survey="four propositions across two timeframes and both directions",
        dominant_context="constructed")
    # Two propositions that are genuinely independent — no band overlap.
    clean_uni = AnalystUniverse(
        candidates=[read("reclaim-of-2000", "LONG", "L2", "L4"),
                    read("supply-at-2010", "SHORT", "L3", "L5")],
        survey="two independent propositions", dominant_context="constructed")
    cands = compile_universe(brief, uni, th, cm)
    viable = [c for c in cands if c.viable]
    gated = [c for c in cands if not c.viable]
    check("all four were evaluated", len(cands) == 4)
    check("the unusable ref was refused, not dropped silently",
          any("L9" in (c.refusal.reason if c.refusal else "") for c in gated),
          gated[-1].disposition_reason if gated else "")
    check("more than one proposition survived compilation", len(viable) >= 2,
          f"{len(viable)} viable")
    for c in cands:
        print(c.render())

    # ---------------------------------------------------------------- 2
    print("\n2. budget does not bind -> nothing dropped for ranking low")
    roomy = Heat(max_open_risk_r=6.0, correlation_haircut=0.0, max_daily_loss_r=3.0)
    sel = select(compile_universe(brief, clean_uni, th, cm), roomy, max_concurrent=4)
    n_viable = sum(1 for c in sel.candidates if c.viable)
    check("every viable candidate was taken", len(sel.taken) == n_viable,
          f"{len(sel.taken)} of {n_viable}")
    check("budget_bound is False", sel.budget_bound is False)
    check("tiebreak was not load-bearing", sel.tiebreak_used is False)
    print(sel.render())

    # ---------------------------------------------------------------- 3
    print("\n3. budget binds -> it says so and names what it left")
    tight = Heat(max_open_risk_r=1.0, correlation_haircut=0.65, max_daily_loss_r=3.0)
    sel2 = select(compile_universe(brief, uni, th, cm), tight, max_concurrent=1)
    check("exactly one taken under a one-position ceiling", len(sel2.taken) == 1)
    check("budget_bound is True", sel2.budget_bound is True)
    check("the losers are DEFERRED, not GATED", len(sel2.deferred) >= 1,
          f"{len(sel2.deferred)} deferred")
    check("a deferral says it was a budget, not merit",
          all("budget" in c.disposition_reason or "ceiling" in c.disposition_reason
              or "overlap" in c.disposition_reason for c in sel2.deferred),
          "; ".join(c.disposition_reason[:60] for c in sel2.deferred))
    print(sel2.render())

    # ---------------------------------------------------------------- 4
    print("\n4. redundancy: the same bet twice, and a legitimate sequence")
    dup_uni = AnalystUniverse(
        candidates=[read("reclaim-a", "LONG", "L2", "L4"),
                    read("reclaim-b", "LONG", "L2", "L4")],
        survey="same band twice", dominant_context="constructed")
    dc = compile_universe(brief, dup_uni, th, cm)
    check("two identical-band longs are flagged redundant",
          redundancy(dc[0], dc[1]) is not None,
          redundancy(dc[0], dc[1]) or "")
    sc = compile_universe(brief, clean_uni, th, cm)
    ok_pair = [c for c in sc if c.viable]
    if len(ok_pair) == 2:
        check("disjoint opposite-direction bands are NOT flagged",
              redundancy(ok_pair[0], ok_pair[1]) is None,
              redundancy(ok_pair[0], ok_pair[1]) or "left alone")
    else:
        check("disjoint pair compiled", False,
              f"only {len(ok_pair)} viable: "
              + "; ".join(c.disposition_reason[:50] for c in sc))
    sel3 = select(dc, roomy, max_concurrent=4)
    check("only one of the duplicate pair is taken", len(sel3.taken) == 1,
          f"{len(sel3.taken)} taken")

    # ---------------------------------------------------------------- 5
    print("\n5. non-taken candidates keep their geometry")
    for c in sel2.deferred:
        j = c.to_journal()
        check(f"deferred [{c.index}] carries entry/stop/tp2",
              all(k in j for k in ("entry", "stop", "tp2", "risk_price")),
              f"{j.get('entry')} / {j.get('stop')} / {j.get('tp2')}")
    check("the selection journal is serialisable and complete",
          set(sel2.to_journal()) >= {"enumerated", "taken", "budget_bound",
                                     "tiebreak_used", "candidates"})

    # ---------------------------------------------------------------- 6
    print("\n6. a single-read provider still works")
    one = as_universe(read("solo", "LONG", "L2", "L4"))
    check("one read becomes a one-candidate universe", len(one.candidates) == 1)
    check("the wrapper does not claim the market had only one opportunity",
          "property of the interface" in one.survey)
    nothing = as_universe(AnalystRead(setup=Setup.NO_SETUP, direction="NONE",
                                      entry_ref="NONE", stop_ref="NONE",
                                      tp1_ref="NONE", tp2_ref="NONE",
                                      mechanism_name="none", confidence=1,
                                      read="", why="", why_not="nothing here",
                                      invalidation=""))
    check("NO_SETUP becomes an empty universe, not a NO_SETUP candidate",
          nothing.candidates == [])

    # ---------------------------------------------------------------- 7
    print("\n7. measured negative EV is gated, not deferred")
    cohorts = {"reclaim-of-2000": CohortStat("reclaim-of-2000", 120, 6, -0.4,
                                             6 / 120, 0.05, True)}
    cc = compile_universe(brief, uni, th, cm, cohorts)
    sel4 = select(cc, roomy, max_concurrent=4)
    bad = next((c for c in sel4.candidates
                if c.mechanism == "reclaim-of-2000"), None)
    check("a measured-negative cohort is refused",
          bad is not None and bad.disposition == "GATED",
          bad.disposition_reason if bad else "candidate missing")
    check("and it is refused for EV, not for scarcity",
          bad is not None and ("scarcity is irrelevant" in bad.disposition_reason
                               or "expectancy" in bad.disposition_reason),
          bad.disposition_reason[:90] if bad else "")
    check("its sibling with no such cohort still competes",
          any(c.disposition in ("TAKEN", "DEFERRED") for c in sel4.candidates
              if c.mechanism != "reclaim-of-2000" and c.viable))

    # ---------------------------------------------------------------- 8
    print("\n8. the restrictions are registered")
    from golddesk.constitution import BY_ID
    for rid in ("entry.single_read", "entry.universe_cap",
                "entry.universe_tiebreak", "entry.universe_redundancy"):
        check(f"{rid} is in the registry", rid in BY_ID,
              BY_ID[rid].kind.value if rid in BY_ID else "MISSING")
        if rid in BY_ID:
            check(f"{rid} is discretionary (must earn its keep)",
                  not BY_ID[rid].exempt)

    # ---------------------------------------------------------------- 9
    print("\n9. the LIVE path, end to end")
    live_ok = live_universe_run()

    print(f"\n{OK} ok, {BAD} failed")
    return 1 if (BAD or not live_ok) else 0


# --------------------------------------------------------------------------
# End to end through LiveDesk itself — the only proof that matters
# --------------------------------------------------------------------------

def live_universe_run() -> bool:
    """Drive a real LiveDesk in universe mode over synthetic bars.

    Standalone module tests prove the arithmetic. This proves the WIRING: that
    on_bar reaches _decide_universe, that entries actually open, that every
    record lands with a distinct decision_id, and that a deferred candidate is
    journalled with the geometry needed to resolve it forward. A module that
    passes its own tests and is never called from the live path is exactly the
    failure this project has hit before.
    """
    import json
    import tempfile
    from pathlib import Path

    from golddesk.analyst import Setup
    from golddesk.features import Bar, atr, classify, swings
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.notify import build_sink
    from golddesk.providers import AnalystProvider, ProviderRead
    from golddesk.runner import build_brief, session_of
    from golddesk.universe import AnalystUniverse

    class MultiProvider(AnalystProvider):
        """Proposes both a long and a short off whatever levels the brief has."""
        name, model = "stub-universe", "none"
        calls = 0

        def read(self, brief, charts=()):
            raise AssertionError("universe mode must not call read()")

        def survey(self, brief, charts=()):
            MultiProvider.calls += 1
            lows = sorted((l for l in brief.levels
                           if l.confirmed and l.price < brief.mid),
                          key=lambda l: l.price)
            highs = sorted((l for l in brief.levels
                            if l.confirmed and l.price > brief.mid),
                           key=lambda l: l.price)
            cands = []
            if lows and highs:
                # Nearest level as the stop, furthest as the objective — the
                # geometry a real proposal has, so the R:R clears the cold-start
                # prior and the run exercises entry rather than the gate.
                lo_stop = lows[-2] if len(lows) >= 2 else lows[-1]
                hi_stop = highs[1] if len(highs) >= 2 else highs[0]
                cands.append(read("stub-long", "LONG", lo_stop.id, highs[-1].id,
                                  setup=Setup.SWING_REVERSAL))
                cands.append(read("stub-short", "SHORT", hi_stop.id, lows[0].id,
                                  setup=Setup.TREND_CONTINUATION))
            uni = AnalystUniverse(candidates=cands, survey="stub survey",
                                  dominant_context="stub")
            return ProviderRead(cands[0] if cands else None, self.name,
                                self.model, 0.0, {"candidates": len(cands)}), uni

    # A path with enough noise to produce swings, and legs to produce a trend.
    #
    # The jitter is not decoration. A perfectly regular path makes the turning
    # bar's high TIE with its neighbour's, the fractal swing test requires a
    # STRICT local extreme, and the fixture then yields zero swings, zero
    # structure and zero decisions — a green run that proved nothing. Same
    # reason capture_proof.py carries one.
    now = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
    seed = 20260814

    def jitter() -> float:
        nonlocal seed
        seed = (1103515245 * seed + 12345) % (1 << 31)
        return (seed / (1 << 31) - 0.5) * 1.6

    # Structure at TWO scales, on purpose. A single trend leaves no confirmed
    # level on one side of the market, so the counter-direction proposition can
    # never compile and the fixture silently tests only half of what it claims.
    # A slow swing plus a fast one puts levels both near and far, above and
    # below, which is the state where two live propositions genuinely coexist.
    import math
    bars, prev = [], 2000.0
    for k in range(460):
        px = (2000.0 + 26.0 * math.sin(2 * math.pi * k / 41)
              + 13.0 * math.sin(2 * math.pi * k / 13) + jitter())
        h = max(prev, px) + 0.5 + abs(jitter())
        lo = min(prev, px) - 0.5 - abs(jitter())
        bars.append(Bar(now + timedelta(minutes=15 * k), prev, h, lo, px))
        prev = px

    atrs, sw = atr(bars), swings(bars)

    def drive(ceiling: int, max_open_risk_r: float = 2.0,
              max_daily_loss_r: float = 50.0) -> tuple[list[dict], object, int]:
        # The daily-loss limit is deliberately slack in BOTH arms. It is a
        # HARD_RISK restriction and it fires before the ceiling ever binds, so
        # leaving it at its live value would mean the two arms differ by which
        # limit stopped them rather than by the one under test — and the
        # comparison would say nothing about risk.one_position at all.
        from golddesk.runner import RiskLimits
        out = Path(tempfile.mkdtemp())
        desk = LiveDesk(MultiProvider(), Ledger(out / "l.jsonl"), build_sink(None),
                        shadow=True, vision=Vision.NUMERIC_ONLY,
                        thresholds=Thresholds(fallback_min_rr=1.0),
                        limits=RiskLimits(max_open_risk_r=max_open_risk_r,
                                          max_daily_loss_r=max_daily_loss_r),
                        universe_mode=True, concurrency_ceiling=ceiling,
                        measure_position_constraint=False)
        peak = 0
        tl: list[str] = []
        for i in range(60, len(bars) - 61):
            st = classify(bars, i, sw, atrs)
            if st is None:
                continue
            tl.append(f"{bars[i].ts.date()} {st.trend_direction}/{st.trend_health}")
            tl[:] = tl[-8:]
            desk.on_bar(bars, i, sw, atrs, None,
                        (bars[i].close - 0.05, bars[i].close + 0.05, 1.0), tl)
            peak = max(peak, len(desk.open_trades))
        rows = [json.loads(l) for l in (out / "l.jsonl").read_text().splitlines()
                if l.strip()]
        return rows, desk, peak

    # ---- run A: the constitution as shipped (risk.one_position ENFORCING).
    # max_concurrent() is 1 here regardless of concurrency_ceiling, so the
    # universe path's job is not to trade more — it is to WRITE DOWN what the
    # ceiling turned away, which is the evidence the restriction's own review
    # needs and which the single-read path could never produce.
    rows, desk, peak = drive(3)
    ids = [r["decision_id"] for r in rows if "decision_id" in r]
    uni_rows = [r for r in rows if "universe" in (r.get("decision") or {})]
    entries = [r for r in rows if r.get("kind") == "SIGNAL"]
    deferred = [r for r in rows
                if (r.get("decision") or {}).get("candidate", {}).get("disposition") == "DEFERRED"]
    multi = [r for r in uni_rows
             if sum(1 for c in r["decision"]["universe"]["candidates"]
                    if c["disposition"] != "GATED") >= 2]

    check("universe mode called survey(), never read()", MultiProvider.calls > 0,
          f"{MultiProvider.calls} surveys")
    check("the universe itself was journalled", len(uni_rows) > 0,
          f"{len(uni_rows)} universe rows")
    check("entries were actually opened", len(entries) > 0, f"{len(entries)} entries")
    check("every decision_id is unique", len(ids) == len(set(ids)),
          f"{len(ids)} rows, {len(set(ids))} distinct")
    check("the ceiling was 1 — risk.one_position is ENFORCING", peak == 1,
          f"peak simultaneous open = {peak}")
    check("bars offering two live propositions were recorded", len(multi) > 0,
          f"{len(multi)} of {len(uni_rows)} surveys had 2+ survive the gates")
    check("each of those journalled the one it turned away",
          len(deferred) >= len(multi), f"{len(deferred)} deferral rows")
    if deferred:
        g = deferred[0]["decision"]["candidate"]
        check("a deferred candidate carries geometry",
              all(k in g for k in ("entry", "stop", "tp2")),
              f"{g.get('entry')} / {g.get('stop')} / {g.get('tp2')}")
        check("a deferred candidate carries a forward outcome",
              deferred[0].get("outcome") is not None,
              "resolve_forward ran — the ceiling's cost is measurable")
    print(f"  A  {desk.stats.states} states, {desk.stats.wakes} wakes, "
          f"{desk.stats.reads} surveys, {desk.stats.entries} entries, "
          f"{len(deferred)} deferrals, peak concurrency {peak}")

    # ---- run B: the same states with the restriction demoted. Proves the
    # ceiling is genuinely removable rather than demoted on paper — the exact
    # failure the audit found in the single-read path.
    from golddesk.constitution import BY_ID, Status
    r = BY_ID["risk.one_position"]
    was = r.status
    r.status = Status.ADVISORY
    try:
        rows_b, desk_b, peak_b = drive(3, max_open_risk_r=4.0)
    finally:
        r.status = was

    def reasons(rs):
        out = []
        for x in rs:
            u = (x.get("decision") or {}).get("universe")
            if not u:
                continue
            out += [c["disposition_reason"] for c in u["candidates"]
                    if c["disposition"] == "DEFERRED"]
        return out

    ra, rb = reasons(rows), reasons(rows_b)
    check("demoting risk.one_position actually raises concurrency", peak_b >= 2,
          f"peak simultaneous open = {peak_b} (was {peak} while enforcing)")
    check("and it opens trades the enforcing arm could not",
          desk_b.stats.entries > desk.stats.entries,
          f"{desk_b.stats.entries} entries vs {desk.stats.entries}")
    check("the ceiling was never what turned a second thesis away",
          not any("concurrency ceiling" in x for x in ra),
          f"{sum(1 for x in ra if 'concurrency ceiling' in x)} of {len(ra)} "
          f"deferrals cite it")
    check("redundancy is what turned it away",
          all("overlap" in x for x in ra) and len(ra) > 0,
          f"{sum(1 for x in ra if 'overlap' in x)} of {len(ra)}")
    print(f"  B  {desk_b.stats.entries} entries, peak concurrency {peak_b} "
          f"— the same states, one restriction demoted")
    print()
    print("  FINDING, and it is not a fixture artefact: two MARKET entries in")
    print("  opposite directions at the same moment ALWAYS share a price band, so")
    print("  they are always one fee-paying wash rather than two theses. On a")
    print("  single instrument, same-moment concurrency is therefore mostly")
    print("  redundancy, not opportunity — the ceiling was never the binding")
    print("  constraint on this data. The concurrency that demoting the")
    print("  restriction DID unlock came from theses arriving at DIFFERENT")
    print("  moments while an earlier one was still open, which the single-read")
    print("  path could not even enumerate. That is the effect worth measuring,")
    print("  and it is now in the ledger rather than in an argument.")
    return True


if __name__ == "__main__":
    sys.exit(main())
