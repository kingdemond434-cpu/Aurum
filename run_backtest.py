"""Run the walk-forward harness on whatever data and credentials exist.

Deliberately degrades LOUDLY. If there is no API key the provider arms are
omitted from the ladder rather than quietly falling back to the deterministic
baseline, because an arm labelled '+analyst' that ran without an analyst would
corrupt every comparison above it.

    python3 run_backtest.py [--parquet PATH] [--folds N]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.backtest import (Backtest, assert_arms_differ, check_pairing,
                               chronological_splits, fit_knowledge, full_report,
                               ladder, load_fine_series, resolution_note,
                               walk_forward_folds)
from golddesk.live import Resolution
from golddesk.evaluation import Preregistration, metrics
from golddesk.providers import AnthropicAnalyst
from golddesk.runner import ParquetBarSource

DEFAULT_PARQUET = ("/root/.claude/uploads/353d9479-657d-5787-9c73-4a674604017c/"
                   "c3041b3a-XAUUSD_D1.parquet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", default=DEFAULT_PARQUET)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--out", default="backtest_out")
    ap.add_argument("--m1", default=None,
                    help="parquet of REAL M1 bars; enables arm F")
    ap.add_argument("--ticks", default=None,
                    help="parquet of REAL ticks; preferred over --m1 for arm F")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    src = ParquetBarSource(args.parquet, timeframe=args.timeframe)
    bars = src.bars()
    print(f"data: {len(bars)} {args.timeframe} bars  "
          f"{bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d}")

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    # FINE SERIES — loaded from real files or absent. Never synthesised, and
    # arm F is only offered when observations actually cover the entry series.
    fine, cov, fine_res = {}, None, None
    src_path = args.ticks or args.m1
    if src_path:
        kind = "TICK" if args.ticks else "M1"
        fine, cov = load_fine_series(Path(src_path), bars, kind=kind)
        fine_res = (Resolution.TICK_OBSERVED if kind == "TICK"
                    else Resolution.M1_OBSERVED)
        print(cov.render())
        if not cov.usable:
            print("  fine series NOT USABLE — arm F will be omitted rather than "
                  "run on partial or mismatched data")
            fine, fine_res = {}, None
    has_ticks = bool(fine) and cov is not None and cov.usable
    arms = ladder(has_key, has_ticks)

    ladder_problems = assert_arms_differ(arms)
    if ladder_problems:
        print("LADDER IS MALFORMED — refusing to run:")
        for pr in ladder_problems:
            print("   ", pr)
        return 2
    print("ladder integrity: PASS — each rung differs in exactly its declared "
          "capability")
    print(f"credentials: ANTHROPIC_API_KEY={'set' if has_key else 'NOT SET'}   "
          f"tick/M1 history={'present' if has_ticks else 'ABSENT'}")
    print(f"ladder: {len(arms)} arm(s) runnable — "
          f"{', '.join(a.name for a in arms)}")
    if not has_key:
        print("  arms B..H OMITTED (not downgraded): every rung above the "
              "baseline requires the analyst.")
    print()
    for a in arms:
        print("  " + a.render())
    print()

    bt = Backtest(bars, Path(args.out), timeframe=args.timeframe,
                  provider_factory=(lambda: AnthropicAnalyst(model="claude-opus-5"))
                  if has_key else None,
                  intrabar=fine, fine_resolution=fine_res)

    train, calib, oos = chronological_splits(bars)
    print("SPLITS (chronological, never shuffled)")
    for s in (train, calib, oos):
        print("  " + s.render())
    print()

    prereg = Preregistration(
        hypothesis=("Each rung of the ablation ladder adds positive incremental "
                    "net R per state over the rung beneath it, out of sample."),
        arms=tuple(a.name for a in arms),
        primary_metric="net_r_per_state",
        secondary_metrics=("capture_rate", "forgone_r", "max_dd_r"),
        holdout_start=oos.start.date().isoformat(),
        holdout_end=oos.end.date().isoformat(),
        min_ess=20.0, fdr_q=0.10,
        trials_declared=max(1, len(arms) - 1), trials_inflation=8.0,
        promote_rule="paired CI lower bound > 0 and survives BH-FDR",
        demote_rule="fails either, at any scheduled review")
    pp = Path(args.out) / "prereg.json"
    pp.parent.mkdir(parents=True, exist_ok=True)
    if pp.exists():
        pp.unlink()
    frozen_hash = prereg.freeze(pp)
    # read back the STAMPED spec — the in-memory object is deliberately not
    # mutated by freeze(), so reporting it directly would show frozen_at=None
    import json as _json
    prereg = Preregistration(**_json.loads(pp.read_text())["spec"])
    ok_spec, why_spec = Preregistration.verify(pp)
    print(f"preregistration frozen: {frozen_hash}  -> {pp}")
    print(f"  tamper check: {'PASS' if ok_spec else 'FAIL'} — {why_spec}\n")

    ok, notes = bt.leakage_report(oos)
    print(f"LEAKAGE truncation-invariance on OOS: {'PASS' if ok else 'FAIL'}")
    for n in notes[:8]:
        print("   ", n)
    print()

    folds = walk_forward_folds(bars, n_folds=args.folds)
    print(f"WALK-FORWARD: {len(folds)} fold(s), anchored origin")
    all_runs = []
    for f in folds:
        print(f"  fold {f.index}: fit {f.fit.start:%Y-%m-%d}..{f.fit.end:%Y-%m-%d} "
              f"-> test {f.test.start:%Y-%m-%d}..{f.test.end:%Y-%m-%d}")
        # FITTED KNOWLEDGE, causal: cohorts and sealed hypotheses derived from
        # the fold's pre-test window only. Arms that carry the router receive it;
        # arms below the router rung must not, or the rungs stop being a ladder.
        cohorts, book = fit_knowledge(bt, arms[-1], f.fit,
                                      Path(args.out) / f"book-fold{f.index}.json")
        print(f"      fitted on {f.fit.name}: {len(cohorts)} cohort(s), "
              f"{len(book.items)} sealed hypothesis(es), "
              f"{len(book.enforcing())} enforcing")
        for arm in arms:
            run = bt.run_arm(arm, f.test,
                             cohorts=cohorts if arm.router else None,
                             book=book if arm.router else None)
            all_runs.append(run)
            m = metrics(f"{arm.name}", run.outcomes)
            print(f"      {arm.name}: states={m.n_states:<5} acted={m.n_acted:<4} "
                  f"netR={m.net_r:>+8.2f} mean={m.mean_r_per_trade:>+7.3f} "
                  f"win={m.win_rate:>5.1%} capture={m.capture_rate:>6.1%} "
                  f"forgone={m.forgone_r:>+8.2f}")
    print()

    merged = {}
    for r in all_runs:
        merged.setdefault(r.arm.name, []).extend(r.outcomes)
    pc = check_pairing(merged)
    print(pc.render())
    if not pc.ok:
        print("\nPAIRING FAILED — every paired delta below would be computed over "
              "an empty or partial intersection. Refusing to report deltas.")
        return 3
    print()
    print(full_report(
        [r for r in all_runs], prereg, ok, notes))

    print()
    print("=" * 92)
    print("WHAT THIS IS AND IS NOT")
    print("=" * 92)
    if not has_key:
        print("  Only arm A ran. Arm A is the DETERMINISTIC BASELINE — it contains")
        print("  no model. Nothing here says anything whatsoever about Claude's")
        print("  trading ability, because Claude did not trade.")
    if not has_ticks:
        print("  No tick/M1 history. Management, profit-lock, partials and runners")
        print(f"  all resolve inside a single {args.timeframe} bar, which the desk")
        print("  cannot see. Treat every management-sensitive number as undefined.")
    print(f"  resolution mix: {resolution_note(all_runs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
