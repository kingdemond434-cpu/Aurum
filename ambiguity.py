"""How much of the result is assumption rather than observation?

This is REAL-DATA measurement, on the broker's own XAUUSD bars. It answers one
question and no other: on what fraction of trades does the coarse series fail
to say whether the stop or the target came first, and how large is the R
interval that ambiguity leaves open?

That interval is the honest error bar on every backtest number produced without
M1 or tick data. It is not a performance result and implies nothing about edge.

Method: compile signals through the REAL compiler, walk forward on real bars,
and classify each resolution as OBSERVED (only one of stop/target touched in
the deciding bar) or AMBIGUOUS (the bar spans both, so ordering is unknown).
For the ambiguous ones, report both branches.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.analyst import AnalystRead, Refusal, Setup, Thresholds, compile_signal
from golddesk.features import atr, classify, swings
from golddesk.runner import ParquetBarSource, build_brief

PARQUET = "/root/.claude/uploads/353d9479-657d-5787-9c73-4a674604017c/c3041b3a-XAUUSD_D1.parquet"
MAX_HOLD = 40


def main() -> int:
    src = ParquetBarSource(PARQUET, timeframe="D1")
    bars = src.bars()
    sw, atrs = swings(bars), atr(bars)
    print(f"REAL XAUUSD {src.timeframe}: {len(bars)} bars "
          f"{bars[0].ts:%Y-%m-%d} .. {bars[-1].ts:%Y-%m-%d}\n")

    observed, ambiguous = [], []
    unresolved = 0

    for i in range(260, len(bars) - MAX_HOLD - 1):
        st = classify(bars, i, sw, atrs)
        if st is None:
            continue
        b = bars[i]
        brief = build_brief(bars, i, st, sw, b.close - 0.21, b.close + 0.21, 1.0,
                            None, (), timeframe=src.timeframe)
        if not brief.levels:
            continue
        lo = min(brief.levels, key=lambda l: l.price)
        hi = max(brief.levels, key=lambda l: l.price)
        read = AnalystRead(setup=Setup.SWING_REVERSAL, direction="LONG",
                           entry_ref="MARKET", stop_ref=lo.id, tp1_ref=hi.id,
                           tp2_ref=hi.id, mechanism_name="ambiguity-probe",
                           confidence=3, read="probe", why="probe",
                           why_not="probe", invalidation="probe")
        sig = compile_signal(brief, read, Thresholds(fallback_min_rr=1.2))
        if isinstance(sig, Refusal):
            continue

        risk = sig.risk
        done = False
        for j in range(i + 1, min(i + 1 + MAX_HOLD, len(bars))):
            fb = bars[j]
            hit_stop = fb.low <= sig.stop
            hit_tp = fb.high >= sig.tp2
            if hit_stop and hit_tp:
                ambiguous.append(((sig.stop - sig.entry) / risk,
                                  (sig.tp2 - sig.entry) / risk, j - i))
                done = True
                break
            if hit_stop:
                observed.append((sig.stop - sig.entry) / risk)
                done = True
                break
            if hit_tp:
                observed.append((sig.tp2 - sig.entry) / risk)
                done = True
                break
        if not done:
            unresolved += 1

    n = len(observed) + len(ambiguous)
    if not n:
        print("no signals compiled")
        return 1

    pess = list(observed) + [a[0] for a in ambiguous]
    opti = list(observed) + [a[1] for a in ambiguous]

    print(f"signals compiled and resolved : {n}   (timed out unresolved: {unresolved})")
    print(f"  ordering OBSERVED           : {len(observed):>5}  "
          f"({len(observed)/n:.1%}) — one side touched in the deciding bar")
    print(f"  ordering AMBIGUOUS          : {len(ambiguous):>5}  "
          f"({len(ambiguous)/n:.1%}) — the deciding bar spans BOTH stop and target\n")
    print("  the resulting error bar on total R, from ordering alone:")
    print(f"    pessimistic (assume stop first) : {sum(pess):>9.2f}R  "
          f"mean {statistics.fmean(pess):+.4f}R/trade")
    print(f"    optimistic  (assume tp   first) : {sum(opti):>9.2f}R  "
          f"mean {statistics.fmean(opti):+.4f}R/trade")
    print(f"    WIDTH OF THE UNKNOWN            : {sum(opti) - sum(pess):>9.2f}R  "
          f"({(sum(opti)-sum(pess))/max(abs(sum(pess)),1e-9):.0%} of the "
          f"pessimistic total)\n")
    if ambiguous:
        holds = [a[2] for a in ambiguous]
        print(f"  ambiguous trades decided after a median of {statistics.median(holds):.0f} "
              f"bar(s)")

    # The conclusion is DERIVED from the measurement. Writing it in advance is
    # how a report ends up asserting something its own numbers contradict.
    width = sum(opti) - sum(pess)
    print("\n  CONCLUSION")
    if not ambiguous:
        print("  Exit ORDERING is not a source of error on this configuration: no\n"
              "  deciding bar spanned both the stop and the target, so first-touch is\n"
              "  observed rather than assumed on every trade. That is a property of\n"
              "  wide targets on a daily series, and it does NOT generalise — at M15\n"
              "  with closer objectives the same test is the one that matters.\n"
              "\n"
              "  It also does not make this series adequate. The ordering question is\n"
              "  the SMALLER of the two intrabar problems. The larger one is that a\n"
              "  daily bar cannot drive management at all: excursion, profit-lock,\n"
              "  partials and runners all resolve inside a day, and on D1 the desk\n"
              "  sees one price per session. That gap is not measurable from these\n"
              "  bars, so it is not measured here, and no management result computed\n"
              "  on D1 should be believed.")
    else:
        print(f"  Ordering is undetermined on {len(ambiguous)/n:.1%} of trades, leaving a\n"
              f"  {width:.2f}R interval that the data cannot close. Any total quoted\n"
              f"  from this series is a choice of assumption within that band. M1 or\n"
              f"  tick history collapses it to a point; nothing else does.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
