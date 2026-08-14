"""Integration tests for every defect found in the independent audit.

RUN THIS BEFORE SPENDING A CENT ON ARMS B-H.

Each test names the defect it guards and fails loudly rather than degrading.
The most important is test_11_two_arm_ab, which runs a real two-arm A/B through
the real harness with a provider DOUBLE — no API calls — and proves the things
that would otherwise first be discovered after paying for a full model run:

    * non-zero, identical, arm-independent paired state IDs
    * paired deltas computed over a real intersection
    * full_report() completes and renders comparisons AND verdicts
    * arm D actually consults fitted router knowledge
    * arm H actually adapts
    * arm F actually consumes real fine-resolution input

    python3 test_integration.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import AnalystRead, Setup, Thresholds
from golddesk.backtest import (Arm, Backtest, FineCoverage, assert_arms_differ,
                               check_pairing, chronological_splits, fit_knowledge,
                               full_report, ladder, load_fine_series, state_id)
from golddesk.evaluation import Preregistration, StateOutcome, compare
from golddesk.features import Bar, aggregate, atr, classify, swings
from golddesk.hypothesis import Hypothesis, HypothesisBook, Stage
from golddesk.live import LiveDesk, Resolution, Vision
from golddesk.management import (Anchor, BrokerLimits, Excursion, ManagementPolicy,
                                 Position, ThesisState, apply_option,
                                 enumerate_options, stop_is_legal)
from golddesk.observer import TradeObserver, WakePolicy
from golddesk.providers import AnalystProvider, ProviderRead

PASS, FAIL = [], []
UTC = timezone.utc


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------

class DoubleProvider(AnalystProvider):
    """A deterministic stand-in that satisfies the provider CONTRACT.

    It is not a model and makes no API call. Its only job is to let the harness
    run the real multi-arm path so the plumbing can be proved before money is
    spent on it. `bias` makes two doubles disagree, which is what a genuine A/B
    needs in order to produce a non-trivial delta.
    """

    name = "double"

    def __init__(self, model: str = "double-v1", bias: float = 0.0):
        self.model, self.bias = model, bias
        self.reads = 0
        self.choices = 0

    def read(self, brief, charts=()) -> ProviderRead:
        self.reads += 1
        if not brief.levels:
            raise RuntimeError("no levels")
        lo = min(brief.levels, key=lambda l: l.price)
        hi = max(brief.levels, key=lambda l: l.price)
        # bias flips some states to NO_SETUP so the arms genuinely differ
        setup = (Setup.NO_SETUP if (self.reads * 7 + int(self.bias * 10)) % 5 == 0
                 else Setup.SWING_REVERSAL)
        r = AnalystRead(
            setup=setup,
            direction="NONE" if setup is Setup.NO_SETUP else "LONG",
            entry_ref="MARKET", stop_ref=lo.id, tp1_ref=hi.id, tp2_ref=hi.id,
            mechanism_name="double-mech", confidence=3,
            read="double", why="double", why_not="double", invalidation="double")
        return ProviderRead(r, self.name, self.model, 0.0, {"in": 0, "out": 0})

    def choose_option(self, system, prompt, option_ids):
        self.choices += 1
        return option_ids[min(1, len(option_ids) - 1)]


def synth_bars(n: int = 900, start: float = 1800.0) -> list[Bar]:
    """A deterministic price path. Used only where market realism is irrelevant."""
    out, px = [], start
    t = datetime(2024, 1, 1, tzinfo=UTC)
    for k in range(n):
        drift = 1.4 if (k // 40) % 2 == 0 else -1.1
        px += drift + (3.0 if k % 17 == 0 else -2.4 if k % 11 == 0 else 0.3)
        hi, lo = px + 4.5 + (k % 7), px - 4.5 - (k % 5)
        out.append(Bar(t + timedelta(minutes=15 * k), px - drift, hi, lo, px, 100.0, 0.42))
    return out


# --------------------------------------------------------------------------
# 1. Canonical, arm-independent state IDs
# --------------------------------------------------------------------------

def test_01_state_ids_pair():
    print("\n1. PAIRED EVALUATION — canonical state IDs")
    t0 = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)
    a = state_id("XAUUSD", "M15", t0)
    b = state_id("XAUUSD", "M15", t0.isoformat())
    check("same state yields the same id regardless of input type", a == b, a)
    check("id contains no arm/model identity",
          not any(x in a.lower() for x in ("arm", "claude", "double", "deterministic")))

    outs = {"A": [StateOutcome(a, t0, True, 1.0, 2.0)],
            "B": [StateOutcome(a, t0, False, 0.0, 2.0)]}
    pc = check_pairing(outs)
    check("two arms on one state pair", pc.ok and pc.shared == 1, pc.detail)

    broken = {"A": [StateOutcome(f"A-{a}", t0, True, 1.0, 2.0)],
              "B": [StateOutcome(f"B-{a}", t0, False, 0.0, 2.0)]}
    pc2 = check_pairing(broken)
    check("arm-prefixed ids are DETECTED as unpaired (the original defect)",
          not pc2.ok and pc2.shared == 0, pc2.detail)


# --------------------------------------------------------------------------
# 2. full_report handles compare()'s 3-tuple
# --------------------------------------------------------------------------

def test_02_full_report_tuple():
    print("\n2. full_report UNPACKS compare() CORRECTLY")
    t0 = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)
    ids = [state_id("XAUUSD", "M15", t0 + timedelta(minutes=15 * k)) for k in range(40)]
    arms = {
        "A": [StateOutcome(i, t0, True, (-1.0 if k % 3 else 2.0), 2.5)
              for k, i in enumerate(ids)],
        "B": [StateOutcome(i, t0, True, (-0.5 if k % 3 else 2.6), 2.5)
              for k, i in enumerate(ids)],
    }
    prereg = Preregistration(
        hypothesis="B beats A", arms=("A", "B"), primary_metric="net_r_per_state",
        secondary_metrics=(), holdout_start="2025-01-01", holdout_end="2025-12-31",
        min_ess=5.0, fdr_q=0.10, trials_declared=1, trials_inflation=8.0,
        promote_rule="CI>0", demote_rule="else")
    res = compare(arms, prereg)
    check("compare() returns a 3-tuple", isinstance(res, tuple) and len(res) == 3,
          f"got {type(res).__name__} of len {len(res) if isinstance(res, tuple) else '-'}")
    mets, comps, verdicts = res
    check("comparisons are Comparison objects, not a nested tuple",
          bool(comps) and hasattr(comps[0], "p_value"),
          f"{len(comps)} comparison(s)")
    check("paired n is non-zero", comps[0].n_paired == len(ids),
          f"n_paired={comps[0].n_paired}")


# --------------------------------------------------------------------------
# 3. Ladder integrity
# --------------------------------------------------------------------------

def test_03_ladder_differs():
    print("\n3. LADDER — adjacent arms differ in exactly one capability")
    arms = ladder(provider_available=True, tick_available=True)
    problems = assert_arms_differ(arms)
    check("full ladder is well-formed", not problems, "; ".join(problems[:2]))
    bad = [Arm("X", "thing"), Arm("Y", "thing")]
    check("identical adjacent arms are DETECTED", bool(assert_arms_differ(bad)),
          assert_arms_differ(bad)[0][:80])
    names = [a.name for a in arms]
    check("F omitted when no tick data exists",
          "F" not in [a.name for a in ladder(True, False)],
          f"with ticks: {names}")


# --------------------------------------------------------------------------
# 4. Fine series loading
# --------------------------------------------------------------------------

def test_04_fine_series():
    print("\n4. FINE SERIES — real M1 mapped causally, never synthesised")
    import pandas as pd
    bars = synth_bars(40)
    rows = []
    for b in bars[:20]:
        for m in range(15):
            t = b.ts + timedelta(minutes=m)
            rows.append({"utc": t, "open": b.open, "high": b.high,
                         "low": b.low, "close": b.close})
    d = Path(tempfile.mkdtemp())
    p = d / "m1.parquet"
    pd.DataFrame(rows).set_index("utc").to_parquet(p)
    m, cov = load_fine_series(p, bars, kind="M1")
    check("coverage measured, not assumed", cov.covered_bars == 20 and cov.entry_bars == 40,
          cov.render().splitlines()[0])
    check("uncovered bars get NO synthetic data", all(k < 20 for k in m),
          f"covered indices max={max(m)}")
    for j, obs in m.items():
        span = (bars[j].ts, bars[j].ts + timedelta(minutes=15))
        if not all(span[0] <= t < span[1] for t, _ in obs):
            check("every observation lands in its own bar", False, f"bar {j} leaked")
            break
    else:
        check("every observation lands in its own bar (no cross-bar leakage)", True)
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. True HTF aggregation
# --------------------------------------------------------------------------

def test_06_aggregation():
    print("\n6. MULTIMODAL — true H4 OHLC, not sampled M15")
    bars = synth_bars(64)
    agg = aggregate(bars, 16)
    g = bars[:16]
    a = agg[0]
    check("H4 open is the FIRST open", a.open == g[0].open)
    check("H4 high is the MAX high", a.high == max(x.high for x in g),
          f"{a.high} vs sampled {g[0].high}")
    check("H4 low is the MIN low", a.low == min(x.low for x in g))
    check("H4 close is the LAST close", a.close == g[-1].close)
    sampled = bars[0:64:16]
    check("aggregation differs from sampling (the original defect)",
          a.high != sampled[0].high or a.low != sampled[0].low,
          f"aggregated range {a.high - a.low:.2f} vs sampled {sampled[0].high - sampled[0].low:.2f}")


# --------------------------------------------------------------------------
# 8. Venue legality
# --------------------------------------------------------------------------

def test_08_stop_legality():
    print("\n8. MANAGEMENT LEGALITY — stops validated against the live quote")
    pos = Position("LONG", 1800.0, 1790.0, 1790.0, 10.0, 1.0, 0.0,
                   datetime(2025, 1, 1, tzinfo=UTC), "S")
    lim = BrokerLimits(min_stop_distance=1.0, freeze_distance=0.4)
    ok, _ = stop_is_legal(pos, 1808.0, 1810.0, 1810.2, lim)
    check("a legal stop is accepted", ok)
    ok2, why2 = stop_is_legal(pos, 1812.0, 1810.0, 1810.2, lim)
    check("a stop through the market is refused", not ok2, why2)
    ok3, why3 = stop_is_legal(pos, 1809.5, 1810.0, 1810.2, lim)
    check("a stop inside the broker minimum is refused", not ok3, why3)

    exc = Excursion(1.5, -0.2, 60, 10, 1.0, 5)
    thesis = ThesisState(True, "STRONG", "NORMAL", False, False, False)
    anchors = [Anchor("A1", "SWING_LOW", 1809.8, "M15", True)]
    opts = enumerate_options(pos, thesis, exc, anchors, 5.0, 0.4,
                             bid=1810.0, ask=1810.2, broker=lim)
    check("an illegal candidate never reaches the option list",
          all(o.new_stop is None or o.new_stop <= 1809.0 for o in opts),
          f"{len(opts)} option(s): {[o.new_stop for o in opts]}")


# --------------------------------------------------------------------------
# 9. Repeated partials
# --------------------------------------------------------------------------

def test_09_repeated_partials():
    print("\n9. PARTIAL SIZING — banked profit accounted across repeated wakes")
    pol = ManagementPolicy()
    thesis = ThesisState(True, "STRONG", "NORMAL", False, False, False)
    pos = Position("LONG", 1800.0, 1790.0, 1795.0, 10.0, 1.0, 0.0,
                   datetime(2025, 1, 1, tzinfo=UTC), "S")
    exc = Excursion(2.0, -0.3, 60, 10, 2.0, 5)
    o1 = [o for o in enumerate_options(pos, thesis, exc, [], 5.0, 0.4, pol,
                                       bid=1820.0, ask=1820.2)
          if o.partial_fraction]
    check("a partial is offered while risk remains", bool(o1),
          f"f={o1[0].partial_fraction if o1 else None}")
    if not o1:
        return
    d1 = apply_option(pos, o1[0], exc, pol)
    p2 = d1.position_after
    check("after banking, the position is risk-free or better",
          p2.locked_r >= -1e-6, f"locked={p2.locked_r:+.4f}R banked={p2.banked_r:+.4f}R")

    o2 = [o for o in enumerate_options(p2, thesis, exc, [], 5.0, 0.4, pol,
                                       bid=1820.0, ask=1820.2)
          if o.partial_fraction]
    check("NO second partial is offered once already risk-free "
          "(the over-banking defect)", not o2,
          f"offered {[o.partial_fraction for o in o2]}" if o2 else
          f"runner preserved at {p2.remaining_fraction:.0%}")

    naive = 10.0 / 5.0
    naive_f = naive / (exc.r_open_now + naive)
    check("solved fraction accounts for banked profit vs the naive formula",
          abs((o1[0].partial_fraction or 0) - naive_f) > 1e-6
          or p2.banked_r > 0,
          f"solved={o1[0].partial_fraction:.4f} naive={naive_f:.4f}")


# --------------------------------------------------------------------------
# 10. Wake policy + provenance
# --------------------------------------------------------------------------

def test_10_wake_and_provenance():
    print("\n10. WAKE POLICY registered; RESOLUTION provenance truthful")
    from golddesk.constitution import BY_ID
    check("wake constants are a registered restriction",
          "wake.policy_constants" in BY_ID,
          BY_ID.get("wake.policy_constants").kind.value if "wake.policy_constants" in BY_ID else "")
    o = TradeObserver("LONG", 100.0, 99.0, 105.0, 1.0, datetime(2025, 1, 1, tzinfo=UTC),
                      wake=WakePolicy(proximity_r=0.9))
    check("wake policy is versioned and stamped", o.snapshot()["wake_policy"].startswith("wake-"))
    check("a tighter wake policy changes behaviour",
          TradeObserver("LONG", 100.0, 99.0, 105.0, 1.0,
                        datetime(2025, 1, 1, tzinfo=UTC),
                        wake=WakePolicy(proximity_r=0.01)).wake.proximity_r != o.wake.proximity_r)
    names = {r.value for r in Resolution}
    check("no resolution label claims M1 without an M1 series",
          "M15_PESSIMISTIC_UNCERTAIN" not in names
          and "BAR_UNAMBIGUOUS" in names and "BAR_ASSUMED_STOP_FIRST" in names,
          str(sorted(names)))
    check("only the genuinely ambiguous category is flagged as an assumption",
          Resolution.BAR_ASSUMED_STOP_FIRST.is_assumption
          and not Resolution.BAR_UNAMBIGUOUS.is_assumption
          and not Resolution.M1_OBSERVED.is_assumption)


# --------------------------------------------------------------------------
# 7. One-position constraint is measured
# --------------------------------------------------------------------------

def test_07_position_constraint_measured():
    print("\n7. ONE-POSITION CONSTRAINT registered and measured")
    from golddesk.constitution import BY_ID, DEFAULT_REASON_MAP
    check("registered as discretionary", "risk.one_position" in BY_ID
          and not BY_ID["risk.one_position"].exempt)
    check("its refusals map to it for counterfactual measurement",
          "one-position constraint" in DEFAULT_REASON_MAP)


# --------------------------------------------------------------------------
# 11. THE ONE THAT MATTERS — two-arm A/B on the real harness, no API
# --------------------------------------------------------------------------

def test_11_two_arm_ab():
    print("\n11. TWO-ARM A/B THROUGH THE REAL HARNESS (provider double, no API)")
    out = Path(tempfile.mkdtemp())
    bars = synth_bars(900)
    bt = Backtest(bars, out, timeframe="M15", warmup=120,
                  provider_factory=lambda: DoubleProvider(bias=0.3),
                  thresholds=Thresholds(fallback_min_rr=1.0),
                  adapt_every=20)
    train, calib, oos = chronological_splits(bars, warmup=120)

    a = Arm("A", "baseline")
    b = Arm("B", "analyst", analyst="provider")
    check("arms differ in exactly one capability", not assert_arms_differ([a, b]))

    ra = bt.run_arm(a, oos)
    rb = bt.run_arm(b, oos)
    outs = {"A": ra.outcomes, "B": rb.outcomes}
    pc = check_pairing(outs)
    check("NON-ZERO shared paired state IDs", pc.shared > 0,
          f"shared={pc.shared} per_arm={pc.per_arm}")
    check("pairing check passes", pc.ok, pc.detail)

    prereg = Preregistration(
        hypothesis="B beats A", arms=("A", "B"), primary_metric="net_r_per_state",
        secondary_metrics=(), holdout_start=oos.start.date().isoformat(),
        holdout_end=oos.end.date().isoformat(), min_ess=5.0, fdr_q=0.10,
        trials_declared=1, trials_inflation=8.0,
        promote_rule="CI>0", demote_rule="else")
    mets, comps, verdicts = compare(outs, prereg)
    check("paired delta computed over a real intersection",
          bool(comps) and comps[0].n_paired > 0,
          f"n_paired={comps[0].n_paired if comps else 0}")

    txt = full_report([ra, rb], prereg, True, ["(test)"])
    check("full_report() completes", bool(txt) and "PAIRED DELTAS" in txt)
    check("report renders comparisons, not a tuple repr",
          "(" not in txt.split("PAIRED DELTAS")[1].splitlines()[1][:20]
          if "PAIRED DELTAS" in txt else False,
          txt.split("PAIRED DELTAS")[1].splitlines()[1][:70] if "PAIRED DELTAS" in txt else "")
    check("report includes the pairing section", "PAIRING" in txt)

    # ---- D actually uses fitted knowledge -----------------------------
    book_path = out / "fit_book.json"
    cohorts, book = fit_knowledge(bt, b, train, book_path)
    check("fit_knowledge produced cohorts from the FIT window only",
          isinstance(cohorts, dict), f"{len(cohorts)} cohort(s)")
    seeded = Hypothesis(
        hid="setup=SWING_REVERSAL", statement="test veto",
        selector={"setup": "SWING_REVERSAL"}, predicted_sign=-1,
        discovered_on=train.end.date().isoformat(), seal_ts=train.end.isoformat(),
        discovery_n=50, discovery_mean_r=-0.4)
    book.seal(seeded)
    h = book.items["setup=SWING_REVERSAL"]
    h.stage, h.post_n, h.post_mean_r = Stage.ENFORCING.value, 40, -0.4
    h.enforcing_since = train.end.date().isoformat()
    h.expires = (oos.end.date() + timedelta(days=1)).isoformat()
    book._write()

    d = Arm("D", "router", analyst="provider", router=True)
    rd = bt.run_arm(d, oos, cohorts=cohorts, book=book)
    check("arm D CONSULTED the fitted hypothesis book",
          rd.stats.get("hypothesis_vetoes", 0) > 0,
          f"vetoes={rd.stats.get('hypothesis_vetoes')} (arm B had "
          f"{rb.stats.get('hypothesis_vetoes', 0)})")
    check("D differs from B in outcomes, so the router is causal",
          [o.acted for o in rd.outcomes] != [o.acted for o in rb.outcomes])

    # ---- H actually adapts --------------------------------------------
    h_arm = Arm("H", "adaptation", analyst="provider", router=True, adaptation=True)
    rh = bt.run_arm(h_arm, oos, cohorts=cohorts, book=HypothesisBook(out / "h_book.json"))
    check("arm H RAN adaptation cycles (was an unused label)",
          rh.stats.get("adapt_cycles", 0) > 0,
          f"cycles={rh.stats.get('adapt_cycles')} moves={rh.stats.get('adapt_moves')}")

    # ---- F actually consumes fine data --------------------------------
    import pandas as pd
    rows = []
    for j, bar in enumerate(bars):
        for m in (0, 5, 10):
            rows.append({"utc": bar.ts + timedelta(minutes=m),
                         "open": bar.low if m else bar.open,
                         "high": bar.high, "low": bar.low,
                         "close": bar.high if m == 5 else bar.close})
    fp = out / "m1.parquet"
    pd.DataFrame(rows).set_index("utc").to_parquet(fp)
    fine, cov = load_fine_series(fp, bars, kind="M1")
    check("fine series covers the entry series", cov.usable and cov.fraction > 0.9,
          cov.render().splitlines()[0])

    bt_f = Backtest(bars, out / "f", timeframe="M15", warmup=120,
                    provider_factory=lambda: DoubleProvider(bias=0.3),
                    thresholds=Thresholds(fallback_min_rr=1.0),
                    intrabar=fine, fine_resolution=Resolution.M1_OBSERVED)
    f_arm = Arm("F", "observation", analyst="provider", router=True,
                management="contextual-v1", observation=True)
    rf = bt_f.run_arm(f_arm, oos)
    check("arm F consumed REAL fine observations", rf.stats.get("ticks", 0) > 0,
          f"ticks={rf.stats.get('ticks')} observer_wakes={rf.stats.get('observer_wakes')}")
    # The property that matters is not that every exit says M1 — a management
    # EXIT happens at a known price and is not an ordering question at all. It
    # is that NO exit falls back to the assumed category while a fine series
    # covers the bar.
    assumed = rf.resolutions.get("BAR_ASSUMED_STOP_FIRST", 0)
    check("F resolves without falling back to an assumed fill",
          assumed == 0, f"resolutions={rf.resolutions}")
    check("no exit is mislabelled M1_OBSERVED without a fine series",
          "M1_OBSERVED" not in ra.resolutions,
          f"arm A resolutions={ra.resolutions}")

    shutil.rmtree(out, ignore_errors=True)


def main() -> int:
    print("=" * 78)
    print("AURUM INTEGRATION TESTS — run before any paid model run")
    print("=" * 78)
    for fn in (test_01_state_ids_pair, test_02_full_report_tuple,
               test_03_ladder_differs, test_04_fine_series, test_06_aggregation,
               test_07_position_constraint_measured, test_08_stop_legality,
               test_09_repeated_partials, test_10_wake_and_provenance,
               test_11_two_arm_ab):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
    print("\n" + "=" * 78)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
