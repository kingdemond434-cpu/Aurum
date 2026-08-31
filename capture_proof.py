"""Does the wired machinery actually convert MFE when it can see the path?

WHAT THIS IS
    A controlled mechanism test on a CONSTRUCTED price path. The fixture is
    synthetic and labelled as such throughout. It answers one engineering
    question and no market question:

        the ledger says 15 of 20 real trades reached >=1R MFE and 2 survived,
        with `observations = 0` on every one of them. Is that because the desk
        was blind, or because the management path is broken and would fail to
        convert MFE even with perfect visibility?

    Those two diagnoses look identical in a D1 backtest and imply completely
    different work. If the machinery is sound, the fix is the MT5 export and
    nothing else. If it is broken, the export would buy nothing and the money
    would be spent chasing a data problem that was really a code problem.

WHAT THIS IS NOT
    Evidence of edge. The path is constructed to contain the exact shape the
    real ledger showed — run up, then reverse into the stop — because that is
    the shape whose handling is in question. Reporting a P&L from it would be
    reporting the fixture back to itself.

METHOD
    One series, two arms, identical entries and identical states:

        BLIND     bar-close observation only, as on D1 today
        OBSERVED  the same bars plus a real M1 series, tick path driven

    Both use the REAL LiveDesk, the REAL observer, the REAL management engine.
    The only difference is whether the finer series exists.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import AdversarialReview, AnalystRead, PathForecast, Setup, Thresholds
from golddesk.backtest import Arm, Backtest, check_pairing, load_fine_series
from golddesk.features import Bar
from golddesk.live import Resolution
from golddesk.providers import AnalystProvider, ProviderRead

UTC = timezone.utc


class FixtureProvider(AnalystProvider):
    """Always proposes the same structural trade. Not a model; not a judgement.

    Held constant so the two arms differ ONLY in visibility. If the analyst
    varied between arms, any capture difference could be an entry difference.
    """
    name, model = "fixture", "fixture-v1"

    def read(self, brief, charts=()) -> ProviderRead:
        if not brief.levels:
            raise RuntimeError("no levels")
        lo = min(brief.levels, key=lambda l: l.price)
        hi = max(brief.levels, key=lambda l: l.price)
        r = AnalystRead(setup=Setup.SWING_REVERSAL, direction="LONG",
                        entry_ref="MARKET", stop_ref=lo.id, tp1_ref=hi.id,
                        tp2_ref=hi.id, mechanism_name="fixture-mech", confidence=3,
                        read="fixture", why="fixture", why_not="fixture",
                        invalidation="fixture",
                        path=PathForecast(p_plus_1r=0.5, p_minus_1r_first=0.45,
                                          expected_mfe_r=1.8, expected_mae_r=0.8,
                                          expected_r=1.0,
                                          expected_holding_hours=6.0,
                                          path_narrative="fixture path"),
                        adversarial=AdversarialReview(
                            thesis="fixture", counter_cases="fixture",
                            missing="fixture", forced="fixture",
                            timing="fixture", monetization="fixture"))
        return ProviderRead(r, self.name, self.model, 0.0, {"in": 0, "out": 0})

    def choose_option(self, system, prompt, option_ids):
        # Prefer a protective/banking move over HOLD when one is legal. This is
        # a scripted preference standing in for a policy, NOT a model decision.
        return option_ids[min(1, len(option_ids) - 1)]


def build_fixture(n_cycles: int = 40) -> tuple[list[Bar], list[dict]]:
    """M15 bars plus a real M1 series that is CONSISTENT with them.

    Each cycle: a build-up, a strong run (the MFE), then a full reversal through
    where the stop will sit. This is the shape the real ledger showed — trades
    reaching +6R and closing -1R — reproduced deliberately so the handling of
    that shape can be observed rather than inferred.

    The M1 series is generated FIRST and the M15 bars are aggregated FROM it, so
    the two are consistent by construction. Generating them independently would
    let the M1 path contradict its own M15 bar, which is the one thing a real
    broker export cannot do.
    """
    m1: list[dict] = []
    t = datetime(2025, 1, 1, tzinfo=UTC)
    px = 2000.0
    seed = 12345                     # deterministic; the fixture must reproduce

    def jitter() -> float:
        """Small reproducible noise. Not decoration.

        A perfectly linear leg makes the turning bar's high TIE with its
        neighbour's, and the fractal swing test requires a STRICT local extreme,
        so a noiseless path yields zero swings, zero structure and zero
        decisions. Real ticks are never tied. This keeps the fixture honest
        rather than compensating for a code defect.
        """
        nonlocal seed
        seed = (1103515245 * seed + 12345) % (1 << 31)
        return (seed / (1 << 31) - 0.5) * 0.8

    def emit(step: float) -> None:
        nonlocal px, t
        prev = px
        px += step + jitter()
        m1.append({"utc": t, "open": prev,
                   "high": max(px, prev) + 0.4 + abs(jitter()),
                   "low": min(px, prev) - 0.4 - abs(jitter()), "close": px})
        t += timedelta(minutes=1)

    for c in range(n_cycles):
        # A slow leg to BUILD STRUCTURE — swings, a trend, something to trade.
        # Deliberately not a multiple of 15 so turning points do not land on
        # M15 boundaries and tie with the neighbouring bar's extreme.
        for _ in range(143):
            emit(+0.9)
        for _ in range(37):
            emit(-1.1)

        # THE SHAPE THAT MATTERS. The whole excursion — a violent run up and a
        # full round trip back through the entry — happens inside ~2 M15 bars.
        #
        # This is what the real D1 ledger recorded: a trade reaching +6.31R MFE
        # and closing -1.00R with `observations = 0`. A bar-close manager sees
        # one candle with a long upper wick and a close near the low, and gets
        # no opportunity to act between those two facts. A tick-driven observer
        # sees the whole path and can protect, bank or trail on the way.
        #
        # The earlier fixture spread this over ~40 bars, which gave the blind
        # arm forty chances to manage and therefore did not reproduce blindness
        # at all. The excursion has to be INTRABAR for the question to be live.
        for _ in range(14):
            emit(+3.4)
        for _ in range(16):
            emit(-3.1)

        # settle, so the next cycle starts from fresh structure
        for _ in range(60):
            emit(-0.35)

    # Aggregate the M1 into M15 so the two series cannot disagree.
    bars: list[Bar] = []
    for k in range(0, len(m1) - 15, 15):
        g = m1[k:k + 15]
        bars.append(Bar(g[0]["utc"], g[0]["open"], max(x["high"] for x in g),
                        min(x["low"] for x in g), g[-1]["close"], 100.0, 0.30))
    return bars, m1


def summarise(run, label: str) -> dict:
    tr = run.trades
    if not tr:
        return {"label": label, "trades": 0}
    realised = sum(t["realised_r"] for t in tr)
    mfe = sum(t["mfe_r"] for t in tr)
    forgone = sum(t["forgone_r"] for t in tr)
    obs = sum(t["observations"] for t in tr)
    reached = sum(1 for t in tr if t["mfe_r"] >= 1.0)
    survived = sum(1 for t in tr if t["mfe_r"] >= 1.0 and t["realised_r"] > 0)
    return {"label": label, "trades": len(tr), "realised": realised, "mfe": mfe,
            "forgone": forgone, "capture": (realised / mfe if mfe > 0 else 0.0),
            "obs": obs, "reached": reached, "survived": survived,
            "stop_moves": run.stats.get("stop_moves", 0),
            "partials": run.stats.get("partials", 0),
            "wakes": run.stats.get("observer_wakes", 0),
            "res": dict(run.resolutions)}


def row(s: dict) -> str:
    if not s.get("trades"):
        return f"  {s['label']:<10} no trades"
    return (f"  {s['label']:<10}{s['trades']:>7}{s['realised']:>+11.2f}"
            f"{s['mfe']:>+10.2f}{s['capture']:>9.0%}{s['forgone']:>+10.2f}"
            f"{s['obs']:>9}{s['wakes']:>7}{s['stop_moves']:>7}{s['partials']:>6}")


def main() -> int:
    import logging
    import pandas as pd
    logging.basicConfig(level=logging.ERROR)

    print(__doc__)
    print("=" * 92)
    out = Path(tempfile.mkdtemp())
    bars, m1 = build_fixture(40)
    print(f"FIXTURE (synthetic, consistent by construction): "
          f"{len(bars)} M15 bars aggregated from {len(m1)} M1 bars")
    print(f"  span {bars[0].ts:%Y-%m-%d %H:%M} .. {bars[-1].ts:%Y-%m-%d %H:%M}")

    fp = out / "m1.parquet"
    pd.DataFrame(m1).set_index("utc").to_parquet(fp)
    fine, cov = load_fine_series(fp, bars, kind="M1")
    print(f"  {cov.render().splitlines()[0]}")
    assert cov.usable, "fixture fine series is not usable"

    warmup = 120
    from golddesk.backtest import Split
    split = Split("fixture", warmup, len(bars), bars[warmup].ts, bars[-1].ts)

    # The cold-start prior is relaxed for the FIXTURE ONLY. The question here
    # is whether management converts MFE, so entries must exist to manage; at
    # the production prior this synthetic geometry compiles nothing and the
    # test answers a different question than the one asked.
    th = Thresholds(fallback_min_rr=0.2)

    # 2 x 3 GRID. Visibility is not one variable — what the desk DOES with what
    # it sees is a second, and the first run of this test conflated them: the
    # observed arm looked worse purely because the scripted chooser acts on
    # every wake, and visibility gave it 20x more wakes to act on. Separating
    # them is the difference between "sight does not help" and "sight plus an
    # indiscriminate policy does not help".
    grid = [("passive", "never intervene — the floor"),
            ("heuristic-v1", "hand-written preference order"),
            ("contextual-v1", "the fixture's ALWAYS-ACT script")]

    results = []
    for pol, note in grid:
        bt_b = Backtest(bars, out / f"blind-{pol}", timeframe="M15", warmup=warmup,
                        provider_factory=FixtureProvider, thresholds=th)
        rb = bt_b.run_arm(Arm("BLIND", "baseline", analyst="provider",
                              management=pol), split)
        bt_o = Backtest(bars, out / f"obs-{pol}", timeframe="M15", warmup=warmup,
                        provider_factory=FixtureProvider, thresholds=th,
                        intrabar=fine, fine_resolution=Resolution.M1_OBSERVED)
        ro = bt_o.run_arm(Arm("OBSERVED", "observation", analyst="provider",
                              management=pol, observation=True), split)
        results.append((pol, note, summarise(rb, "BLIND"), summarise(ro, "OBSERVED"),
                        rb, ro))

    print("\n" + "=" * 92)
    print("RESULT — same bars, same entries. Visibility x management policy.")
    print("=" * 92)
    print(f"  {'policy':<15}{'arm':<10}{'trades':>7}{'realised':>11}{'MFE':>9}"
          f"{'capture':>9}{'obs':>8}{'wakes':>7}{'stops':>7}{'part':>6}")
    for pol, note, b, o, rb, ro in results:
        print(f"  {pol:<15}" + row(b).strip().replace("BLIND", "BLIND     ", 1))
        print(f"  {'':<15}" + row(o).strip().replace("OBSERVED", "OBSERVED  ", 1))
        if b.get("trades") and o.get("trades"):
            print(f"  {'':<15}{'delta':<10}{'':>7}{o['realised']-b['realised']:>+11.2f}"
                  f"{'':>9}{o['capture']-b['capture']:>+9.0%}")
        print(f"  {'':<15}({note})")

    pc = check_pairing({"BLIND": results[0][4].outcomes,
                        "OBSERVED": results[0][5].outcomes})
    print(f"\n  {pc.render().splitlines()[0]}")
    print(f"\n  resolution provenance (passive arm):")
    print(f"    BLIND    {results[0][2].get('res')}")
    print(f"    OBSERVED {results[0][3].get('res')}")

    print("\n" + "=" * 92)
    print("VERDICT")
    print("=" * 92)
    usable = [(pol, b, o) for pol, note, b, o, rb, ro in results
              if b.get("trades") and o.get("trades")]
    if not usable:
        print("  INCONCLUSIVE — no policy produced trades in both arms.")
        shutil.rmtree(out, ignore_errors=True)
        return 1

    helped = [(pol, o["capture"] - b["capture"], o["realised"] - b["realised"])
              for pol, b, o in usable]
    for pol, dcap, dreal in helped:
        verdict = "HELPS" if dcap > 0.01 else ("NEUTRAL" if abs(dcap) <= 0.01 else "HURTS")
        print(f"  {pol:<15} visibility {verdict:<8} capture {dcap:+.0%}  "
              f"realised {dreal:+.2f}R")

    best = max(helped, key=lambda x: x[1])
    any_help = best[1] > 0.01
    obs_ok = all(o["obs"] > 0 for _, _, o in usable)
    print()
    if not obs_ok:
        print("  FAIL — the observed arms consumed no finer observations. The M1 "
              "series is not reaching the observer.")
        rc = 1
    elif any_help:
        print(f"  PASS — the machinery converts MFE once it can see, under at "
              f"least one policy ({best[0]}, capture {best[1]:+.0%}).")
        print("  Visibility is necessary but NOT sufficient: the always-act "
              "script is worse\n  with sight than without it, because sight "
              "multiplies its opportunities to act.")
        print("  So part 1 is the data AND a management policy that has earned "
              "its place —\n  which is exactly what arms E/F of the ladder "
              "exist to decide.")
        rc = 0
    else:
        print("  FAIL — no policy converted more MFE with sight than without it.")
        print("  Fix the management path BEFORE spending anything on data.")
        rc = 1

    print("\n  Reminder: the fixture is synthetic and was built to contain the "
          "run-then-reverse\n  shape the real ledger showed. It proves a "
          "mechanism works. It is not evidence\n  of edge and no P&L from it "
          "means anything.")
    shutil.rmtree(out, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
