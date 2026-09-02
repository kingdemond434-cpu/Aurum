"""What the trade is likely to DO, not merely which way it points.

THE UPGRADE THIS IS. The desk's answer to "what is the trade here" is a
direction and a confidence out of five. That is a point estimate of a label, and
it cannot answer any of the questions that actually decide a position:

    how often does this reach +1R before the stop?
    how far does it typically go against me first?
    when does it get there — in twenty minutes, or in six hours?
    what does the BAD tenth of these look like?

Every one of those is estimable from the record the desk already keeps.
TRADE_CLOSED rows carry mfe_r, mae_r, t_mfe, t_mae and realised_r, which is
exactly a (censored) sample of the path. Nothing new has to be collected; the
numbers were being written down and never read.

WHY A BARRIER PROBABILITY IS EASY HERE AND HARD ELSEWHERE. The stop terminates
the trade at -1R, so any trade whose MFE reached +xR necessarily reached it
BEFORE -1R. "P(+1R before -1R)" is therefore just the fraction of trades with
mfe_r >= 1 — no path reconstruction, no assumption about the order of events.

AND WHY IT IS A LOWER BOUND, WHICH THE REPORT SAYS EVERY TIME. A managed exit
closes a trade that was still open. Its MFE is the best it achieved BEFORE being
closed, not the best it would have achieved, so every managed exit censors the
upper tail downward. The estimate is honest as a floor and dishonest as a point
value, and the difference is large enough to matter: this desk banks partials
and moves stops, so a large share of its closes are managed.

IT PREDICTS NOTHING IT HAS NOT SEEN. Every field is None until its own minimum
is met, and `verdict` prints UNMEASURED rather than a figure. A barrier
probability computed from six trades is not an estimate with wide error bars; it
is the sample itself, wearing a percentage sign.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

BARRIERS_VERSION = "barrier-2026-08-29-a"

#: Resolved trades before any probability is shown at all.
MIN_FOR_BARRIERS = 15

#: Resolved trades before the word MEASURED is used rather than THIN. Matches
#: cohort_stats.MIN_FOR_MEASURED so the two cannot disagree about the same word.
MIN_FOR_MEASURED = 30

#: Resolved trades before forward-return quantiles are shown. A 10th percentile
#: over fifteen samples is the second-worst trade in the set with a decimal
#: point after it.
MIN_FOR_QUANTILES = 20

#: The barriers estimated, in R. Round numbers on purpose: a fitted set of
#: barriers would be this desk choosing the levels at which its own record looks
#: best and calling the result a measurement.
BARRIERS_R = (0.5, 1.0, 2.0, 3.0)


def _wilson(k: int, n: int, z: float = 1.96) -> Optional[tuple[float, float]]:
    """Wilson interval. Correct at the edges, where normal approximation is not.

    A cohort that hit 15 of 15 has a real upper bound of 1.0 and a lower bound
    well under it; the textbook interval gives [1.0, 1.0], which is a claim of
    certainty from fifteen observations.
    """
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return round((c - h) / d, 4), round((c + h) / d, 4)


def _quantile(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return round(s[0], 4)
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 4)


@dataclass(frozen=True)
class Barrier:
    r: float
    hits: int
    n: int

    @property
    def p(self) -> float:
        return self.hits / self.n if self.n else 0.0

    @property
    def interval(self) -> Optional[tuple[float, float]]:
        return _wilson(self.hits, self.n)

    def to_dict(self) -> dict:
        return {"r": self.r, "hits": self.hits, "n": self.n,
                "p": round(self.p, 4), "interval": self.interval}


@dataclass
class Barriers:
    """The outcome distribution of one cohort, or of the desk as a whole."""
    label: str = "all"
    n: int = 0
    n_managed: int = 0                       # closes that censored the upper tail
    barriers: list[Barrier] = field(default_factory=list)
    mfe_mean: Optional[float] = None
    mae_mean: Optional[float] = None
    mae_p80: Optional[float] = None          # the deep tail, for stop placement
    minutes_to_mfe_median: Optional[float] = None
    r_q10: Optional[float] = None
    r_q50: Optional[float] = None
    r_q90: Optional[float] = None

    @property
    def verdict(self) -> str:
        if self.n < MIN_FOR_BARRIERS:
            return "UNMEASURED"
        return "MEASURED" if self.n >= MIN_FOR_MEASURED else "THIN"

    @property
    def censored_share(self) -> float:
        return self.n_managed / self.n if self.n else 0.0

    def p_at(self, r: float) -> Optional[float]:
        if self.verdict == "UNMEASURED":
            return None
        b = next((x for x in self.barriers if abs(x.r - r) < 1e-9), None)
        return None if b is None else round(b.p, 4)

    def to_dict(self) -> dict:
        return {"version": BARRIERS_VERSION, "label": self.label, "n": self.n,
                "verdict": self.verdict, "n_managed": self.n_managed,
                "censored_share": round(self.censored_share, 4),
                "barriers": [b.to_dict() for b in self.barriers],
                "mfe_mean": self.mfe_mean, "mae_mean": self.mae_mean,
                "mae_p80": self.mae_p80,
                "minutes_to_mfe_median": self.minutes_to_mfe_median,
                "r_q10": self.r_q10, "r_q50": self.r_q50, "r_q90": self.r_q90}

    def render(self) -> str:
        if self.verdict == "UNMEASURED":
            return (f"OUTCOME DISTRIBUTION [{self.label}]: UNMEASURED — {self.n} "
                    f"resolved trade(s), under {MIN_FOR_BARRIERS}. A barrier "
                    f"probability from this sample is not an estimate with wide "
                    f"error bars; it is the sample itself with a percent sign on "
                    f"it.")
        lines = [f"OUTCOME DISTRIBUTION [{self.label}] ({BARRIERS_VERSION}) — "
                 f"{self.n} resolved, {self.verdict}"]
        for b in self.barriers:
            iv = b.interval
            lines.append(f"  P(+{b.r:g}R before -1R)   {b.p:.0%}"
                         + (f"  [{iv[0]:.0%}, {iv[1]:.0%}]" if iv else ""))
        if self.mfe_mean is not None:
            lines.append(f"  mean MFE {self.mfe_mean:+.2f}R   mean MAE "
                         f"{self.mae_mean:+.2f}R   MAE p80 {self.mae_p80:+.2f}R")
        if self.minutes_to_mfe_median is not None:
            lines.append(f"  median time to its best point: "
                         f"{self.minutes_to_mfe_median:.0f} min")
        if self.r_q50 is not None:
            lines.append(f"  realised R quantiles  10% {self.r_q10:+.2f}   "
                         f"50% {self.r_q50:+.2f}   90% {self.r_q90:+.2f}")
        if self.n_managed:
            lines.append(
                f"  LOWER BOUND ON THE UPSIDE: {self.censored_share:.0%} of these "
                f"were closed by management while still open, so their MFE is the "
                f"best they reached BEFORE being closed, not the best they would "
                f"have reached. Every probability above is a floor.")
        return "\n".join(lines)


def _terminal(reason: Any) -> bool:
    """Did a BARRIER close this trade, rather than a decision?

    Deliberately explicit rather than a substring test. The first version asked
    whether the reason contained "STOP" or "TP", and "TARGET" contains neither,
    so every winner was filed as a censored management exit and the report
    claimed the entire sample was a lower bound.
    """
    s = str(reason or "").upper()
    return s.endswith("STOP") or "TARGET" in s or s.startswith("TP")


def _rows(rows: Sequence[dict], mechanism: Optional[str]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("kind") != "TRADE_CLOSED" or r.get("evidence_valid") is False:
            continue
        if mechanism and str(r.get("mechanism_name") or "") != mechanism:
            continue
        if not isinstance(r.get("realised_r"), (int, float)):
            continue
        out.append(r)
    return out


def estimate(rows: Sequence[dict], mechanism: Optional[str] = None) -> Barriers:
    """The outcome distribution of the resolved record. Pure; decides nothing.

    Quarantined rows are excluded for the same reason cohort_stats excludes
    them, and it matters most here: an unobserved path carries mfe 0 and mae 0,
    two numbers that are not measurements, and they would drag every barrier
    probability toward zero — hardest on the trades that worked.
    """
    got = _rows(rows, mechanism)
    b = Barriers(label=mechanism or "all", n=len(got))
    if not got:
        return b

    mfes = [float(r.get("mfe_r") or 0.0) for r in got]
    maes = [float(r.get("mae_r") or 0.0) for r in got]
    rs = [float(r["realised_r"]) for r in got]
    # CENSORED = closed by something other than the two terminal barriers.
    # A stop or a target ends the trade at a level that was going to end it
    # anyway, so the MFE observed is the MFE the trade had. Anything else —
    # a management exit, a time exit, a session flatten — closed a trade that
    # was still open, and its MFE is a floor rather than a fact.
    b.n_managed = sum(1 for r in got if not _terminal(r.get("reason")))

    if b.n < MIN_FOR_BARRIERS:
        return b

    b.barriers = [Barrier(x, sum(1 for m in mfes if m >= x - 1e-9), len(got))
                  for x in BARRIERS_R]
    b.mfe_mean = round(statistics.fmean(mfes), 4)
    b.mae_mean = round(statistics.fmean(maes), 4)
    # p80 of the DEPTH, so the number quoted is the drawdown that 80% of these
    # stayed inside — the figure a stop has to clear, not the average one.
    b.mae_p80 = _quantile([abs(m) for m in maes], 0.8)
    if b.mae_p80 is not None:
        b.mae_p80 = -b.mae_p80

    times = [float(r["t_mfe"]) / 60.0 for r in got
             if isinstance(r.get("t_mfe"), (int, float)) and r["t_mfe"] > 0]
    if times:
        b.minutes_to_mfe_median = round(statistics.median(times), 1)

    if b.n >= MIN_FOR_QUANTILES:
        b.r_q10, b.r_q50, b.r_q90 = (_quantile(rs, 0.1), _quantile(rs, 0.5),
                                     _quantile(rs, 0.9))
    return b


def by_mechanism(rows: Sequence[dict]) -> dict[str, Barriers]:
    names = {str(r.get("mechanism_name") or "") for r in rows
             if r.get("kind") == "TRADE_CLOSED"}
    return {n: estimate(rows, n) for n in sorted(names) if n}


def render_all(rows: Sequence[dict]) -> str:
    out = [estimate(rows).render()]
    for name, b in by_mechanism(rows).items():
        if b.verdict != "UNMEASURED":
            out.append(b.render())
    if len(out) == 1:
        out.append("  No mechanism has enough resolved trades for its own "
                   "distribution. That is the expected state early and it is "
                   "not a failure — it is the reason the desk-wide figure above "
                   "is the only one shown.")
    return "\n\n".join(out)
