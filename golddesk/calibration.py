"""Known-answer tests: worlds where the right result is known before the run.

WHY THE EXISTING TESTS WERE NOT ENOUGH

Seven material defects surfaced in a single session — gold charged three percent
of its spread for the life of the desk, a same-day lookahead join, an effective
trial count that silently fell back to the uncorrected number while printing as
though it had worked, a variance that put the Sharpe bar at 6.5, a fifty-percent
blend where a marginal weight belonged, "not applicable" read as zero and
blocking the first promotion, and equal weighting unable to see a duplicate.

Every one was found by looking hard at a specific thing. That is not a process,
it is luck plus attention, and it does not scale: the defects nobody thinks to
look at are exactly the ones that stay.

THE FIX IS A WORLD WHERE THE ANSWER IS KNOWN IN ADVANCE

Generate a price series with a KNOWN embedded edge and KNOWN costs, run the real
engine over it, and require the engine to return the number that was put in. No
hypothesis about what might be wrong is needed — any discrepancy between the
planted answer and the recovered one is a defect, whatever its cause.

The gold spread bug is the case in point. It survived every hand-written test
because every test asked "does this run and produce plausible numbers", and it
did. One calibration would have caught it instantly and quantitatively:

    A ZERO-EDGE RANDOM WALK MUST RETURN EXACTLY -(cost / stop) IN R.

Price with no drift has no edge, so the only thing left in the expectancy is the
cost. That single assertion measures what the engine ACTUALLY charges, in R, and
compares it to what it was told to charge. It would have printed "charging 0.03x
of stated cost" the first time it ran.

WHAT EACH PROBE IS FOR

    cost_recovery   what the engine charges vs what it was told. Catches unit
                    errors, per-lot/per-unit confusion, missing commission.
    edge_recovery   a planted directional edge must come back at its planted
                    size. Catches sign errors, fill-price errors, R scaling.
    lookahead       shuffle the FUTURE only. Any edge that survives is reading
                    it. Catches same-day joins and off-by-one indexing.
    monotonicity    costs up must mean returns down, never up. Catches sign
                    errors that a single-point test cannot see.

These are metamorphic and known-answer probes rather than assertions about
specific values, so they keep working when the strategy, the symbol or the
timeframe changes.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

CALIBRATION_VERSION = "calibration-2026-08-18-a"

#: Tolerance on a recovered quantity, as a fraction of the planted one. Loose
#: enough that sampling noise on a few thousand trades does not fire it, tight
#: enough that a units error cannot hide: the gold bug was a factor of 33.
TOLERANCE = 0.15

#: Bars generated per probe. Enough that the standard error on a recovered
#: expectancy is small against TOLERANCE.
N_BARS = 20000


@dataclass
class Probe:
    """One known-answer check and what it found."""
    name: str
    expected: float
    recovered: float
    passed: bool
    detail: str = ""

    @property
    def ratio(self) -> Optional[float]:
        if self.expected == 0:
            return None
        return self.recovered / self.expected

    def render(self) -> str:
        mark = "PASS" if self.passed else "**FAIL**"
        r = "n/a" if self.ratio is None else f"{self.ratio:.4f}x"
        return (f"  {mark:<9}{self.name:<26}expected {self.expected:>+10.5f}  "
                f"got {self.recovered:>+10.5f}  ({r})\n"
                + (f"            {self.detail}\n" if self.detail else ""))


def random_walk(n: int = N_BARS, start: float = 2000.0, vol: float = 2.0,
                drift: float = 0.0, seed: int = 0) -> list:
    """OHLC bars from a driftless (or known-drift) random walk.

    A ZERO-DRIFT WALK HAS NO EDGE BY CONSTRUCTION. That is the whole point: any
    expectancy a strategy shows on it is cost, noise, or a bug, and the first two
    are quantifiable. Highs and lows are placed around the path rather than drawn
    independently, so an intrabar stop is reachable in a way that resembles a
    real bar.
    """
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        o = p
        step = rng.gauss(drift, vol)
        c = max(o + step, 0.01)
        wick = abs(rng.gauss(0, vol * 0.5))
        out.append({"open": o, "high": max(o, c) + wick,
                    "low": max(min(o, c) - wick, 0.005), "close": c})
        p = c
    return out


def with_edge(n: int = N_BARS, edge_r: float = 0.20, stop: float = 10.0,
              seed: int = 0) -> tuple:
    """Bars carrying a PLANTED edge of known size, and the entries that hold it.

    THE ENGINE ENTERS AT THE NEXT BAR'S OPEN, AND THE FIRST VERSION OF THIS
    FIXTURE FORGOT IT. Each bar moved the full +2R or -1R within the signal bar,
    so entry at the following open landed exactly on the target: every trade
    closed at +0.0000R and the probe reported that a 0.20R edge had vanished.
    The engine was fine; the world was malformed.

    So each planted trade now spans THREE bars: a signal bar that does nothing,
    an entry bar the engine fills at the open of, and a resolution bar that
    reaches +2R or -1R. The win rate is solved so expectancy is exactly
    `edge_r`, and an engine reporting anything else is wrong by a measurable
    amount.
    """
    # p*2 - (1-p)*1 = edge  ->  p = (edge + 1) / 3
    p_win = (edge_r + 1.0) / 3.0
    rng = random.Random(seed)
    bars, entries, price = [], [], 2000.0
    i = 0
    while len(bars) + 3 <= n:
        # signal bar: flat, so the decision carries no information about the move
        bars.append({"open": price, "high": price + 0.01,
                     "low": price - 0.01, "close": price})
        sig_i = i
        i += 1
        # entry bar: the engine fills at this open, still flat
        bars.append({"open": price, "high": price + 0.01,
                     "low": price - 0.01, "close": price})
        i += 1
        # resolution bar: reaches exactly one of the two levels
        win = rng.random() < p_win
        c = price + (2.0 * stop if win else -1.0 * stop)
        bars.append({"open": price, "high": max(price, c),
                     "low": min(price, c), "close": c})
        i += 1
        entries.append({"i": sig_i, "side": 1, "stop": price - stop,
                        "target": price + 2 * stop, "r": 2.0 if win else -1.0})
        price = c
    # THE REALISED EXPECTANCY, NOT THE INTENDED ONE. Drawing 6,666 outcomes at
    # p=0.400 lands somewhere near it, not on it: this fixture drew 0.389, whose
    # exact expectancy is 0.167, and comparing the engine to the planted 0.200
    # reported a 0.83x "defect" in an engine that had returned every trade
    # exactly right. A known-answer probe must know the answer to the world it
    # actually built, not the one it asked for.
    realised = sum(e["r"] for e in entries) / len(entries) if entries else 0.0
    return bars, entries, realised


def cost_recovery(run: Callable, truth_cost_per_unit: float,
                  seed: int = 0) -> Probe:
    """THE PROBE THAT WOULD HAVE CAUGHT THE GOLD BUG — and nearly did not.

    On a driftless walk the strategy has no edge, so the expectancy IS the cost,
    expressed in R as -(cost / stop_distance).

    THE FIRST VERSION OF THIS PROBE COULD NOT CATCH THE BUG IT WAS WRITTEN FOR,
    and running it against the known-defective engine is what exposed that. It
    took `cost_per_unit` from the adapter and compared the engine to it — but in
    the buggy configuration the adapter passed the wrong figure too, so both
    sides of the comparison were wrong in the same direction and it PASSED at
    0.64x while certifying a 33x error.

    A known-answer probe must take its answer from OUTSIDE the thing under test.
    `truth_cost_per_unit` therefore comes from the instrument's own metadata —
    spread in points times tick size, plus commission over contract size — and
    never from whatever the caller happened to configure. If the engine and the
    truth disagree, that is the finding, whichever of them is wrong.

    It also uses the REALISED mean stop distance rather than the nominal one.
    The engine enters at the next bar's open, so the distance from entry to stop
    is not the distance the signal asked for, and dividing by the nominal figure
    puts a few percent of error into a probe whose whole job is detecting error.
    """
    bars = random_walk(seed=seed)
    exp, stop_dist = run(bars)
    if stop_dist <= 0:
        return Probe("cost recovery", 0.0, 0.0, False,
                     "no trades resolved; the probe could not measure anything")
    expected = -truth_cost_per_unit / stop_dist
    ok = (abs(exp - expected) <= max(abs(expected) * TOLERANCE, 0.005))
    ratio = (exp / expected) if expected else float("nan")
    return Probe("cost recovery", expected, exp, ok,
                 f"realised stop distance {stop_dist:.3f}" if ok else
                 f"the engine charged {ratio:.4f}x of the instrument's REAL "
                 f"cost (realised stop {stop_dist:.3f}). A ratio far from 1.0 "
                 f"is a UNITS error — per-lot read as per-unit, or a contract "
                 f"size applied twice.")


def edge_recovery(run: Callable, planted_r: float, seed: int = 0) -> Probe:
    """A planted edge must come back at its planted size.

    Catches sign errors, fills taken at the wrong price, and R-multiple scaling
    that is off by the stop distance — none of which a "does it run" test sees.
    """
    bars, entries, realised = with_edge(edge_r=planted_r, seed=seed)
    got = run(bars, entries)
    # Tight tolerance, because there is no sampling noise left to absorb: the
    # fixture reports the exact expectancy of the outcomes it generated, so any
    # gap is the engine.
    ok = abs(got - realised) <= 0.005
    return Probe("edge recovery", realised, got, ok,
                 "" if ok else
                 "a planted edge did not come back at its planted size; the "
                 "engine is not measuring what it was given.")


def lookahead(run: Callable, seed: int = 0) -> Probe:
    """Shuffle the RETURNS and require the edge to vanish.

    SHUFFLING BARS IS NOT A NULL, AND THE FIRST VERSION OF THIS PROBE DID THAT.
    Bars carry absolute prices, so reordering them teleports the series between
    price levels and manufactures enormous gaps — the probe reported +0.18R and
    called it lookahead when the driftless walk it was compared against returned
    +0.003R. The engine was fine; the null was broken.

    Shuffling the RETURNS and rebuilding the path preserves everything that
    makes the series a random walk — same step distribution, same volatility,
    continuous prices — while destroying any order-dependent structure. An edge
    that survives that is reading something no strategy could know.
    """
    base = random_walk(seed=seed)
    steps = [b["close"] - b["open"] for b in base]
    wicks = [max(b["high"] - max(b["open"], b["close"]),
                 min(b["open"], b["close"]) - b["low"]) for b in base]
    rng = random.Random(seed + 991)
    order = list(range(len(steps)))
    rng.shuffle(order)
    out, p = [], base[0]["open"]
    for k in order:
        o, c = p, max(p + steps[k], 0.01)
        w = wicks[k]
        out.append({"open": o, "high": max(o, c) + w,
                    "low": max(min(o, c) - w, 0.005), "close": c})
        p = c
    got = run(out)
    ok = abs(got) <= 0.05
    return Probe("lookahead (shuffled returns)", 0.0, got, ok,
                 "" if ok else
                 "an edge survived the returns being reordered. The step "
                 "distribution and volatility are unchanged, so nothing about "
                 "this series is predictable — an edge here is the strategy or "
                 "the harness reading data it should not have.")


def monotone_costs(run: Callable, seed: int = 0) -> Probe:
    """Raising costs must lower returns. Always, at every level.

    A metamorphic probe: it asserts a RELATIONSHIP rather than a value, so it
    keeps working when the strategy or instrument changes, and it catches sign
    errors that any single-point check passes.
    """
    bars = random_walk(seed=seed)
    xs = [run(bars, mult=m) for m in (1.0, 2.0, 3.0)]
    ok = xs[0] > xs[1] > xs[2]
    return Probe("costs monotone", -1.0, 1.0 if ok else -1.0, ok,
                 "" if ok else
                 f"expectancy did not fall as costs rose: {xs}. Cost is "
                 f"entering the P&L with the wrong sign, or not at all.")


@dataclass
class Report:
    probes: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(p.passed for p in self.probes)

    def render(self) -> str:
        head = (f"CALIBRATION  ({CALIBRATION_VERSION})\n"
                f"  known-answer probes against the real engine\n")
        body = "".join(p.render() for p in self.probes)
        fails = [p for p in self.probes if not p.passed]
        if not fails:
            tail = ("\n  All probes recovered their planted answers. This does "
                    "not prove the\n  engine is correct — it proves the four "
                    "failure modes these probes\n  cover are absent.\n")
        else:
            tail = (f"\n  {len(fails)} PROBE(S) FAILED. Each is a known-answer "
                    f"check, so a failure is a\n  DEFECT rather than a "
                    f"disagreement about method — the right answer was put in "
                    f"before\n  the run and something else came out.\n")
        return head + "\n" + body + tail


def run_all(engine: dict) -> Report:
    """Every probe against one engine adapter.

    `engine` supplies callables the probes drive:
        no_edge(bars, mult=1.0) -> expectancy in R on a driftless walk
        planted(bars, entries)  -> expectancy in R on a planted-edge series
        cost_per_unit, stop     -> what the adapter TOLD the engine to charge
    """
    r = Report()
    r.probes.append(cost_recovery(
        lambda b: engine["no_edge_with_stop"](b), engine["truth_cost_per_unit"]))
    r.probes.append(edge_recovery(
        lambda b, e: engine["planted"](b, e), engine.get("planted_r", 0.20)))
    r.probes.append(lookahead(lambda b: engine["no_edge"](b)))
    r.probes.append(monotone_costs(
        lambda b, mult=1.0: engine["no_edge"](b, mult=mult)))
    return r
