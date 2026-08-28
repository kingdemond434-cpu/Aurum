"""What comparable setups actually did — with intervals, not point estimates.

WHY THIS EXISTS SEPARATELY FROM build_cohorts

`opportunity.build_cohorts` answers one question — the shrunk hit rate feeding
the EV gate — and answers it well. It is not enough to decide anything else. A
mechanism's hit rate says nothing about how far winners travelled AGAINST the
entry before working, whether TP2 is reached often enough to justify keeping a
runner, or how wide the uncertainty on its expectancy is. Those are the numbers
that set stops, targets and size, and none of them existed.

THE LAW THIS FILE IS BUILT AROUND

A number computed from three trades is not a small number, it is a WRONG one,
and printing it next to a real one launders it. So every field here is Optional
and every one of them is None until its own minimum is met. `verdict` says which
of UNMEASURED / THIN / MEASURED applies, and `render()` prints the word rather
than a figure. This desk's most repeated defect is absence resolving to a clean
answer (L1.28a / WS-005); a statistics module is where that does the most damage,
because its output looks authoritative by construction.

WHAT IT READS

`opportunity.resolved_outcomes`, which is the desk's single reader and already
drops rows whose path was never observed (evidence_valid false). That matters
here more than anywhere: a quarantined row carries mfe 0 and mae 0, and those
two zeros would drag every excursion statistic below toward zero — hardest on
losers, which is exactly where stop placement is decided.

INTERVALS, NOT POINTS. Expectancy comes with a t-interval and win rate with a
Wilson interval, because "+0.17R" and "+0.17R [-0.41, +0.75]" support completely
different decisions and only one of them is honest at n=14.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Optional, Sequence

COHORT_STATS_VERSION = "cohort-2026-08-28-a"

#: Resolved trades before an expectancy figure is shown at all. Below this the
#: mean of the sample is dominated by which particular trades happened to land
#: in it, and an interval wide enough to be honest is wide enough to be useless.
MIN_FOR_EXPECTANCY = 8

#: Resolved trades before the cohort is called MEASURED rather than THIN. Matches
#: tiers.MEASURED_N so the two cannot disagree about the same word.
MIN_FOR_MEASURED = 30

#: Winners before their MAE distribution is used for stop guidance. Stop
#: placement off four winners is a stop placed by four coin flips.
MIN_WINNERS_FOR_MAE = 6

#: t multipliers for a two-sided 95% interval, by degrees of freedom. Table
#: rather than scipy: the desk has no scipy dependency and this is the whole of
#: what would be used. Values above 30 df converge on the normal 1.96.
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36,
        8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06,
        30: 2.04}


def _t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    for k in sorted(_T95):
        if df <= k:
            return _T95[k]
    return 1.96


def _mean_ci(xs: Sequence[float]) -> Optional[tuple[float, float]]:
    """Two-sided 95% t-interval on the mean, or None below two samples."""
    n = len(xs)
    if n < 2:
        return None
    sd = statistics.stdev(xs)
    if sd == 0.0:
        m = statistics.fmean(xs)
        return (m, m)
    half = _t95(n - 1) * sd / math.sqrt(n)
    m = statistics.fmean(xs)
    return (m - half, m + half)


def _wilson(wins: int, n: int) -> Optional[tuple[float, float]]:
    """Wilson score interval. Chosen over normal-approximation because at the
    sample sizes this desk actually has, the normal interval routinely runs
    below 0 or above 1 -- an interval that includes impossible values is a
    strong hint the method does not apply."""
    if n <= 0:
        return None
    z = 1.96
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Cohort:
    """One mechanism's measured history. Every figure is Optional BY DESIGN."""

    mechanism: str
    n: int
    #: Trades whose excursion was actually observed. Lower than `n` when rows
    #: predate the observer fix; kept separate so an excursion statistic is
    #: never quietly computed over a different sample than the expectancy.
    n_excursion: int
    verdict: str                                    # UNMEASURED | THIN | MEASURED

    net_expectancy_r: Optional[float] = None
    expectancy_ci: Optional[tuple[float, float]] = None
    win_rate: Optional[float] = None
    win_rate_ci: Optional[tuple[float, float]] = None
    median_winner_r: Optional[float] = None
    median_loser_r: Optional[float] = None

    #: THE STOP QUESTION. How far winners went AGAINST the entry before working.
    #: A stop tighter than this is not conservative, it is structurally wrong:
    #: it removes the trades the mechanism actually wins on.
    winner_mae_median_r: Optional[float] = None
    #: The stop that would have survived 80% of this mechanism's winners.
    winner_mae_p80_r: Optional[float] = None

    #: THE RUNNER QUESTION. Keeping 75% on for TP2 is only right if TP2 arrives.
    median_mfe_r: Optional[float] = None

    @property
    def capital_bearing(self) -> bool:
        """May this cohort price a live decision, or is it research only?

        A mechanism with no measured history is a hypothesis. It is allowed to
        FIRE -- this desk deliberately runs experiments to generate evidence --
        but it is not allowed to claim an expectancy while doing it.
        """
        return self.verdict == "MEASURED"

    def render(self) -> str:
        """One block, honest about what it does not know."""
        if self.verdict == "UNMEASURED":
            return (f"COHORT {self.mechanism}: UNMEASURED — {self.n} resolved "
                    f"trade(s), under {MIN_FOR_EXPECTANCY}. No expectancy, no "
                    f"stop guidance. This is a hypothesis, not a measurement.")
        out = [f"COHORT {self.mechanism}: {self.verdict} — n={self.n}"
               + (f", excursion on {self.n_excursion}"
                  if self.n_excursion != self.n else "")]
        if self.net_expectancy_r is not None:
            ci = (f" [{self.expectancy_ci[0]:+.2f}, {self.expectancy_ci[1]:+.2f}]"
                  if self.expectancy_ci else "")
            out.append(f"  net expectancy {self.net_expectancy_r:+.2f}R{ci}")
        if self.win_rate is not None:
            ci = (f" [{self.win_rate_ci[0]:.0%}, {self.win_rate_ci[1]:.0%}]"
                  if self.win_rate_ci else "")
            out.append(f"  win rate {self.win_rate:.0%}{ci}"
                       + (f", median winner {self.median_winner_r:+.2f}R"
                          if self.median_winner_r is not None else "")
                       + (f", median loser {self.median_loser_r:+.2f}R"
                          if self.median_loser_r is not None else ""))
        if self.winner_mae_median_r is not None:
            out.append(
                f"  winners went {self.winner_mae_median_r:+.2f}R against first "
                f"(80% within {self.winner_mae_p80_r:+.2f}R) — a stop tighter "
                f"than that removes the trades this mechanism wins on")
        else:
            out.append(f"  stop guidance UNMEASURED — under {MIN_WINNERS_FOR_MAE} "
                       f"observed winners")
        return "\n".join(out)


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def build(rows: Sequence[dict]) -> dict[str, Cohort]:
    """One Cohort per mechanism, from the desk's single resolved-outcome reader."""
    from .opportunity import resolved_outcomes

    by_mech: dict[str, list[dict]] = {}
    for o in resolved_outcomes(rows):
        by_mech.setdefault(o.get("mechanism_name") or "unnamed", []).append(o)
    return {k: summarise(k, v) for k, v in by_mech.items()}


def summarise(mechanism: str, outcomes: Sequence[dict]) -> Cohort:
    """Everything measurable about one mechanism, and nothing that is not."""
    rs = [_f(o.get("realised_r")) for o in outcomes]
    rs = [x for x in rs if x is not None]
    n = len(rs)

    # Excursion is counted over its OWN sample. A row with no observed path
    # contributes to expectancy and not to stop guidance, and conflating the two
    # denominators is how a statistic quietly describes a different set of
    # trades than its label claims.
    exc = [o for o in outcomes
           if _f(o.get("mae_r")) is not None and _f(o.get("mfe_r")) is not None]
    n_exc = len(exc)

    if n < MIN_FOR_EXPECTANCY:
        return Cohort(mechanism, n, n_exc, "UNMEASURED")

    verdict = "MEASURED" if n >= MIN_FOR_MEASURED else "THIN"
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]

    winner_mae = sorted(
        _f(o.get("mae_r")) or 0.0 for o in exc
        if (_f(o.get("realised_r")) or 0.0) > 0)
    mae_med = mae_p80 = None
    if len(winner_mae) >= MIN_WINNERS_FOR_MAE:
        mae_med = statistics.median(winner_mae)
        # MAE is negative. The stop that survives 80% of winners is the 20th
        # PERCENTILE of that negative distribution -- the deepest excursion the
        # mechanism's winners routinely produce, not the shallowest.
        idx = max(0, int(0.2 * (len(winner_mae) - 1)))
        mae_p80 = winner_mae[idx]

    mfes = [_f(o.get("mfe_r")) for o in exc]
    mfes = [x for x in mfes if x is not None]

    return Cohort(
        mechanism=mechanism, n=n, n_excursion=n_exc, verdict=verdict,
        net_expectancy_r=statistics.fmean(rs),
        expectancy_ci=_mean_ci(rs),
        win_rate=len(wins) / n,
        win_rate_ci=_wilson(len(wins), n),
        median_winner_r=statistics.median(wins) if wins else None,
        median_loser_r=statistics.median(losses) if losses else None,
        winner_mae_median_r=mae_med,
        winner_mae_p80_r=mae_p80,
        median_mfe_r=statistics.median(mfes) if mfes else None,
    )


def render_all(cohorts: dict[str, Cohort]) -> str:
    """The desk-wide view, ordered so the measured ones are read first."""
    if not cohorts:
        return ("COHORT STATISTICS — no resolved trades on any mechanism yet. "
                "Every signal the desk sends is currently a hypothesis.")
    order = {"MEASURED": 0, "THIN": 1, "UNMEASURED": 2}
    rows = sorted(cohorts.values(), key=lambda c: (order.get(c.verdict, 3), -c.n))
    head = (f"COHORT STATISTICS ({COHORT_STATS_VERSION}) — "
            f"{sum(1 for c in rows if c.verdict == 'MEASURED')} measured, "
            f"{sum(1 for c in rows if c.verdict == 'THIN')} thin, "
            f"{sum(1 for c in rows if c.verdict == 'UNMEASURED')} unmeasured")
    return "\n".join([head] + [c.render() for c in rows])
