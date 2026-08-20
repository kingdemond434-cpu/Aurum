"""Gold seasonality — MEASURED from the desk's own bars, never asserted.

WHY THIS EXISTS INSTEAD OF THE OBVIOUS TABLE

The version this replaces shipped a hardcoded `MONTHLY_BIAS` dict: September "bullish, 0.65 win
rate, +3.5%, Peak festival demand, Diwali prep", March "bearish, 0.42, -1.2%, Post-Chinese demand
drop", and so on for twelve months, annotated "based on historical averages". Nobody computed
those numbers. They are a remembered folk table with two decimal places and a causal story
attached to each row.

That shape is worse than having no seasonality module at all, for three reasons:

  IT IS CONFIDENT AND CONSTANT. A wrong prior that fires every single day of a month, with a win
  rate quoted to two decimals, is a systematic bias the brain cannot argue with — it reads like a
  measurement because it is formatted like one.

  THE STORIES ARE CAUSAL CLAIMS. "Diwali prep" is not an observation, it is an explanation for an
  observation that was never made. Once a causal story is in the prompt the model will reason
  FROM it, and a fabricated mechanism produces confident reasoning about a market that is not
  there.

  IT CANNOT BE WRONG. A table nobody derived cannot be refuted by data, so it never updates. The
  desk's whole thesis is that experience compounds; a constant is the one thing that cannot.

So this module computes the table from bars, keeps the sample size beside every number, and
REFUSES to state a bias where the sample cannot carry one.

WHAT "ENOUGH" MEANS HERE, AND WHY IT IS STRICTER THAN IT LOOKS

Eight years of history gives EIGHT observations per calendar month. That is not a sample from
which a 0.65 win rate can be read — the standard error on eight Bernoulli trials is about 0.18, so
"0.65" and "0.50" are indistinguishable. `MIN_YEARS` is therefore a floor on honesty rather than a
formality, and months below it report UNMEASURED.

Even above the floor the answer is usually "no detectable bias", and that is the correct answer.
A seasonality module whose output is mostly NEUTRAL is not a failed module; it is a module that
declines to invent twelve signals from ninety-six observations.

MONTHLY RETURNS ARE COMPUTED FROM THE FIRST AND LAST CLOSE IN THE MONTH, on the bar series the
desk actually trades, so the number means what a position held that month would have earned —
not what a differently-sourced spot series did.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

SEASONALITY_VERSION = "seasonality-2026-08-20-a"

#: Distinct years a calendar month needs before ANY directional read is issued. Below this the
#: month reports UNMEASURED — see the docstring for the standard-error arithmetic.
MIN_YEARS = 8

#: |t| below which a month is NEUTRAL regardless of the mean. Two-sided, ~5% at these sample
#: sizes. Not tuned: it is the conventional bar, chosen before the numbers were looked at.
T_NEUTRAL = 2.0

MONTH_NAMES = ("", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December")


@dataclass(frozen=True)
class MonthStat:
    """One calendar month's measured behaviour, with the sample that produced it."""

    month: int
    n_years: int
    mean_return: float
    sd_return: float
    win_rate: float
    t_stat: float
    verdict: str                    # BULLISH | BEARISH | NEUTRAL | UNMEASURED
    why: str
    years: tuple[int, ...] = field(default_factory=tuple)


def monthly_returns(bars: Sequence, ) -> dict[tuple[int, int], float]:
    """(year, month) -> fractional return from the month's first close to its last.

    Takes anything with `.ts` (tz-aware datetime) and `.close`. Months are keyed by the bar's own
    timestamp, so a bar series in broker time is bucketed in broker time — which is the series the
    desk trades and therefore the only one whose seasonality is actionable.
    """
    first: dict[tuple[int, int], float] = {}
    last: dict[tuple[int, int], float] = {}
    for b in bars:
        ts = b.ts
        key = (ts.year, ts.month)
        if key not in first:
            first[key] = float(b.close)
        last[key] = float(b.close)
    return {k: (last[k] / first[k]) - 1.0 for k in first if first[k] > 0}


def measure(bars: Sequence) -> list[MonthStat]:
    """The twelve months, each measured or each refused. Never partly invented."""
    rets = monthly_returns(bars)
    out: list[MonthStat] = []
    for m in range(1, 13):
        pairs = sorted((y, r) for (y, mm), r in rets.items() if mm == m)
        years = tuple(y for y, _ in pairs)
        vals = [r for _, r in pairs]
        n = len(vals)
        if n < MIN_YEARS:
            out.append(MonthStat(
                m, n, 0.0, 0.0, 0.0, 0.0, "UNMEASURED",
                f"{n} year(s) of {MONTH_NAMES[m]} in the sample; {MIN_YEARS} required. "
                f"A win rate from {n} observations has a standard error near "
                f"{0.5 / math.sqrt(n) if n else float('nan'):.2f} — it cannot distinguish an "
                f"edge from a coin flip", years))
            continue
        mean = sum(vals) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
        wins = sum(1 for v in vals if v > 0)
        t = mean / (sd / math.sqrt(n)) if sd > 0 else (math.inf if mean > 0 else
                                                      (-math.inf if mean < 0 else 0.0))
        if abs(t) < T_NEUTRAL:
            verdict = "NEUTRAL"
            why = (f"mean {mean:+.2%} over {n} years, t={t:+.2f} — indistinguishable from zero. "
                   "This is the honest answer for most months")
        else:
            verdict = "BULLISH" if mean > 0 else "BEARISH"
            why = (f"mean {mean:+.2%} over {n} years, t={t:+.2f}, {wins}/{n} positive. "
                   f"ONE TEST, uncorrected for having looked at twelve months — at t={T_NEUTRAL} "
                   "roughly one month in twenty clears this by chance, and twelve were examined")
        out.append(MonthStat(m, n, mean, sd, wins / n, t, verdict, why, years))
    return out


def to_prompt(stats: Sequence[MonthStat], now: Optional[datetime] = None) -> str:
    """Only the CURRENT month goes to the brain, with its sample size attached.

    Handing over all twelve rows would invite the model to reason about next month's seasonality
    while deciding today's trade, which is a horizon it has no position over.
    """
    now = now or datetime.now(timezone.utc)
    cur = next((s for s in stats if s.month == now.month), None)
    if cur is None:
        return "[SEASONALITY]\n  UNAVAILABLE — no measurement for this month\n[/SEASONALITY]"
    head = f"[SEASONALITY — {MONTH_NAMES[cur.month]}, measured, {SEASONALITY_VERSION}]"
    if cur.verdict == "UNMEASURED":
        return f"{head}\n  UNMEASURED: {cur.why}\n[/SEASONALITY]"
    return (f"{head}\n"
            f"  {cur.verdict}: mean {cur.mean_return:+.2%}, win rate {cur.win_rate:.0%}, "
            f"n={cur.n_years} years (t={cur.t_stat:+.2f})\n"
            f"  {cur.why}\n"
            f"[/SEASONALITY]")


def build(bars: Sequence, out_path: Optional[Path] = None) -> dict:
    """Measure and persist. The artifact carries provenance so a reader can date the claim."""
    stats = measure(bars)
    ts = [b.ts for b in bars]
    art = {
        "version": SEASONALITY_VERSION,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "bars": len(bars),
        "span": [min(ts).isoformat(), max(ts).isoformat()] if ts else None,
        "min_years": MIN_YEARS,
        "t_neutral": T_NEUTRAL,
        "months": [asdict(s) for s in stats],
        "note": ("Measured from the desk's own bar series. Replaces a hardcoded MONTHLY_BIAS "
                 "table whose win rates and causal stories were asserted, not computed."),
    }
    if out_path:
        Path(out_path).write_text(json.dumps(art, indent=1), encoding="utf-8")
    return art


def load(path: Path) -> list[MonthStat]:
    """Rehydrate a persisted measurement. Raises if absent — a missing table is not a neutral one."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return [MonthStat(**{**m, "years": tuple(m.get("years", ()))}) for m in d["months"]]
