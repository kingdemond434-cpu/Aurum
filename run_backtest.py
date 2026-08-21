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

# A sandbox upload path was hardcoded here, which does not exist on any machine
# you would actually run this on. The default now looks where the fetchers write.
DEFAULT_PARQUET = "data/XAUUSD_M15.parquet"


def estimate_cost(n_states: int, arms, *, per_read_usd: float = 0.28,
                  mgmt_steps_per_trade: float = 8.0,
                  entry_rate: float = 0.12,
                  wake_rate: float = 0.55) -> dict:
    """What this run will cost BEFORE it starts spending.

    THIS IS THE MISSING SAFETY RAIL. The ladder runs seven analyst arms over
    every state in the sample; at a few hundred thousand states that is a bill
    nobody intended to authorise, discovered afterwards. An estimate is not
    exact — token counts vary with brief size and charts — but the difference
    between "about forty dollars" and "about four thousand" is the decision, and
    that difference is never subtle.

    Chart arms cost materially more: three images per read is most of the input.
    """
    from golddesk.live import Vision
    from golddesk.policies import ContextualChooser

    rows, total = [], 0.0
    for a in arms:
        if a.analyst != "provider":
            rows.append({"arm": a.name, "reads": 0, "usd": 0.0,
                         "note": "deterministic baseline — no inference"})
            continue
        mult = 3.2 if a.vision is Vision.NUMERIC_PLUS_CHARTS else 1.0
        # NOT every state is a read. The watcher only wakes the analyst when
        # deterministic structure has changed, and the observed rate on a live
        # M15 run was about 55%. This is the single biggest term in the bill, so
        # it is a parameter rather than an assumption baked into the arithmetic.
        reads = int(n_states * wake_rate)
        usd = reads * per_read_usd * mult
        note = "numeric" if mult == 1.0 else "charts (~3x input tokens)"
        if a.management == ContextualChooser.name:
            extra = n_states * entry_rate * mgmt_steps_per_trade
            usd += extra * per_read_usd * 0.35      # management calls are small
            note += f" + ~{extra:,.0f} management calls"
        rows.append({"arm": a.name, "reads": reads, "usd": usd, "note": note})
        total += usd
    return {"rows": rows, "total_usd": total, "states": n_states}


def render_cost(est: dict) -> str:
    out = ["ESTIMATED INFERENCE COST", "",
           f"  states in sample: {est['states']:,}"]
    for r in est["rows"]:
        out.append(f"  arm {r['arm']}  {r['reads']:>7,} reads  "
                   f"${r['usd']:>9,.2f}   {r['note']}")
    out += ["", f"  TOTAL ESTIMATE  ${est['total_usd']:,.2f}", "",
            "  An ESTIMATE. Token counts vary with brief size and how much the",
            "  cached prefix is reused. Treat it as the right order of magnitude,",
            "  not a quote — and set a spend limit in the Anthropic console too,",
            "  because that limit is the one that actually stops anything."]
    if est["total_usd"] > 500:
        out += ["",
                "  THIS IS PROBABLY NOT THE RUN YOU WANT.",
                "",
                "  The full ladder prices every arm over every wake, and the",
                "  chart arms are ~3x the numeric ones. Before paying for seven",
                "  arms, note that ONE comparison gates all the others:",
                "",
                "      arm A (deterministic)  vs  arm B (Claude, numeric)",
                "",
                "  If Claude does not beat the baseline, C through H are answers",
                "  to a question that no longer matters — they all sit above B on",
                "  the ladder. If it does beat it, you have bought the fact that",
                "  decides everything else for a small fraction of this.",
                "",
                "      --arms AB              just that comparison",
                "      --from / --to          narrow the window",
                "",
                "  Spend the minimum that can change your mind. That is the whole",
                "  point of an ablation ladder — it is ordered so you can stop."]
    return "\n".join(out)


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
    ap.add_argument("--estimate-only", action="store_true",
                    help="price the run and stop. ALWAYS DO THIS FIRST — the "
                         "ladder runs every analyst arm over every state, and "
                         "the bill is not obvious from the arm count")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="refuse to start if the estimate exceeds this. A "
                         "second line of defence behind the console spend limit")
    ap.add_argument("--yes", action="store_true",
                    help="skip the cost confirmation prompt (for unattended runs)")
    ap.add_argument("--arms", default=None,
                    help="run only these rungs, e.g. AB or ABC. The ladder is "
                         "ORDERED so you can stop early: if B does not beat A, "
                         "C..H answer a question that no longer matters")
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

    if args.arms:
        want = {c.upper() for c in args.arms if c.isalpha()}
        kept = [a for a in arms if a.name in want]
        missing = want - {a.name for a in kept}
        if missing:
            print(f"requested arm(s) {sorted(missing)} are not runnable in this "
                  f"environment — they are omitted, never downgraded")
        if not kept:
            print("no runnable arms selected")
            return 2
        # A ladder must keep its baseline: every rung is measured as an
        # INCREMENT over the one beneath it, and a subset with no floor
        # measures nothing.
        if "A" not in {a.name for a in kept}:
            kept = [a for a in arms if a.name == "A"] + kept
            print("arm A re-added: every rung is an increment over the baseline, "
                  "so a subset without it has nothing to be an increment over")
        arms = kept

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

    # ---- PRICE IT BEFORE SPENDING ANYTHING ----------------------------
    est = estimate_cost(len(bars), arms)
    print(render_cost(est))
    print()
    if args.estimate_only:
        print("--estimate-only given; nothing was run and nothing was spent.")
        return 0
    if args.max_usd is not None and est["total_usd"] > args.max_usd:
        print(f"REFUSING TO START: estimate ${est['total_usd']:,.2f} exceeds "
              f"--max-usd ${args.max_usd:,.2f}.")
        print("Narrow the date range, drop the chart arms, or raise the cap "
              "deliberately.")
        return 2
    if has_key and est["total_usd"] > 25.0 and not args.yes:
        try:
            ans = input(f"This will spend roughly ${est['total_usd']:,.2f}. "
                        f"Type the word 'spend' to continue: ").strip()
        except EOFError:
            ans = ""
        if ans != "spend":
            print("aborted — nothing was spent.")
            return 1
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
    prereg = Preregistration(**_json.loads(pp.read_text(encoding='utf-8'))["spec"])
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
