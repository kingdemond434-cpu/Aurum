"""Has this sleeve's edge decayed, or is it just having a bad month?

THE GAP THIS FILLS

`research/promoter.py` retires a sleeve on three triggers — rolling-20
expectancy at or below zero, forward drawdown past a floor, or expectancy below
a minimum after fifty trades. Useful, and two things are missing.

First, its own docstring: "The armed gold book is NOT managed here (hunt5
authority, armed by human)." The three sleeves carrying the desk's actual
capital have NO decay monitoring at all. Everything that can be retired
automatically is a challenger; everything that trades real money is watched by a
person who has to remember to look.

Second, retirement is a CLIFF, not a measurement. A sleeve is fine until the
moment it is dead. There is no reading that says "this edge has halved and the
evidence for that is now stronger than the evidence against", which is the
statement you would want two hundred trades before the kill trigger fires.

THE DISTINCTION EVERYTHING HERE TURNS ON

A drawdown inside the expected distribution is NOT decay. A book with +0.16R
expectancy and unit variance produces losing months constantly — that is what
the distribution says it does — and a monitor that fires on them retires good
sleeves at exactly the rate the market hands out bad luck. So the question is
never "is it losing" but "is the recent record less consistent with the
validated edge than with a degraded one", and that is a sequential likelihood
question rather than a threshold on a rolling mean.

CUSUM is the tool. It accumulates the log-likelihood ratio of "edge halved"
against "edge intact" trade by trade, and crosses a threshold when the evidence
genuinely piles up. It is optimal for detecting a persistent shift and it does
not fire on a single bad week, which is precisely the failure mode of a rolling
average with a threshold under it.

THE UNCOMFORTABLE ARITHMETIC, REPORTED RATHER THAN HIDDEN

Detection is slow when the edge is thin. `detection_latency` computes how many
trades it takes on average to notice a halving, and for a +0.16R book with unit
variance the answer is in the hundreds. That is not a defect of the monitor —
it is the information content of the data, and no method beats it. What it
means operationally is that a thin edge cannot be protected by monitoring
alone: by the time decay is provable, it has been paid for. The remedies are
diversification and position sizing, both of which act before detection, and
the monitor exists to catch the large breaks rather than the slow bleeds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

DECAY_VERSION = "decay-2026-08-18-a"

#: Log-likelihood units the CUSUM must accumulate before declaring a break.
#: 4.6 is roughly a 100:1 likelihood ratio — chosen because retiring a live
#: sleeve is expensive and irreversible in evidence terms: the trades it would
#: have taken are never observed, so a false retirement cannot be discovered.
CUSUM_THRESHOLD = 4.6

#: The alternative the monitor tests against. Half the validated edge, not zero:
#: an edge that has gone to exactly zero is a special case, and a monitor tuned
#: to detect only total collapse misses every partial decay on the way there.
DECAY_FRACTION = 0.5

#: Trades before any verdict. Below this the CUSUM is dominated by whichever
#: side the first few trades happened to land on.
MIN_TRADES = 40


@dataclass
class DecayState:
    """One sleeve's standing, from its forward record against its warrant."""
    sleeve: str
    n: int
    baseline_exp_r: float
    recent_exp_r: float
    cusum: float
    peak_cusum: float
    status: str                  # INTACT | WATCH | DECAYED | INSUFFICIENT
    trades_since_break: Optional[int]
    why: str

    @property
    def decayed(self) -> bool:
        return self.status == "DECAYED"

    def render(self) -> str:
        return "\n".join([
            f"{self.sleeve}  [{self.status}]",
            f"  n                  {self.n}",
            f"  validated exp      {self.baseline_exp_r:+.3f}R",
            f"  forward exp        {self.recent_exp_r:+.3f}R",
            f"  cusum / threshold  {self.cusum:.2f} / {CUSUM_THRESHOLD}",
            f"  {self.why}",
        ])


def cusum_decay(r_multiples: Sequence[float], baseline_exp_r: float,
                sd: Optional[float] = None,
                decay_fraction: float = DECAY_FRACTION,
                threshold: float = CUSUM_THRESHOLD) -> tuple:
    """Sequential likelihood ratio of "edge decayed" vs "edge intact".

    Per trade the log-likelihood ratio under two normals with the same variance
    and means mu0 (validated) and mu1 (decayed) is

        (mu1 - mu0) * (r - (mu0 + mu1) / 2) / sd^2

    accumulated and floored at zero, so a run of good trades resets the evidence
    rather than banking credit against a future break. Returns
    `(cusum_path, peak, first_crossing_index)`.
    """
    r = [float(x) for x in r_multiples if x == x]
    if not r:
        return [], 0.0, None
    s = sd if sd and sd > 0 else (
        (sum((x - sum(r) / len(r)) ** 2 for x in r) / max(len(r) - 1, 1)) ** 0.5)
    if s <= 0:
        s = 1.0
    mu0 = baseline_exp_r
    mu1 = baseline_exp_r * decay_fraction
    k = (mu1 - mu0) / (s * s)
    mid = (mu0 + mu1) / 2.0
    path, acc, peak, first = [], 0.0, 0.0, None
    for i, x in enumerate(r):
        acc = max(0.0, acc + k * (x - mid))
        path.append(acc)
        peak = max(peak, acc)
        if first is None and acc >= threshold:
            first = i
    return path, peak, first


def detection_latency(baseline_exp_r: float, sd: float = 1.0,
                      decay_fraction: float = DECAY_FRACTION,
                      threshold: float = CUSUM_THRESHOLD) -> Optional[float]:
    """Average trades to detect a halving, once it has happened.

    THE NUMBER THAT DECIDES WHETHER MONITORING IS A STRATEGY. Average run length
    for a CUSUM under the alternative is approximately threshold divided by the
    per-trade Kullback-Leibler divergence between the two hypotheses, which for
    equal-variance normals is (mu0 - mu1)^2 / (2 sd^2).

    For a thin edge this is large, and that is the information content of the
    data rather than a shortcoming of the method. A +0.16R book takes hundreds
    of trades to prove a halving — by which time it has been paid for.
    """
    d = baseline_exp_r * (1 - decay_fraction)
    if d == 0 or sd <= 0:
        return None
    kl = (d * d) / (2 * sd * sd)
    return threshold / kl if kl > 0 else None


def assess(sleeve: str, r_multiples: Sequence[float], baseline_exp_r: float,
           sd: Optional[float] = None, min_trades: int = MIN_TRADES) -> DecayState:
    """One sleeve's decay standing. Works on armed and promoted alike.

    `baseline_exp_r` is the expectancy the sleeve was VALIDATED at, not its own
    running mean. Comparing a sleeve to its own recent average asks whether it
    changed recently, which every series does; comparing it to its warrant asks
    whether it still deserves the authority it was given.
    """
    r = [float(x) for x in r_multiples if x == x]
    if len(r) < min_trades:
        return DecayState(sleeve, len(r), baseline_exp_r,
                          sum(r) / len(r) if r else 0.0, 0.0, 0.0,
                          "INSUFFICIENT", None,
                          f"{len(r)} forward trades, {min_trades} before a decay "
                          f"verdict means anything. Not the same as INTACT — this "
                          f"sleeve is unmonitored, not proven healthy.")
    path, peak, first = cusum_decay(r, baseline_exp_r, sd)
    cur = path[-1] if path else 0.0
    recent = sum(r) / len(r)
    if first is not None:
        since = len(r) - first
        return DecayState(sleeve, len(r), baseline_exp_r, recent, cur, peak,
                          "DECAYED", since,
                          f"the evidence for a halved edge crossed {CUSUM_THRESHOLD} "
                          f"at trade {first + 1} and the sleeve has taken {since} "
                          f"more since. Forward {recent:+.3f}R against a validated "
                          f"{baseline_exp_r:+.3f}R. This is not a bad month — it is "
                          f"a persistent shift the record now supports.")
    if cur >= threshold_watch(CUSUM_THRESHOLD):
        return DecayState(sleeve, len(r), baseline_exp_r, recent, cur, peak,
                          "WATCH", None,
                          f"cusum {cur:.2f} is over halfway to the break threshold. "
                          f"Not yet evidence of decay, and the point at which a "
                          f"person should be looking rather than being told.")
    return DecayState(sleeve, len(r), baseline_exp_r, recent, cur, peak,
                      "INTACT", None,
                      f"forward {recent:+.3f}R against a validated "
                      f"{baseline_exp_r:+.3f}R; the record is more consistent with "
                      f"the edge intact than halved. A drawdown inside the "
                      f"expected distribution is not decay.")


def threshold_watch(threshold: float = CUSUM_THRESHOLD) -> float:
    return threshold / 2.0


# ------------------------------------------------------------ the replacement

@dataclass
class BookHealth:
    """The whole book's standing, and whether the bench can cover it."""
    states: tuple
    ready_replacements: int
    why: str

    @property
    def decayed(self) -> tuple:
        return tuple(s for s in self.states if s.decayed)

    @property
    def unmonitored(self) -> tuple:
        return tuple(s for s in self.states if s.status == "INSUFFICIENT")

    def render(self) -> str:
        lines = [f"BOOK HEALTH  ({DECAY_VERSION})"]
        for s in self.states:
            lines.append(f"  {s.sleeve:<26}{s.status:<14}"
                         f"cusum {s.cusum:>5.2f}  n={s.n}")
        lines += ["", f"  {self.why}"]
        return "\n".join(lines)


def book_health(states: Sequence[DecayState], ready_replacements: int = 0,
                min_sleeves: int = 3) -> BookHealth:
    """Decay is only survivable if something is queued to take the slot.

    A monitor that retires sleeves without a bench does not protect the book, it
    shrinks it — and a shrinking book concentrates the remaining risk in fewer
    bets exactly when the evidence says the edges are degrading. The number of
    shadow sleeves ready to promote is therefore part of the health reading, not
    a separate concern.
    """
    st = list(states)
    dead = [s for s in st if s.decayed]
    live = [s for s in st if not s.decayed and s.status != "INSUFFICIENT"]
    unmon = [s for s in st if s.status == "INSUFFICIENT"]

    parts = []
    if unmon:
        parts.append(f"{len(unmon)} sleeve(s) have too little forward evidence to "
                     f"monitor at all: {', '.join(s.sleeve for s in unmon)}. "
                     f"Unmonitored is not healthy.")
    if not dead:
        parts.append(f"{len(live)} sleeve(s) reading INTACT or WATCH.")
    else:
        parts.append(f"{len(dead)} sleeve(s) DECAYED: "
                     f"{', '.join(s.sleeve for s in dead)}.")
        remaining = len(live)
        if remaining + ready_replacements < min_sleeves:
            parts.append(
                f"Retiring them leaves {remaining} live with "
                f"{ready_replacements} ready to promote, against a floor of "
                f"{min_sleeves}. A monitor that retires without a bench does not "
                f"protect the book, it shrinks it — and concentrates the "
                f"remaining risk in fewer bets exactly when the evidence says "
                f"edges are degrading. Widen the shadow set before retiring.")
        else:
            parts.append(f"{ready_replacements} replacement(s) ready; the slot "
                         f"can be refilled.")
    return BookHealth(tuple(st), ready_replacements, " ".join(parts))
