"""Large moves the desk did not trade — the false-negative ledger, kept as facts not regrets.

WHY THIS IS NOT THE SAME AS `opportunity.py`

`opportunity.py` gates the trades the brain PROPOSED: given a candidate, is its expected value
positive. It is a filter on things that reached the gate. Nothing in the desk asks the opposite
question — what moved while the brain said nothing at all — and that asymmetry is self-serving in
a specific way: every recorded outcome is a trade the desk chose to take, so the record can only
ever show how good the selections were, never how much was left on the table.

Aurum's own charter says it has six false negatives on record and needs hundreds. This produces
them mechanically.

WHAT COUNTS AS MISSED, AND WHY THE BAR IS IN R

A move is only "missed" relative to what the desk could have risked on it. Ten dollars of gold is
a rout in one volatility regime and noise in another, so the threshold is in ATR-denominated R,
not price. `THRESHOLD_R` of 2.0 means: a clean directional run worth at least twice the stop the
desk would have used, with no signal against it.

THE THREE WAYS THIS COULD LIE, AND WHAT IS DONE ABOUT EACH

  IT COULD COUNT MOVES NOBODY COULD HAVE CAUGHT. A move that reverses within the same bar it
  completes is not an opportunity, it is hindsight drawing a line between two extremes. Runs are
  measured from a bar's close forward, never from an intrabar low to a later intrabar high, so
  every reported move is one an entry at a real closing price could have participated in.

  IT COULD COUNT MOVES THE DESK DID TRADE. A signal anywhere inside the run's window disqualifies
  it — including a signal in the WRONG direction, which is a different failure (mis-read, already
  covered by attribution.py) and must not be double-counted here.

  IT COULD BECOME A REGRET MACHINE. Every large move in a market will look catchable afterwards.
  The output is therefore a COUNT AND A DISTRIBUTION, not a per-move indictment: the useful
  question is "how much of the available move did the desk participate in this month", and that
  is a rate, not a story about Tuesday.

THE OUTPUT IS EVIDENCE, NOT A TARGET. Driving missed moves to zero means trading everything,
which `opportunity.py` exists to prevent. The number is meant to be read next to the win rate,
where a desk that misses nothing and loses often is visibly worse than one that misses plenty.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

MISSED_MOVE_VERSION = "missed-move-2026-08-20-a"

#: Minimum size, in R, for an untraded run to count. Below this the desk standing aside is not a
#: failure worth recording — it is the ordinary business of not trading noise.
THRESHOLD_R = 2.0

#: Bars the run may take to develop. Beyond this it is a trend, not a move a single entry with a
#: fixed stop would have captured, and counting it would inflate the ledger with things no trade
#: could have held.
MAX_RUN_BARS = 48


@dataclass(frozen=True)
class MissedMove:
    """One untraded run, with everything needed to argue it should have been taken."""

    start_ts: str
    end_ts: str
    direction: str              # UP | DOWN
    move_r: float
    bars: int
    entry_close: float
    extreme: float
    atr_at_start: float

    def render(self) -> str:
        return (f"{self.start_ts} {self.direction} {self.move_r:.1f}R over {self.bars} bars "
                f"({self.entry_close:.2f} -> {self.extreme:.2f})")


@dataclass(frozen=True)
class MissedReport:
    """The distribution. Deliberately not a list of individual regrets — see the docstring."""

    scanned_bars: int
    signals: int
    missed: int
    total_missed_r: float
    largest_r: float
    threshold_r: float
    max_run_bars: int
    state: str
    why: str
    examples: tuple[MissedMove, ...] = ()

    def to_prompt(self) -> str:
        if self.state != "MEASURED":
            return f"[MISSED MOVES]\n  {self.state}: {self.why}\n[/MISSED MOVES]"
        return (f"[MISSED MOVES — {MISSED_MOVE_VERSION}]\n"
                f"  {self.missed} untraded run(s) >= {self.threshold_r:.1f}R across "
                f"{self.scanned_bars} bars, {self.signals} signal(s) given\n"
                f"  total {self.total_missed_r:.1f}R left, largest {self.largest_r:.1f}R\n"
                f"  {self.why}\n[/MISSED MOVES]")


def _signal_bars(signal_times: Iterable[datetime], ts_index: dict) -> set[int]:
    """Map signal timestamps onto bar indices. Unmatched timestamps are DROPPED LOUDLY by the
    caller's count, never silently treated as 'no signal' — see `scan`."""
    out = set()
    for t in signal_times:
        i = ts_index.get(t)
        if i is not None:
            out.add(i)
    return out


def scan(bars: Sequence, atrs: Sequence[Optional[float]],
         signal_times: Sequence[datetime] = (),
         *, threshold_r: float = THRESHOLD_R,
         max_run_bars: int = MAX_RUN_BARS) -> MissedReport:
    """Find runs of >= `threshold_r` that had no signal anywhere inside them.

    `bars` need `.ts`, `.high`, `.low`, `.close`. `atrs` is the ATR series aligned to `bars`
    (None during warmup), which is what makes the threshold volatility-relative.

    **RUNS ARE MEASURED CLOSE-FORWARD.** From each bar's close, the best excursion reachable in
    the next `max_run_bars` is computed in both directions. That is deliberately conservative
    against the alternative of extreme-to-extreme, which would report moves that existed only
    between two ticks nobody could have traded between.

    Overlapping runs are collapsed: once a bar is inside a counted run, later bars starting inside
    it do not open a second one. Without that, one clean trend reports as forty separate missed
    moves and the ledger becomes noise.
    """
    n = len(bars)
    if n < 2 or len(atrs) != n:
        return MissedReport(n, len(signal_times), 0, 0.0, 0.0, threshold_r, max_run_bars,
                            "UNMEASURED",
                            f"need >=2 bars and a matching ATR series; got {n} bars and "
                            f"{len(atrs)} ATR values")

    ts_index = {b.ts: i for i, b in enumerate(bars)}
    matched = _signal_bars(signal_times, ts_index)
    unmatched = len(signal_times) - len(matched)

    found: list[MissedMove] = []
    covered_until = -1
    for i in range(n - 1):
        if i <= covered_until:
            continue
        a = atrs[i]
        if a is None or a <= 0:
            continue
        entry = float(bars[i].close)
        end = min(i + max_run_bars, n - 1)
        hi = max(float(b.high) for b in bars[i + 1:end + 1])
        lo = min(float(b.low) for b in bars[i + 1:end + 1])
        up_r, dn_r = (hi - entry) / a, (entry - lo) / a
        best_r, direction, extreme = ((up_r, "UP", hi) if up_r >= dn_r else (dn_r, "DOWN", lo))
        if best_r < threshold_r:
            continue
        # A signal ANYWHERE in the window means the desk was engaged. Wrong-direction signals
        # count as engaged too: that is a mis-read, which attribution.py owns, not a miss.
        if any(j in matched for j in range(i, end + 1)):
            covered_until = end
            continue
        j = next((k for k in range(i + 1, end + 1)
                  if (float(bars[k].high) >= extreme if direction == "UP"
                      else float(bars[k].low) <= extreme)), end)
        found.append(MissedMove(
            start_ts=bars[i].ts.isoformat(), end_ts=bars[j].ts.isoformat(),
            direction=direction, move_r=round(best_r, 2), bars=j - i,
            entry_close=round(entry, 2), extreme=round(extreme, 2), atr_at_start=round(a, 4)))
        covered_until = j

    total = sum(m.move_r for m in found)
    largest = max((m.move_r for m in found), default=0.0)
    why = (f"threshold {threshold_r:.1f}R, window {max_run_bars} bars, runs measured "
           "close-forward and de-overlapped")
    if unmatched:
        why += (f". {unmatched} signal timestamp(s) matched no bar and were IGNORED — if that "
                "count is large the signal log and the bar series are not the same clock, and "
                "this report is overstating misses")
    return MissedReport(n, len(signal_times), len(found), round(total, 2), largest,
                        threshold_r, max_run_bars, "MEASURED", why,
                        tuple(sorted(found, key=lambda m: -m.move_r)[:5]))


def append_ledger(report: MissedReport, path: Path) -> None:
    """Append-only. A missed-move record is forward evidence like any other and is never rewritten."""
    row = {"version": MISSED_MOVE_VERSION, **asdict(report),
           "examples": [asdict(m) for m in report.examples]}
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
