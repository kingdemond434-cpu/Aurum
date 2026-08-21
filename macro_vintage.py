"""Point-in-time macro store. What was KNOWN on a date, not what is true about it.

Every macro gold model dies the same way, and it does not look like cheating.
You join real yields, ETF flows and CFTC positioning to price by the date the
observation REFERS TO, because that is the date printed on the data. The desk
could not have known those values then. The look-ahead is a few days per row and
systematic across the entire sample, which is the profile most likely to survive
a naive out-of-sample split and then fail live.

This repository already documented the sharp version of that hazard in
canonical/2026-08-13-cot-point-in-time-leakage.md (GOLD-COT-PIT-009), and the
three requirements below come straight from it:

  1. Store the ACTUAL publication datetime per row. "report date + 3 days" is
     wrong wherever a backlog was replayed — the CFTC published multiple reports
     in quick succession after the Oct-Nov 2025 suspension, so the offset is
     irregular and report-specific, and a hardcoded constant is wrong in a way
     that runs without error.

  2. Represent ABSENCE explicitly. For six weeks in 2025 no COT data existed.
     Forward-filling asserts positioning did not change during an active market;
     dropping the rows deletes a regime from the sample. Both are wrong. The
     correct state is UNAVAILABLE and the model must be able to see it.

  3. Keep REVISIONS. Real yields, flows and positioning are all restated. The
     first print is what you traded on; the revised value is what a naive
     backtest reads.

WHAT THIS IS NOT

It is not a prediction engine and it produces no probabilities. It is the
substrate that decides whether a prediction engine built on top of it would be
measuring the market or measuring its own look-ahead.
"""

from __future__ import annotations

import json
import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

UTC = timezone.utc
VINTAGE_VERSION = "vintage-2026-08-14-a"

UNAVAILABLE = None          # explicit: the desk knew nothing, and that is a state


@dataclass(frozen=True)
class Vintage:
    """One value, as it was published, at the moment it became knowable.

    `observation_date` is what the number describes. `published_utc` is when the
    desk could first have acted on it. Those are different fields because they
    are different facts, and collapsing them is the entire bug.
    """
    series_id: str
    observation_date: str        # ISO date the value REFERS TO
    published_utc: str           # ISO datetime it became KNOWABLE
    value: Optional[float]
    revision: int = 0            # 0 = first print; higher = restatement
    source: str = ""
    note: str = ""

    def known_by(self, when: datetime) -> bool:
        return datetime.fromisoformat(self.published_utc) <= when


class VintageStore:
    """Append-only vintages, queried strictly as-of a decision time."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._rows: list[Vintage] = []
        self._by_series: dict[str, list[Vintage]] = defaultdict(list)
        if self.path and self.path.exists():
            self.load()

    # -- ingest ----------------------------------------------------------
    def add(self, v: Vintage) -> None:
        self._rows.append(v)
        self._by_series[v.series_id].append(v)
        self._by_series[v.series_id].sort(key=lambda x: (x.published_utc, x.revision))

    def add_many(self, vs: Iterable[Vintage]) -> None:
        for v in vs:
            self.add(v)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as fh:
            for v in self._rows:
                fh.write(json.dumps(asdict(v)) + "\n")

    def load(self) -> "VintageStore":
        self._rows.clear()
        self._by_series.clear()
        for line in self.path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                self.add(Vintage(**json.loads(line)))
        return self

    # -- query -----------------------------------------------------------
    def as_of(self, series_id: str, when: datetime) -> tuple[Optional[float], str]:
        """The value the desk would have had at `when`, and how stale it was.

        Returns (value, provenance). `value` is None — UNAVAILABLE — when nothing
        had been published yet, which is a legitimate model input and not a gap
        to be filled.
        """
        rows = self._by_series.get(series_id, [])
        best: Optional[Vintage] = None
        # Ordering is explicit and three-deep: the most recent observation the
        # desk had, then its highest revision, then the latest publication of
        # that revision. The third key is not decoration — a backlog replay can
        # publish two vintages for the same observation date at the same
        # revision, and without a tie-break the winner depends on insertion
        # order, which is not a property of the data.
        for v in rows:
            if not v.known_by(when):
                break                       # sorted by publication; nothing later qualifies
            key = (v.observation_date, v.revision, v.published_utc)
            if best is None or key > (best.observation_date, best.revision,
                                      best.published_utc):
                best = v
        if best is None:
            return UNAVAILABLE, f"{series_id}: UNAVAILABLE (nothing published by {when:%Y-%m-%d})"
        age_d = (when.date() - date.fromisoformat(best.observation_date)).days
        return best.value, (f"{series_id}: obs {best.observation_date} "
                            f"pub {best.published_utc[:10]} age {age_d}d rev {best.revision}")

    def frame_as_of(self, series_ids: Sequence[str], when: datetime) -> dict:
        """A point-in-time feature row. Missing series stay missing."""
        out: dict[str, Any] = {"as_of": when.isoformat()}
        for s in series_ids:
            val, prov = self.as_of(s, when)
            out[s] = val
            out[f"{s}__provenance"] = prov
        out["n_unavailable"] = sum(1 for s in series_ids if out[s] is UNAVAILABLE)
        return out

    # -- the test that matters -------------------------------------------
    def leakage_test(self, series_ids: Sequence[str],
                     decision_times: Sequence[datetime]) -> tuple[bool, list[str]]:
        """Prove no returned value was published after the decision it informed.

        Cheap, and it is the difference between believing the pipeline is causal
        and knowing it. Run it on every feature build, not once.
        """
        problems: list[str] = []
        for when in decision_times:
            for s in series_ids:
                rows = self._by_series.get(s, [])
                val, prov = self.as_of(s, when)
                if val is UNAVAILABLE:
                    continue
                # locate the row that produced it and verify its publication
                used = [v for v in rows if v.value == val and v.known_by(when)]
                if not used:
                    problems.append(f"{s} at {when:%Y-%m-%d}: returned a value with "
                                    f"no qualifying published vintage")
                    continue
                latest_pub = max(datetime.fromisoformat(v.published_utc) for v in used)
                if latest_pub > when:
                    problems.append(f"{s} at {when:%Y-%m-%d}: used a value published "
                                    f"{latest_pub:%Y-%m-%d} — FUTURE DATA")
        return (not problems), problems

    def coverage(self, series_id: str) -> dict:
        rows = self._by_series.get(series_id, [])
        if not rows:
            return {"series": series_id, "vintages": 0}
        lags = [(datetime.fromisoformat(v.published_utc).date()
                 - date.fromisoformat(v.observation_date)).days for v in rows]
        revs = sum(1 for v in rows if v.revision > 0)
        return {"series": series_id, "vintages": len(rows),
                "distinct_observations": len({v.observation_date for v in rows}),
                "revisions": revs,
                "publication_lag_days_median": statistics.median(lags),
                "publication_lag_days_max": max(lags),
                "lag_is_constant": len(set(lags)) == 1,
                "first_obs": min(v.observation_date for v in rows),
                "last_obs": max(v.observation_date for v in rows)}


# --------------------------------------------------------------------------
# Capacity — how many features may honestly be fitted
# --------------------------------------------------------------------------

@dataclass
class CapacityVerdict:
    effective_observations: float
    n_features: int
    obs_per_parameter: float
    verdict: str
    detail: str

    def render(self) -> str:
        return (f"  ESS {self.effective_observations:.0f} / {self.n_features} features "
                f"= {self.obs_per_parameter:.1f} obs per parameter — {self.verdict}\n"
                f"  {self.detail}")


def capacity_check(effective_observations: float, n_features: int,
                   min_obs_per_param: float = 20.0) -> CapacityVerdict:
    """Refuse to fit more parameters than the sample can support.

    The row count of a macro panel is not its information content. Gold has
    traded for eight years in this sample and that is roughly 2,090 effective
    daily observations, 433 weekly and 80 monthly — a weekly-published series
    such as CFTC positioning carries 433, no matter how many 30-minute rows it
    is broadcast across. Fitting fifty features against that is under nine
    observations per parameter, which fits noise reliably and out-of-sample
    never.

    `min_obs_per_param` is a declared modelling standard, not a market claim. It
    is stated here rather than buried so it can be argued with.
    """
    opp = effective_observations / max(n_features, 1)
    if opp >= min_obs_per_param:
        v, d = "OK", "the sample can support this many parameters"
    elif opp >= min_obs_per_param / 2:
        v, d = "MARGINAL", ("expect unstable coefficients; require walk-forward "
                            "stability before believing any of them")
    else:
        v, d = "REFUSE", (f"under {min_obs_per_param/2:.0f} observations per "
                          f"parameter — this will fit noise and will not "
                          f"reproduce out of sample")
    return CapacityVerdict(effective_observations, n_features, round(opp, 2), v, d)


def broadcast_warning(feature_frequency_days: float,
                      prediction_horizon_minutes: float) -> Optional[str]:
    """Flag a feature/horizon timescale mismatch before it becomes a probability.

    A daily feature is CONSTANT across every intraday bar of its day. Asking a
    model built on daily inputs for a 30-minute probability produces a number
    that is identical for all 48 half-hours and is then read as though it
    described each one. The apparent accuracy comes from daily autocorrelation;
    the effective sample is the number of DAYS, not the number of bars.
    """
    horizon_days = prediction_horizon_minutes / (60 * 24)
    if feature_frequency_days <= horizon_days * 2:
        return None
    ratio = feature_frequency_days / max(horizon_days, 1e-9)
    return (f"TIMESCALE MISMATCH: features update every {feature_frequency_days:g} "
            f"day(s) but the prediction horizon is {prediction_horizon_minutes:g} "
            f"minute(s) — {ratio:.0f}x finer. The inputs are constant across "
            f"~{ratio:.0f} consecutive predictions, so the model is forecasting the "
            f"slower period and being read as if it forecast the faster one. "
            f"Effective sample is the number of feature updates, not the number "
            f"of bars.")
