"""Where does this provider actually enter? Measured against a matched null.

The reconstruction so far says WHAT the machine does — equal-lot baskets, adds
at irregular spacing, confidence-tiered sizing. It does not say what triggers an
entry. That needs timestamps aligned to bars, and it needs one more thing that
is easy to leave out and fatal to leave out.

THE NULL IS THE ENTIRE TEST

"His entries cluster near fair value gaps" is not a finding if fair value gaps
are everywhere. Gold at M5 produces imbalances constantly; a strategy entering
at random times would also land near one most of the time, and the analysis
would confirm any hypothesis it was handed. The same is true of sweeps, of prior
highs, of round numbers, and of round numbers especially.

So every feature is scored against a MATCHED null: random entry times drawn from
the same sessions, the same weekdays and the same hours as the real entries.
Matched on those, because a provider who only trades the London session and a
null that trades all day would differ on every feature for reasons that have
nothing to do with his trigger. The lift over that null is the finding; the raw
hit rate is not.

WHAT CANNOT BE CONCLUDED FROM THIS

That a trigger causes the entry. Clustering is compatible with him watching the
same levels the feature computes, and with both being downstream of something
else entirely — volatility, session opens, a news calendar. The output is a
ranked list of what his entries are NEAR, which is where a reconstruction
starts, not where it ends. `ablate()` in reverse.py is what turns a candidate
trigger into a testable strategy.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

CLASSIFIER_VERSION = "entrycls-2026-08-18-a"

#: Random draws behind the matched null. 200 gives a stable p-value at the
#: resolution that matters here and costs milliseconds.
N_NULL = 200

#: Entries needed before any feature verdict. Below this, lift is dominated by
#: whichever few entries happened to land near something.
MIN_ENTRIES = 30


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


#: Family-wise alpha across ALL features tested. Not per-feature: six features
#: at p<=0.05 each gives a ~26% chance that at least one fires on pure noise,
#: and this module's own test caught exactly that — a random-entry draw scored
#: "displacement" as significant. Testing six hypotheses and reporting each at
#: 0.05 is the multiplicity error the rest of this desk exists to prevent, and
#: it does not stop being one because the hypotheses are structural.
FAMILY_ALPHA = 0.05

#: Lift below this is not worth calling a finding whatever its p-value. On a
#: large sample a 1.05x lift can be significant and mean nothing operationally.
MIN_LIFT = 1.3


@dataclass
class FeatureHit:
    """One structural feature, and how much more often his entries touch it."""
    name: str
    hit_rate: float
    null_rate: float
    lift: float
    p_value: float
    n: int
    #: How many features were tested alongside this one. Carried on the object
    #: so significance cannot be read without the correction that goes with it.
    n_features: int = 1
    why: str = ""

    @property
    def alpha(self) -> float:
        """Bonferroni-corrected threshold. Conservative, and deliberately so:
        these features are positively correlated with each other, so the true
        family-wise rate is below the Bonferroni bound and this errs toward
        refusing a finding rather than manufacturing one."""
        return FAMILY_ALPHA / max(self.n_features, 1)

    @property
    def significant(self) -> bool:
        return self.p_value <= self.alpha and self.lift >= MIN_LIFT

    def render(self) -> str:
        mark = "***" if self.significant else "   "
        return (f"  {mark} {self.name:<22}{self.hit_rate:>7.1%} vs null "
                f"{self.null_rate:>6.1%}   lift {self.lift:>5.2f}x   "
                f"p={self.p_value:.3f} (need <={self.alpha:.4f})")


# ------------------------------------------------------------- the features
#
# Each takes (bars, index) and answers "is this bar at/near the feature". They
# are deliberately crude: the question is whether entries CLUSTER on something,
# and a precise definition of an FVG would trade one arbitrary choice for
# another while making the null harder to match.

def _atr(bars: Sequence[Bar], i: int, n: int = 14) -> float:
    lo = max(1, i - n + 1)
    trs = [max(b.high - b.low, abs(b.high - bars[k - 1].close),
               abs(b.low - bars[k - 1].close))
           for k, b in enumerate(bars[lo:i + 1], start=lo)]
    return statistics.mean(trs) if trs else 0.0


def f_fvg(bars: Sequence[Bar], i: int, look: int = 20) -> bool:
    """Price inside a recent three-bar imbalance."""
    if i < 3:
        return False
    p = bars[i].open
    for k in range(max(2, i - look), i):
        if bars[k].low > bars[k - 2].high:            # bullish gap
            if bars[k - 2].high <= p <= bars[k].low:
                return True
        if bars[k].high < bars[k - 2].low:            # bearish gap
            if bars[k].high <= p <= bars[k - 2].low:
                return True
    return False


def f_sweep(bars: Sequence[Bar], i: int, look: int = 20) -> bool:
    """The previous bar took out a recent extreme and closed back inside."""
    if i < look + 1:
        return False
    w = bars[i - look - 1:i - 1]
    if not w:
        return False
    hi, lo = max(b.high for b in w), min(b.low for b in w)
    prev = bars[i - 1]
    return ((prev.high > hi and prev.close < hi)
            or (prev.low < lo and prev.close > lo))


def f_prior_extreme(bars: Sequence[Bar], i: int, look: int = 20) -> bool:
    """Entry within a quarter-ATR of a recent high or low."""
    if i < look + 1:
        return False
    w = bars[i - look:i]
    a = _atr(bars, i)
    if a <= 0:
        return False
    p = bars[i].open
    return (abs(p - max(b.high for b in w)) < 0.25 * a
            or abs(p - min(b.low for b in w)) < 0.25 * a)


def f_bos(bars: Sequence[Bar], i: int, look: int = 20) -> bool:
    """The previous bar closed BEYOND a recent extreme — a break, not a sweep."""
    if i < look + 1:
        return False
    w = bars[i - look - 1:i - 1]
    if not w:
        return False
    prev = bars[i - 1]
    return (prev.close > max(b.high for b in w)
            or prev.close < min(b.low for b in w))


def f_round_number(bars: Sequence[Bar], i: int, step: float = 10.0) -> bool:
    """Within 0.5 of a 10-dollar level. THE CONTROL FEATURE.

    Included because it is the one everybody's entries hit — price spends a
    fixed fraction of its life near round numbers whatever the strategy. If this
    scores a lift comparable to the structural features, the whole analysis is
    measuring how often gold is near a round number and nothing else.
    """
    p = bars[i].open
    return abs(p - round(p / step) * step) < 0.5


def f_expansion(bars: Sequence[Bar], i: int) -> bool:
    """Previous bar's range above 1.5x the local ATR — displacement."""
    if i < 15:
        return False
    a = _atr(bars, i - 1)
    return a > 0 and (bars[i - 1].high - bars[i - 1].low) > 1.5 * a


FEATURES: dict = {
    "fvg": f_fvg,
    "liquidity_sweep": f_sweep,
    "prior_extreme": f_prior_extreme,
    "break_of_structure": f_bos,
    "displacement": f_expansion,
    "round_number(CONTROL)": f_round_number,
}


# ---------------------------------------------------------------- the harness

def _index_at(bars: Sequence[Bar], t: datetime) -> Optional[int]:
    """Index of the bar CONTAINING t. Binary-search on a sorted series."""
    lo, hi = 0, len(bars) - 1
    if not bars or t < bars[0].ts:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid].ts <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _matched_null(entries: Sequence[datetime], bars: Sequence[Bar],
                  seed: int) -> list:
    """Random times with the SAME weekday and hour distribution as the entries.

    Matching on session is what stops the null being a straw man. A provider who
    only trades London against a null that trades all day differs on every
    feature for reasons unrelated to his trigger, and every hypothesis would
    look confirmed.
    """
    rng = random.Random(seed)
    by_slot = {}
    for b in bars:
        by_slot.setdefault((b.ts.weekday(), b.ts.hour), []).append(b)
    out = []
    for e in entries:
        pool = by_slot.get((e.weekday(), e.hour))
        if pool:
            out.append(rng.choice(pool).ts)
    return out


def classify(entry_times: Sequence[datetime], bars: Sequence[Bar],
             features: Optional[dict] = None, n_null: int = N_NULL,
             seed: int = 0) -> list:
    """Score every feature by lift over a session-matched null.

    The p-value is a permutation p: the fraction of null draws whose hit rate
    reaches the observed one. No distributional assumption, and it handles the
    fact that these features are correlated with each other — which a
    chi-squared per feature would not.
    """
    feats = features or FEATURES
    bars = sorted(bars, key=lambda b: b.ts)
    idx = [i for i in (_index_at(bars, t) for t in entry_times) if i is not None]
    out: list = []
    if len(idx) < MIN_ENTRIES:
        return [FeatureHit(name, 0.0, 0.0, 0.0, 1.0, len(idx), len(feats),
                           f"{len(idx)} aligned entries, {MIN_ENTRIES} required. "
                           f"Lift below that is dominated by whichever few "
                           f"entries happened to land near something.")
                for name in feats]

    for name, fn in feats.items():
        hits = sum(1 for i in idx if fn(bars, i))
        rate = hits / len(idx)
        null_rates = []
        for s in range(n_null):
            nt = _matched_null(entry_times, bars, seed + s)
            ni = [i for i in (_index_at(bars, t) for t in nt) if i is not None]
            if ni:
                null_rates.append(sum(1 for i in ni if fn(bars, i)) / len(ni))
        if not null_rates:
            out.append(FeatureHit(name, rate, 0.0, 0.0, 1.0, len(idx), len(feats),
                                  "the null could not be constructed"))
            continue
        nr = statistics.mean(null_rates)
        p = (sum(1 for x in null_rates if x >= rate) + 1) / (len(null_rates) + 1)
        lift = rate / nr if nr > 0 else 0.0
        out.append(FeatureHit(name, rate, nr, lift, p, len(idx), len(feats)))
    return sorted(out, key=lambda h: -h.lift)


def report(hits: Sequence[FeatureHit]) -> str:
    lines = [f"ENTRY CLASSIFIER  ({CLASSIFIER_VERSION})",
             f"  {hits[0].n if hits else 0} entries aligned to bars",
             f"  {len(hits)} features tested; family-wise alpha "
             f"{FAMILY_ALPHA} corrected to "
             f"{(hits[0].alpha if hits else FAMILY_ALPHA):.4f} per feature. "
             f"Six at 0.05 each is a ~26% chance one fires on noise.", ""]
    lines += [h.render() for h in hits]
    ctrl = next((h for h in hits if "CONTROL" in h.name), None)
    sig = [h for h in hits if h.significant and "CONTROL" not in h.name]
    lines.append("")
    if ctrl and ctrl.significant:
        lines.append(
            f"  THE CONTROL FIRED (lift {ctrl.lift:.2f}x). Price spends a fixed "
            f"fraction of its life near round numbers whatever the strategy, so "
            f"any structural feature scoring near this is measuring the same "
            f"thing. Treat every lift below {ctrl.lift:.2f}x as noise.")
    if not sig:
        lines.append("  NO STRUCTURAL FEATURE CLEARS ITS MATCHED NULL. His "
                     "entries are not concentrated on any of these, or the "
                     "sample is too small to show it. That is a result: it "
                     "rules out the SMC trigger family as stated.")
    else:
        lines.append(f"  Clusters on: {', '.join(h.name for h in sig)}.")
        lines.append("  CLUSTERING IS NOT CAUSATION. He may watch the same "
                     "levels this computes, or both may be downstream of "
                     "volatility or a session open. This is where a "
                     "reconstruction starts — reverse.ablate() is what turns a "
                     "candidate trigger into a testable strategy.")
    return "\n".join(lines)
