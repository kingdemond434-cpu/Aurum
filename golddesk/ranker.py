"""Which recorded feature actually predicts realised R — re-measured every day.

THE MEASUREMENT THAT ASKED FOR THIS, from the desk's own read_quality audit:

    SELECTION: taken trades resolve -0.14R while refusals reached +0.56R.

That is not a frequency fault and it is not a gate fault. The desk is not taking
too few trades, and the trades it refused were not refused by a bad rule — they
were simply never the ones it reached for. Out of a set that contained better, it
kept choosing worse. The missing organ is RANKING: something that puts the best
proposition at the front when risk is scarce, on evidence rather than on the
order the model happened to emit them in.

WHAT THIS IS NOT, and the distinction is the whole design

It is NOT a model that learns weights. Fourteen resolved trades cannot support a
fitted anything, and a ranker that re-fits itself nightly on fourteen rows is
overfitting on a schedule — it would look like learning, produce a different
ordering every morning, and every one of those orderings would be a fit to which
particular trades happened to land in the sample. That is worse than no ranking,
because it is noise with an audit trail.

So this measures instead. For every feature the ledger ALREADY records at signal
time, it asks one question with one answer: did trades in the top half of this
feature resolve better than trades in the bottom half, by more than it costs to
express the difference, and by more than chance explains? A feature that cannot
answer yes contributes NOTHING to ordering — not a small weight, nothing.

FOUR BARS, AND EVERY ONE OF THEM HAS TO BE CLEARED

  SAMPLE       MIN_RESOLVED resolved trades overall, MIN_PER_GROUP either side
               of the split. Below that the answer is UNMEASURED, which is a
               real answer (L1.28a) and the one that will be correct for weeks.

  SIGNIFICANCE a two-sided permutation test on the difference of means, then
               HOLM across every feature tested. The Holm step is not
               decoration: eight features re-tested daily will hand out a
               p < 0.05 by luck alone within a fortnight, and a ranker that
               believes it is exactly how a desk teaches itself a superstition.

  ECONOMICS    the difference must exceed the sample's own median cost_r. A
               reordering that moves expectancy by less than the spread it takes
               to express is not an edge, it is a rounding error with a
               direction. The floor is MEASURED from the ledger rather than
               chosen, so it tightens as costs rise.

  STABILITY    MIN_STREAK_DAYS consecutive measurement days qualifying with the
               SAME SIGN before the feature touches live ordering. This is NOT
               additional statistical evidence — the same trades are being
               re-tested, so consecutive days are nowhere near independent, and
               claiming otherwise would be the cheapest lie in the file. It is a
               hysteresis band: it stops a feature sitting on the boundary from
               switching the live ordering on and off every morning.

VOTES, NOT COEFFICIENTS

A qualifying feature is worth exactly +1 or -1: is this candidate on the side
that resolved better, or not. Integer, unweighted, capped by how many features
qualified. A tuned coefficient would claim a precision the sample cannot carry;
a vote claims only what was tested — which side was better — and that is exactly
what was tested.

IT RANKS. IT NEVER REFUSES.

`score` returns an integer that breaks ties in `universe._sort_key` and nothing
else. It cannot make a candidate ineligible, cannot lower a size, cannot stop a
signal. The standing order is maximum frequency, and the measurement says the
fault is ordering rather than volume — so when the budget does not bind, every
one of these votes changes nothing at all, by construction. With no artifact, or
an empty one, the sort key is byte-identical to what it was before this file
existed.

WHAT IT MEASURES BUT CANNOT YET USE

Some recorded features (the evidence tier, the stop's size against the current
bar, edge_r) are computed downstream of selection and are simply not available
at the moment candidates are ordered. Those are measured anyway and reported
under MEASURED BUT UNWIRED, because a proven predictor that nothing reads is a
III.16 defect and must be visible as one rather than quietly absent.
"""

from __future__ import annotations

import json
import logging
import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

RANKER_VERSION = "rank-2026-08-29-a"

#: Resolved trades before ANY feature may be tested. Matches
#: read_quality.MIN_FOR_EDGE so the two modules cannot disagree about when the
#: desk's own record becomes readable.
MIN_RESOLVED = 30

#: Trades either side of the split. A ten-a-side comparison is still thin; it is
#: the point below which the median split is describing two handfuls.
MIN_PER_GROUP = 10

#: Family-wise error rate for the Holm step, across all features tested on a day.
ALPHA = 0.05

#: Permutation draws. Deterministic seed: the same ledger must yield the same
#: verdict on every box and in every re-run, or "it qualified this morning"
#: becomes unfalsifiable.
N_PERM = 10_000
PERM_SEED = 20260829

#: Absolute floor under the measured cost floor, in R. Costs can be recorded as
#: zero on a mechanism the cost model has no entry for, and a zero economic bar
#: would let a two-hundredths-of-an-R difference qualify.
MIN_EFFECT_FLOOR_R = 0.10

#: Consecutive measurement days a feature must qualify, with the same sign,
#: before it is allowed to move live ordering. Hysteresis, not evidence.
MIN_STREAK_DAYS = 3

#: Where the daily verdict is published. Tracked in git like the rest of state/,
#: because the ordering the desk used on a given day is evidence.
ARTIFACT = Path(__file__).resolve().parent.parent / "state" / "ranker.json"


# --------------------------------------------------------------------------
# The features, and where each one comes from
# --------------------------------------------------------------------------

def _dig(d: Any, *path: str) -> Optional[float]:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur)


@dataclass(frozen=True)
class Feature:
    """One recorded number, and whether ordering can actually see it.

    `scoreable` is the honest half. A feature computed after selection can be
    measured from the ledger but cannot rank anything, and pretending otherwise
    would put a weight on a value that is always None at the moment it matters.
    """
    name: str
    read: Callable[[dict], Optional[float]]
    scoreable: bool
    why: str


FEATURES: tuple[Feature, ...] = (
    Feature("evidence_net", lambda d: _dig(d, "evidence_balance", "net"), True,
            "measured evidence for the direction minus measured evidence against"),
    Feature("confidence", lambda d: _dig(d, "analyst_read", "confidence"), True,
            "the analyst's own stated confidence, 1-5"),
    Feature("rr_tp2", lambda d: _dig(d, "rr_tp2"), True,
            "reward-to-risk at the second objective — TODAY'S declared tiebreak"),
    Feature("cost_r", lambda d: _dig(d, "cost_r"), True,
            "modelled round-trip cost as a fraction of risk"),
    Feature("tier_rank", lambda d: _dig(d, "evidence_tier", "rank"), False,
            "the evidence tier shown to the operator — computed after selection"),
    Feature("stop_in_range", lambda d: _dig(d, "stop_regime", "stop_in_range"), False,
            "stop distance against the CURRENT bar's range — needs bars"),
    Feature("stop_in_atr", lambda d: _dig(d, "stop_regime", "stop_in_atr"), False,
            "stop distance in trailing ATR — needs bars"),
    Feature("edge_r", lambda d: _dig(d, "edge_r"), False,
            "the desk's own pre-trade edge estimate — computed after selection"),
)

SCOREABLE = tuple(f.name for f in FEATURES if f.scoreable)


# --------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------

def _perm_p(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sided permutation p on the difference of means. Deterministic.

    Permutation rather than a t-test because R distributions are bounded below
    at roughly -1 and open above, which is about as far from normal as a sample
    gets, and because it needs no special functions the desk does not have.
    """
    obs = abs(statistics.fmean(a) - statistics.fmean(b))
    pool = list(a) + list(b)
    na = len(a)
    rng = random.Random(PERM_SEED)
    hits = 0
    for _ in range(N_PERM):
        rng.shuffle(pool)
        if abs(statistics.fmean(pool[:na]) - statistics.fmean(pool[na:])) >= obs - 1e-12:
            hits += 1
    # (hits+1)/(n+1): the observed arrangement is itself one of the permutations,
    # so a p of exactly zero is not available and should not be reportable.
    return (hits + 1) / (N_PERM + 1)


@dataclass
class FeatureResult:
    name: str
    scoreable: bool
    n: int = 0
    n_low: int = 0
    n_high: int = 0
    split: Optional[float] = None
    mean_low: Optional[float] = None
    mean_high: Optional[float] = None
    diff: Optional[float] = None          # high minus low, in R
    p: Optional[float] = None
    sign: int = 0                          # +1 higher resolved better, -1 lower
    tested: bool = False
    holm_ok: bool = False
    economic_ok: bool = False
    reason: str = ""

    @property
    def qualified(self) -> bool:
        return self.tested and self.holm_ok and self.economic_ok

    def to_dict(self) -> dict:
        return {"name": self.name, "scoreable": self.scoreable, "n": self.n,
                "n_low": self.n_low, "n_high": self.n_high, "split": self.split,
                "mean_low": self.mean_low, "mean_high": self.mean_high,
                "diff": self.diff, "p": self.p, "sign": self.sign,
                "tested": self.tested, "holm_ok": self.holm_ok,
                "economic_ok": self.economic_ok, "qualified": self.qualified,
                "reason": self.reason}


@dataclass
class Report:
    n_resolved: int
    effect_floor_r: float
    results: list[FeatureResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.n_resolved < MIN_RESOLVED:
            return "UNMEASURED"
        return "MEASURED"

    def get(self, name: str) -> Optional[FeatureResult]:
        for r in self.results:
            if r.name == name:
                return r
        return None


def _pairs(rows: Sequence[dict]) -> list[tuple[dict, float]]:
    """(signal decision, realised R) for every trade whose path was observed.

    Quarantined rows are dropped for the same reason cohort_stats drops them: an
    unobserved path carries zeros that are not measurements, and here they would
    be attributed to whichever feature bucket the signal happened to fall in.
    """
    sig: dict[str, dict] = {}
    for r in rows:
        if r.get("kind") == "SIGNAL":
            sig[str(r.get("t0"))] = r.get("decision") or {}
    out: list[tuple[dict, float]] = []
    for c in rows:
        if c.get("kind") != "TRADE_CLOSED":
            continue
        if c.get("evidence_valid") is False:
            continue
        r_mult = c.get("realised_r")
        if not isinstance(r_mult, (int, float)) or isinstance(r_mult, bool):
            continue
        d = sig.get(str(c.get("entry_t0")))
        if d is None:
            continue
        out.append((d, float(r_mult)))
    return out


def measure(rows: Sequence[dict]) -> Report:
    """Test every recorded feature against realised R. Pure; reads, decides nothing.

    The economic floor is taken from the sample itself — the median modelled
    cost of the trades being compared — so the bar a difference has to clear is
    the desk's own cost of expressing it rather than a number somebody liked.
    """
    pairs = _pairs(rows)
    costs = [c for c in (_dig(d, "cost_r") for d, _ in pairs) if c is not None]
    floor = max(MIN_EFFECT_FLOOR_R,
                round(statistics.median(costs), 4) if costs else 0.0)
    rep = Report(n_resolved=len(pairs), effect_floor_r=floor)

    for feat in FEATURES:
        res = FeatureResult(feat.name, feat.scoreable)
        rep.results.append(res)
        vals = [(feat.read(d), r) for d, r in pairs]
        usable = [(v, r) for v, r in vals if v is not None]
        res.n = len(usable)
        if len(pairs) < MIN_RESOLVED:
            res.reason = (f"{len(pairs)} resolved trade(s), under {MIN_RESOLVED} "
                          f"— UNMEASURED, not neutral")
            continue
        if res.n < MIN_RESOLVED:
            res.reason = (f"only {res.n} of {len(pairs)} resolved trades carry this "
                          f"feature; under {MIN_RESOLVED}")
            continue
        split = statistics.median(v for v, _ in usable)
        low = [r for v, r in usable if v < split]
        high = [r for v, r in usable if v >= split]
        res.split, res.n_low, res.n_high = round(split, 4), len(low), len(high)
        if len(low) < MIN_PER_GROUP or len(high) < MIN_PER_GROUP:
            res.reason = (f"median split is {len(low)}/{len(high)}, under "
                          f"{MIN_PER_GROUP} a side — ties, not evidence")
            continue

        res.mean_low = round(statistics.fmean(low), 4)
        res.mean_high = round(statistics.fmean(high), 4)
        res.diff = round(res.mean_high - res.mean_low, 4)
        res.sign = 1 if res.diff > 0 else -1
        res.p = round(_perm_p(low, high), 5)
        res.tested = True
        res.economic_ok = abs(res.diff) >= floor
        if not res.economic_ok:
            res.reason = (f"difference {res.diff:+.3f}R does not clear the "
                          f"{floor:.3f}R cost of expressing it")

    # HOLM, across everything actually tested today. Sorted ascending, each p
    # compared against alpha/(k-i); the first failure stops the ladder, because
    # Holm's guarantee depends on that and a "keep checking" variant silently
    # becomes uncorrected testing again.
    tested = sorted((r for r in rep.results if r.tested),
                    key=lambda r: r.p if r.p is not None else 1.0)
    k = len(tested)
    for i, r in enumerate(tested):
        if r.p is not None and r.p <= ALPHA / (k - i):
            r.holm_ok = True
        else:
            for rest in tested[i:]:
                if not rest.reason:
                    rest.reason = (f"p={rest.p} does not clear Holm at alpha "
                                   f"{ALPHA} over {k} feature(s) tested today")
            break
    return rep


# --------------------------------------------------------------------------
# The daily artifact — measurement plus the stability the measurement lacks
# --------------------------------------------------------------------------

def advance(prev: Optional[dict], rep: Report, day: str) -> dict:
    """Fold today's measurement into the running artifact.

    Idempotent within a day: re-running the cycle re-measures and rewrites, but
    it does not advance a streak twice. A `--force` re-run must not be able to
    promote a feature into live ordering three times before lunch.
    """
    prev = prev if isinstance(prev, dict) else {}
    prev_feats = prev.get("features") or {}
    same_day = prev.get("day") == day

    feats: dict[str, dict] = {}
    for res in rep.results:
        d = res.to_dict()
        before = prev_feats.get(res.name) or {}
        # THE BASE IS THE STATE AT THE END OF THE PREVIOUS DAY, and on a same-day
        # re-run that is what the artifact's own `prior_*` fields hold rather
        # than the streak this morning's run already wrote. Carrying the base
        # explicitly is what makes `--force` idempotent: without it a feature
        # could be walked from zero to USED by three re-runs before lunch.
        if same_day:
            base = int(before.get("prior_streak") or 0)
            base_sign = int(before.get("prior_sign") or 0)
        else:
            base = int(before.get("streak") or 0)
            base_sign = int(before.get("sign") or 0)
        d["prior_streak"], d["prior_sign"] = base, base_sign
        if not res.qualified:
            streak = 0
        elif base and base_sign == res.sign:
            streak = base + 1
        else:
            # A SIGN FLIP RESETS TO ONE, never continues. A feature that
            # predicted one way last week and the other way this week has
            # not accumulated three days of anything.
            streak = 1
        d["streak"] = streak
        d["used"] = bool(res.qualified and res.scoreable and streak >= MIN_STREAK_DAYS)
        d["why"] = next((f.why for f in FEATURES if f.name == res.name), "")
        feats[res.name] = d

    used = [n for n, d in feats.items() if d["used"]]
    unwired = [n for n, d in feats.items()
               if d["qualified"] and not d["scoreable"]
               and d["streak"] >= MIN_STREAK_DAYS]
    return {"version": RANKER_VERSION, "day": day, "n_resolved": rep.n_resolved,
            "verdict": rep.verdict, "effect_floor_r": rep.effect_floor_r,
            "alpha": ALPHA, "min_resolved": MIN_RESOLVED,
            "min_streak_days": MIN_STREAK_DAYS,
            "features": feats, "used": sorted(used), "unwired": sorted(unwired)}


# EVERY PATH BELOW RESOLVES AT CALL TIME, never as `path=ARTIFACT` in the
# signature. A module constant captured in a default argument is frozen at
# import, so the parameter only LOOKS configurable — a test pointing the desk at
# a temp directory, or a relocated desk, would silently keep writing to the
# original. aurum_cycle._rows carries the same note for the same reason.

def read_artifact(path: Optional[Path] = None) -> Optional[dict]:
    try:
        return json.loads(Path(path or ARTIFACT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def publish(art: dict, path: Optional[Path] = None) -> None:
    p = Path(path or ARTIFACT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(art, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render(art: dict) -> str:
    feats = art.get("features") or {}
    used = art.get("used") or []
    lines = [f"RANKING ({art.get('version')}) — {art.get('n_resolved', 0)} resolved "
             f"trade(s), verdict {art.get('verdict')}"]
    if art.get("verdict") == "UNMEASURED":
        lines.append(f"  Under {MIN_RESOLVED} resolved trades nothing here may touch "
                     f"ordering. The desk's measured fault is SELECTION, and a "
                     f"ranker fitted on this sample would be that fault with "
                     f"better paperwork. Ordering is unchanged.")
    for name in sorted(feats):
        d = feats[name]
        tag = "USED " if d.get("used") else ("QUAL " if d.get("qualified") else "     ")
        if d.get("tested"):
            body = (f"n={d['n']} split {d['split']} "
                    f"low {d['mean_low']:+.3f}R vs high {d['mean_high']:+.3f}R "
                    f"(diff {d['diff']:+.3f}R, p={d['p']}) streak {d['streak']}")
        else:
            body = d.get("reason") or "not tested"
        lines.append(f"  [{tag}] {name:<15} {body}")
        if d.get("tested") and not d.get("qualified") and d.get("reason"):
            lines.append(f"           {d['reason']}")
    if used:
        lines.append(f"  ORDERING USES: {', '.join(used)} — each worth one vote, and "
                     f"votes only ever break a tie when the budget binds.")
    else:
        lines.append("  ORDERING USES: nothing. No feature has cleared sample, Holm, "
                     "cost and stability, so the sort key is exactly what it was.")
    if art.get("unwired"):
        lines.append(f"  MEASURED BUT UNWIRED (III.16): {', '.join(art['unwired'])} "
                     f"predict(s) realised R and is computed AFTER selection, so "
                     f"ordering cannot see it. That is a defect to close, not a "
                     f"result to enjoy.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The live side — scoring a candidate
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Used:
    name: str
    split: float
    sign: int


@dataclass(frozen=True)
class Ranking:
    """What the live sort key is allowed to know. Empty by default."""
    version: str = ""
    day: str = ""
    used: tuple[Used, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.used)

    def score(self, feats: Mapping[str, Optional[float]]) -> int:
        """Votes: +1 per used feature on the side that resolved better.

        A feature whose value is missing on this candidate contributes nothing —
        not a zero-ish guess, not the split value, nothing. Absence is absence.
        """
        total = 0
        for u in self.used:
            v = feats.get(u.name)
            if v is None:
                continue
            high = float(v) >= u.split
            better = high if u.sign > 0 else not high
            total += 1 if better else -1
        return total

    def render(self) -> str:
        if not self.used:
            return "RANKING: no measured feature — ordering unchanged."
        return ("RANKING " + self.version + ": " +
                ", ".join(f"{u.name}{'↑' if u.sign > 0 else '↓'}@{u.split}"
                          for u in self.used))


EMPTY = Ranking()


def from_artifact(art: Optional[dict]) -> Ranking:
    """Build the live ranking from a published artifact. Never raises.

    Anything malformed degrades to EMPTY, which restores the pre-ranker sort key
    exactly. A corrupt artifact must not be able to invent an ordering.
    """
    if not isinstance(art, dict):
        return EMPTY
    feats = art.get("features")
    if not isinstance(feats, dict):
        return EMPTY
    used: list[Used] = []
    for name, d in sorted(feats.items()):
        if not isinstance(d, dict) or not d.get("used"):
            continue
        if name not in SCOREABLE:
            continue                      # published as used but not computable
        split, sign = d.get("split"), d.get("sign")
        if not isinstance(split, (int, float)) or sign not in (1, -1):
            continue
        used.append(Used(name, float(split), int(sign)))
    return Ranking(str(art.get("version") or ""), str(art.get("day") or ""),
                   tuple(used))


_cache: dict[str, Any] = {"mtime": None, "path": None, "ranking": EMPTY}


def load(path: Optional[Path] = None) -> Ranking:
    """The published ranking, re-read when the file changes. Never raises.

    Re-read on mtime rather than cached for the process lifetime: the desk runs
    for days at a time and the cycle republishes nightly, so a process-lifetime
    cache would mean the ordering in use was whatever was on disk at boot.
    """
    p = Path(path or ARTIFACT)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, path=str(p), ranking=EMPTY)
        return EMPTY
    if _cache["path"] == str(p) and _cache["mtime"] == mtime:
        return _cache["ranking"]
    r = from_artifact(read_artifact(p))
    _cache.update(mtime=mtime, path=str(p), ranking=r)
    return r


def features_for(read: Any, compiled: Any, ctx: Any) -> dict[str, Optional[float]]:
    """The scoreable features of a candidate, in the SAME units as the ledger.

    Identical arithmetic to the ledger extractors above, or the measurement is
    about one thing and the ordering about another. `evidence_net` is None —
    never 0 — when the context was not measurable: contradiction.weigh returns
    an empty balance there, whose net is arithmetically zero and epistemically
    nothing.
    """
    out: dict[str, Optional[float]] = {"evidence_net": None, "confidence": None,
                                       "rr_tp2": None, "cost_r": None}
    conf = getattr(read, "confidence", None)
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = float(conf)
    if compiled is not None:
        for k in ("rr_tp2", "cost_r"):
            v = getattr(compiled, k, None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
    try:
        from .contradiction import weigh
        direction = getattr(compiled, "direction", None) or getattr(read, "direction", "")
        bal = weigh(str(direction), ctx)
        if bal.items:
            out["evidence_net"] = float(bal.net)
    except Exception as e:                                        # noqa: BLE001
        log.debug("evidence balance unavailable for ranking: %s", e)
    return out
