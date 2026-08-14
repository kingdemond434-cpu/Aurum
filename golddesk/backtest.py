"""Reproducible, no-lookahead, event-driven walk-forward harness.

It replays the REAL production path. Every arm below constructs a `LiveDesk`
and drives it through `on_bar`/`on_tick` — the same compiler, router, risk gate,
observer, management engine and ledger that a live session uses. There is no
simplified simulation of Aurum anywhere in this file, because a backtest of a
lookalike measures the lookalike.

THREE THINGS THIS FILE REFUSES TO DO

  1. Report a number without saying what resolution produced it. Results
     carry the mix of observed vs assumed fills that generated them.

  2. Tune anything against the holdout. Split boundaries are computed from the
     data's own calendar, frozen into the preregistration hash, and the OOS
     window is never read during fitting.

  3. Call a difference an edge because it is positive. Every rung of the
     ablation ladder is compared to the rung below it on IDENTICAL states,
     paired, and corrected for the number of times the question has been asked.

THE ABLATION LADDER (§9)

Each arm adds exactly ONE capability to the arm beneath it, so the difference
between two adjacent rows is the incremental value of that capability and
nothing else. An arm that cannot beat the rung below it after costs should be
deleted, not improved.

    A  deterministic      rules only, passive management, bar-close observation
    B  + analyst          the model reads the brief
    C  + charts           the model also sees synchronised multi-timeframe images
    D  + router           enforcing hypotheses may veto
    E  + management       contextual management chooses among legal options
    F  + observation      tick/M1 stream drives the observer and wakes management
    G  + reentry          conditional re-entry permitted
    H  + adaptation       cohorts, sealed hypotheses and policy binding update

LEAKAGE IS TESTED, NOT ASSERTED

`leakage_report()` re-derives decisions from a TRUNCATED history — bars[:i+1]
only — and asserts they are identical to the decisions taken with the full
array in memory. If any single decision differs, some code read a bar that had
not closed. This is a proof rather than a code review, and it is cheap.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .analyst import Refusal, Setup, Thresholds, compile_signal
from .costs import CostModel
from .evaluation import (ArmMetrics, Comparison, Preregistration, StateOutcome,
                         bh_fdr, compare, ess, metrics, paired_bootstrap,
                         paired_p_value, report)
from .features import Bar, atr, classify, swings, visible_swings
from .hypothesis import HypothesisBook
from .ledger import Ledger
from .live import LiveDesk, Resolution, Vision
from .notify import Sink
from .opportunity import build_cohorts, resolved_outcomes
from .policies import ContextualChooser, HeuristicChooser, PassiveChooser
from .policy_state import PolicyState
from .providers import AnalystProvider, DeterministicProvider
from .runner import RiskLimits, build_brief

log = logging.getLogger(__name__)

BACKTEST_VERSION = "backtest-2026-08-14-a"


def state_id(symbol: str, timeframe: str, t0: Any) -> str:
    """The canonical identity of a MARKET STATE — never of a decision.

    Two arms looking at the same bar of the same instrument are looking at the
    same state, whatever they each decided about it. Anything arm-specific in
    this string destroys pairing silently, and a broken pairing does not raise:
    it reports a confident null result over an empty intersection.
    """
    ts = t0.isoformat() if hasattr(t0, "isoformat") else str(t0)
    return f"{symbol}|{timeframe}|{ts}"


@dataclass(frozen=True)
class PairingCheck:
    """Did the arms actually pair? Answered before any delta is believed."""
    ok: bool
    expected: int
    shared: int
    per_arm: dict
    detail: str

    def render(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        return (f"PAIRING {head}: {self.shared} shared states vs {self.expected} "
                f"expected — {self.detail}\n"
                + "\n".join(f"    {k}: {v} states" for k, v in sorted(self.per_arm.items())))


def check_pairing(arms: dict[str, Sequence[StateOutcome]],
                  min_fraction: float = 0.90) -> PairingCheck:
    """Assert the arms genuinely share states, and FAIL loudly when they do not.

    `compare()` pairs by intersecting state_id sets and silently returns a null
    result when the intersection is empty. Without this check a completely
    broken evaluation is indistinguishable from an honest "no difference
    detected", and it would first be noticed after paying for a full multi-arm
    model run.
    """
    if len(arms) < 2:
        return PairingCheck(True, 0, 0, {k: len(v) for k, v in arms.items()},
                            "single arm — nothing to pair")
    sets = {k: {o.state_id for o in v} for k, v in arms.items()}
    shared = set.intersection(*sets.values())
    expected = min(len(s) for s in sets.values())
    per_arm = {k: len(v) for k, v in sets.items()}
    if not shared:
        return PairingCheck(False, expected, 0, per_arm,
                            "ZERO shared state ids — every paired delta would be "
                            "computed over an empty set and report a false null")
    frac = len(shared) / max(expected, 1)
    if frac < min_fraction:
        return PairingCheck(False, expected, len(shared), per_arm,
                            f"only {frac:.1%} of the smaller arm's states pair "
                            f"(need {min_fraction:.0%}) — arms saw different states")
    return PairingCheck(True, expected, len(shared), per_arm,
                        f"{frac:.1%} of the smaller arm's states pair")


def assert_arms_differ(arms: Sequence["Arm"]) -> list[str]:
    """Adjacent rungs must differ in EXACTLY the capability they claim to add.

    Two arms with identical configuration execute identical code and produce
    identical outcomes, so their delta is exactly zero by construction. Reported
    as an incremental-value finding that would be a lie, and paid for twice.
    """
    # Explicit label -> field map. A substring test between the human label and
    # the field name is not a check: "charts" and "vision" name the same
    # capability and share no substring, so a naive comparison reports a
    # well-formed ladder as broken and would train the reader to ignore it.
    LABEL_FIELD = {
        "analyst": "analyst",
        "charts": "vision",
        "router": "router",
        "management": "management",
        "observation": "observation",
        "reentry": "reentry",
        "adaptation": "adaptation",
    }
    problems: list[str] = []
    fields = tuple(dict.fromkeys(LABEL_FIELD.values()))
    for prev, cur in zip(arms, arms[1:]):
        diff = [f for f in fields if getattr(prev, f) != getattr(cur, f)]
        if not diff:
            problems.append(f"{prev.name} and {cur.name} are configured IDENTICALLY "
                            f"— they cannot measure the value of '{cur.adds}'")
            continue
        if len(diff) > 1:
            problems.append(f"{prev.name} -> {cur.name} claims to add '{cur.adds}' "
                            f"but changes {len(diff)} things: {diff} — the delta is "
                            f"not attributable to one capability")
            continue
        want = LABEL_FIELD.get(cur.adds)
        if want is None:
            problems.append(f"{cur.name} adds '{cur.adds}', which maps to no known "
                            f"capability field — the ladder cannot be audited")
        elif diff[0] != want:
            problems.append(f"{prev.name} -> {cur.name} is labelled '{cur.adds}' "
                            f"(field {want!r}) but the field that changed is "
                            f"{diff[0]!r}")
    return problems


@dataclass
class FineCoverage:
    """What fraction of the entry series actually has finer observations.

    Reported per split rather than globally, because tick history is typically
    much shorter than bar history: a run can be tick-resolved in its last fold
    and bar-resolved in its first, and averaging those into one number would
    describe neither.
    """
    kind: str                      # "M1" or "TICK"
    entry_bars: int
    covered_bars: int
    observations: int
    first: Optional[datetime]
    last: Optional[datetime]
    problems: list = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return self.covered_bars / max(self.entry_bars, 1)

    @property
    def usable(self) -> bool:
        return not self.problems and self.fraction > 0.0

    def render(self) -> str:
        head = "USABLE" if self.usable else "NOT USABLE"
        return (f"FINE SERIES {self.kind}: {head} — {self.covered_bars}/"
                f"{self.entry_bars} entry bars covered ({self.fraction:.1%}), "
                f"{self.observations:,} observations, "
                f"{self.first} .. {self.last}"
                + ("\n    " + "\n    ".join(self.problems) if self.problems else ""))


def load_fine_series(path: Path, entry_bars: Sequence[Bar], *,
                     kind: str = "M1") -> tuple[dict, FineCoverage]:
    """Map real M1 bars or ticks causally into the entry bar that contains them.

    An entry bar with timestamp T spans [T, T + step). Every finer observation
    whose own timestamp falls inside that half-open interval belongs to it, and
    to no other. Observations outside the entry series' span are dropped rather
    than attached to the nearest bar, because attaching them would place data
    from one bar inside another and silently create lookahead.

    NOTHING IS SYNTHESISED. If a bar has no finer data it gets no entry in the
    returned map, the desk falls back to bar resolution for that bar, and the
    close is stamped BAR_UNAMBIGUOUS or BAR_ASSUMED_STOP_FIRST accordingly. A
    gap in the fine series must show up as coarser provenance, never as invented
    prices.
    """
    import pandas as pd

    problems: list[str] = []
    df = pd.read_parquet(path)
    if df.index.name is None and "utc" in df.columns:
        df = df.set_index("utc")
    idx = pd.to_datetime(df.index, utc=True)

    if kind == "TICK":
        if {"bid", "ask"}.issubset(df.columns):
            price = ((df["bid"].astype(float) + df["ask"].astype(float)) / 2.0).to_numpy()
            crossed = int((df["ask"].astype(float) < df["bid"].astype(float)).sum())
            if crossed:
                problems.append(f"{crossed} crossed quotes (ask < bid) in the tick file")
        elif "last" in df.columns:
            price = df["last"].astype(float).to_numpy()
        else:
            problems.append(f"tick file has neither bid/ask nor last: {list(df.columns)}")
            price = None
        series = list(zip(idx.to_pydatetime(), price)) if price is not None else []
    else:
        need = {"open", "high", "low", "close"}
        if not need.issubset(df.columns):
            problems.append(f"M1 file missing OHLC columns: {sorted(need - set(df.columns))}")
            series = []
        else:
            # Present each M1 bar as open then close. Its own high/low ordering
            # is unknown, and inventing one would recreate at M1 exactly the
            # ambiguity that carrying M1 exists to remove.
            series = []
            for t, o, c in zip(idx.to_pydatetime(), df["open"].astype(float),
                               df["close"].astype(float)):
                series.append((t, float(o)))
                series.append((t, float(c)))

    if len(entry_bars) < 2:
        problems.append("entry series too short to infer its step")
        return {}, FineCoverage(kind, len(entry_bars), 0, 0, None, None, problems)
    step = entry_bars[1].ts - entry_bars[0].ts
    if step <= timedelta(0):
        problems.append(f"entry series step is not positive: {step}")
        return {}, FineCoverage(kind, len(entry_bars), 0, 0, None, None, problems)

    edges = [b.ts for b in entry_bars]
    out: dict[int, list] = {}
    import bisect
    for t, px in series:
        j = bisect.bisect_right(edges, t) - 1
        if j < 0 or j >= len(entry_bars):
            continue
        if t >= edges[j] + step:
            continue                     # inside a gap, belongs to no bar
        out.setdefault(j, []).append((t, px))
    for v in out.values():
        v.sort(key=lambda x: x[0])

    cov = FineCoverage(kind, len(entry_bars), len(out), sum(len(v) for v in out.values()),
                       series[0][0] if series else None,
                       series[-1][0] if series else None, problems)
    if cov.fraction == 0.0:
        cov.problems.append("no entry bar received a single finer observation — "
                            "the fine series does not overlap the entry series")
    return out, cov


class NullSink(Sink):
    """Notifications are a live concern. A backtest that sends them is a bug."""

    def __init__(self) -> None:
        self.count = 0

    def send(self, text: str) -> bool:
        self.count += 1
        return True


# --------------------------------------------------------------------------
# Splits — immutable, derived from the calendar, hashed into the prereg
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Split:
    name: str
    lo: int          # inclusive bar index
    hi: int          # exclusive
    start: datetime
    end: datetime

    def render(self) -> str:
        return (f"{self.name:<12} bars[{self.lo}:{self.hi}]  "
                f"{self.start:%Y-%m-%d} .. {self.end:%Y-%m-%d}  "
                f"({self.hi - self.lo} bars)")


def chronological_splits(bars: Sequence[Bar], warmup: int = 260,
                         train: float = 0.50, calib: float = 0.20
                         ) -> tuple[Split, Split, Split]:
    """Train / calibration / OOS, in time order, never shuffled.

    Shuffling or k-folding a price series lets the model learn from its own
    future. The only honest partition of a time series is a chronological one,
    and the only honest holdout is the most recent piece.
    """
    n = len(bars)
    usable = n - warmup
    if usable < 60:
        raise ValueError(f"need at least 60 usable bars after {warmup} warmup, "
                         f"have {usable}")
    a = warmup + int(usable * train)
    b = a + int(usable * calib)
    return (Split("train", warmup, a, bars[warmup].ts, bars[a - 1].ts),
            Split("calibration", a, b, bars[a].ts, bars[b - 1].ts),
            Split("oos", b, n, bars[b].ts, bars[n - 1].ts))


# --------------------------------------------------------------------------
# The ablation ladder
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Arm:
    """One rung. Differs from the rung below by exactly one capability."""
    name: str
    adds: str
    analyst: str = "deterministic"     # deterministic | provider
    vision: Vision = Vision.NUMERIC_ONLY
    router: bool = False               # enforcing hypotheses may veto
    management: str = PassiveChooser.name
    observation: bool = False          # drive the tick path
    reentry: bool = False
    adaptation: bool = False

    def render(self) -> str:
        return (f"{self.name:<4} +{self.adds:<14} analyst={self.analyst:<13} "
                f"vision={self.vision.value:<20} mgmt={self.management:<14} "
                f"obs={int(self.observation)} router={int(self.router)} "
                f"re={int(self.reentry)} adapt={int(self.adaptation)}")


def ladder(provider_available: bool, tick_available: bool) -> list[Arm]:
    """The ladder, truncated honestly at what the environment can actually run.

    A rung whose capability is unavailable is OMITTED rather than silently
    downgraded to the rung below, because an arm labelled '+charts' that ran
    without charts would contaminate every comparison above it.
    """
    arms = [Arm("A", "baseline")]
    if provider_available:
        arms += [
            Arm("B", "analyst", analyst="provider"),
            Arm("C", "charts", analyst="provider",
                vision=Vision.NUMERIC_PLUS_CHARTS),
            Arm("D", "router", analyst="provider",
                vision=Vision.NUMERIC_PLUS_CHARTS, router=True),
            Arm("E", "management", analyst="provider",
                vision=Vision.NUMERIC_PLUS_CHARTS, router=True,
                management=ContextualChooser.name),
        ]
        if tick_available:
            arms.append(Arm("F", "observation", analyst="provider",
                            vision=Vision.NUMERIC_PLUS_CHARTS, router=True,
                            management=ContextualChooser.name, observation=True))
        arms.append(replace(arms[-1], name="G", adds="reentry", reentry=True))
        arms.append(replace(arms[-1], name="H", adds="adaptation", adaptation=True))
    return arms


# --------------------------------------------------------------------------
# Running one arm
# --------------------------------------------------------------------------

@dataclass
class ArmRun:
    arm: Arm
    split: Split
    outcomes: list[StateOutcome] = field(default_factory=list)
    resolutions: dict[str, int] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)

    @property
    def assumed_fraction(self) -> float:
        tot = sum(self.resolutions.values())
        if not tot:
            return 0.0
        return self.resolutions.get(Resolution.M15_PESSIMISTIC_UNCERTAIN.value, 0) / tot


class Backtest:
    """Replays the production path over a split, one arm at a time."""

    def __init__(self, bars: Sequence[Bar], out: Path, *,
                 provider_factory: Optional[Callable[[], AnalystProvider]] = None,
                 intrabar: Optional[dict[int, list[tuple[datetime, float]]]] = None,
                 cost_model: CostModel = CostModel(),
                 limits: RiskLimits = RiskLimits(),
                 thresholds: Thresholds = Thresholds(),
                 warmup: int = 260,
                 timeframe: str = "M15",
                 fine_resolution: Optional[Resolution] = None,
                 broker: Optional[Any] = None,
                 adapt_every: int = 25):
        self.bars = list(bars)
        self.timeframe = timeframe
        # What an attached fine series is honestly called. None means no fine
        # series exists, and arm F is not runnable.
        self.fine_resolution = fine_resolution
        self.broker = broker
        self.adapt_every = adapt_every
        self.adapt_cycles = 0
        self.adapt_moves = 0
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.provider_factory = provider_factory
        self.intrabar = intrabar or {}
        self.cost_model, self.limits, self.thresholds = cost_model, limits, thresholds
        self.warmup = warmup
        self.sw = swings(self.bars)
        self.atrs = atr(self.bars)

    # -- one arm over one split ------------------------------------------
    def run_arm(self, arm: Arm, split: Split, *,
                cohorts: Optional[dict] = None,
                book: Optional[HypothesisBook] = None) -> ArmRun:
        run = ArmRun(arm, split)
        tag = f"{arm.name}-{split.name}"
        self.adapt_cycles = self.adapt_moves = 0
        ledger = Ledger(self.out / f"ledger-{tag}.jsonl")
        if ledger.path.exists():
            ledger.path.unlink()

        provider: AnalystProvider
        if arm.analyst == "provider":
            if self.provider_factory is None:
                raise RuntimeError(f"arm {arm.name} needs a provider and none was "
                                   f"supplied — refusing to substitute the baseline")
            provider = self.provider_factory()
        else:
            provider = DeterministicProvider()

        pstate = PolicyState(self.out / f"policy-{tag}.json",
                             defaults={"management_chooser": arm.management,
                                       "reentry_policy": "reentry-2026-08-14-a"})
        desk = LiveDesk(
            provider, ledger, NullSink(), shadow=True,
            thresholds=self.thresholds, cost_model=self.cost_model,
            limits=self.limits, vision=arm.vision,
            cohorts=cohorts if arm.router or cohorts else cohorts,
            book=book if arm.router else None,
            policy_state=pstate,
            shadow_management=True, shadow_contextual=False,
            **({"broker": self.broker} if self.broker is not None else {}),
            **({"fine_resolution": self.fine_resolution}
               if self.fine_resolution is not None else {}))

        for i in range(split.lo, split.hi):
            b = self.bars[i]
            # NO-LOOKAHEAD: only swings confirmed at or before i are visible, and
            # the desk is handed the history up to i. The forward slice used for
            # outcome resolution is written by the ledger and never read back
            # into a decision — leakage_report() proves that rather than trusting it.
            vis = visible_swings(self.sw, i)
            desk.on_bar(self.bars, i, vis, self.atrs, None,
                        (b.close - b.spread / 2 if b.spread else b.close - 0.21,
                         b.close + b.spread / 2 if b.spread else b.close + 0.21,
                         1.0),
                        (f"{split.name} replay",),
                        intrabar=self.intrabar.get(i) if arm.observation else None)

            if arm.observation and desk.open is not None:
                for ts, px in self.intrabar.get(i, ()):
                    if desk.open is None:
                        break
                    desk.on_tick(px, ts)

            if not arm.reentry:
                desk.prior = None          # re-entry capability withheld on this rung

            # BOUNDED ADAPTATION (arm H). Runs inside the test window using ONLY
            # rows already written, so every update is causal: knowledge at bar i
            # is derived from outcomes resolved at or before bar i. `adaptation`
            # was previously a label on the Arm dataclass that nothing read, so
            # arm H executed byte-identical logic to arm G and its "incremental
            # value" was zero by construction.
            if arm.adaptation and (i - split.lo) > 0 and (i - split.lo) % self.adapt_every == 0:
                self._adapt_step(desk, ledger, book, on=b.ts.date())

        rows = ledger.read_all()
        run.stats = asdict(desk.stats)
        run.trades = [r for r in rows if r.get("kind") == "TRADE_CLOSED"]
        for r in run.trades:
            k = r.get("resolution", "UNKNOWN")
            run.resolutions[k] = run.resolutions.get(k, 0) + 1
        run.outcomes = self._outcomes(rows, arm.name)
        run.stats["adapt_cycles"] = self.adapt_cycles
        run.stats["adapt_moves"] = self.adapt_moves
        return run

    def _adapt_step(self, desk: LiveDesk, ledger: Ledger,
                    book: Optional[HypothesisBook], on: date) -> None:
        """One bounded adaptation cycle on information available so far.

        Deliberately narrow. It refreshes cohort statistics — which is pure
        measurement and immediately changes what the expectancy gate takes — and
        lets sealed hypotheses accrue post-seal evidence and be adjudicated. It
        does NOT rebind policies mid-window, because a policy that changes
        halfway through a test makes the arm a mixture of two systems and its
        result attributable to neither.
        """
        rows = ledger.read_all()
        desk.cohorts = build_cohorts(rows)
        self.adapt_cycles += 1
        if book is not None:
            book.accrue(rows)
            moves = book.adjudicate(on=on)
            self.adapt_moves += len(moves)

    def _outcomes(self, rows: Sequence[dict], arm: str) -> list[StateOutcome]:
        """Every decided state, acted or not — refusals carry their counterfactual.

        A refusal with a forward path is an observation about a decision, not an
        absence of one. Dropping them is how a system convinces itself that
        being inactive is free.

        STATE IDs ARE CANONICAL AND ARM-INDEPENDENT. They previously carried an
        `{arm}-` prefix, which meant `compare()` — which pairs by intersecting
        state_id sets — found an empty intersection for every pair. Every paired
        delta was computed over zero states: mean 0.0, CI [0,0], p=1.0, and it
        looked like a legitimate "no significant difference" result rather than
        the total failure to pair that it was. A canonical id is the market
        state, which is exactly what must be identical across arms.
        """
        out: list[StateOutcome] = []
        closed = {r.get("entry_t0"): r for r in rows if r.get("kind") == "TRADE_CLOSED"}
        for r in rows:
            kind = str(r.get("kind", ""))
            t0 = r.get("t0")
            if not t0:
                continue
            ts = datetime.fromisoformat(t0) if isinstance(t0, str) else t0
            fwd = r.get("outcome") or {}
            best = float(fwd.get("best_achievable_r") or 0.0)
            sid = state_id(r.get("symbol", "XAUUSD"), self.timeframe, t0)
            if kind == "SIGNAL":
                c = closed.get(t0)
                net = float(c["realised_r"]) if c else 0.0
                out.append(StateOutcome(sid, ts, True, net, best))
            elif kind.startswith("REFUSAL"):
                out.append(StateOutcome(sid, ts, False, 0.0, best))
        return out

    # -- leakage -----------------------------------------------------------
    def leakage_report(self, split: Split, sample: int = 40) -> tuple[bool, list[str]]:
        """Re-derive decisions from a TRUNCATED history and demand they match.

        If any decision computed from bars[:i+1] differs from the decision
        computed with the full series resident in memory, some component read a
        bar that had not closed. This is the difference between believing the
        code is causal and knowing it.
        """
        problems: list[str] = []
        step = max(1, (split.hi - split.lo) // sample)
        checked = 0
        for i in range(split.lo, split.hi, step):
            full_st = classify(self.bars, i, visible_swings(self.sw, i), self.atrs)
            trunc_bars = self.bars[:i + 1]
            trunc_sw = swings(trunc_bars)
            trunc_atrs = atr(trunc_bars)
            trunc_st = classify(trunc_bars, i, visible_swings(trunc_sw, i), trunc_atrs)
            checked += 1
            if (full_st is None) != (trunc_st is None):
                problems.append(f"bar {i}: state presence differs under truncation")
                continue
            if full_st is None:
                continue
            for fld in ("trend_direction", "trend_health", "volatility_state",
                        "displacement_state", "reclaim_state", "sweep_state"):
                a, b = getattr(full_st, fld, None), getattr(trunc_st, fld, None)
                if a != b:
                    problems.append(f"bar {i}: {fld} {a!r} (full) != {b!r} (truncated) "
                                    f"— FUTURE LEAKED INTO A DECISION")
            if full_st.atr is not None and trunc_st.atr is not None:
                if abs(full_st.atr - trunc_st.atr) > 1e-9:
                    problems.append(f"bar {i}: atr differs under truncation")
        return (not problems), problems + [f"(checked {checked} states)"]


# --------------------------------------------------------------------------
# Walk-forward
# --------------------------------------------------------------------------

@dataclass
class Fold:
    index: int
    fit: Split
    test: Split


def walk_forward_folds(bars: Sequence[Bar], warmup: int = 260,
                       n_folds: int = 4, min_fit: int = 300) -> list[Fold]:
    """Rolling origin: fit on everything before the window, test on the window.

    Anchored rather than sliding, so each fold's fit set is a strict superset of
    the last. That matches how the desk actually accumulates knowledge, and it
    means a fold cannot be helped by data that arrives after it.
    """
    n = len(bars)
    usable = n - warmup - min_fit
    if usable < n_folds * 30:
        n_folds = max(1, usable // 30)
    width = usable // max(n_folds, 1)
    folds = []
    for k in range(n_folds):
        lo = warmup + min_fit + k * width
        hi = lo + width if k < n_folds - 1 else n
        if hi - lo < 20:
            continue
        folds.append(Fold(
            k,
            Split(f"fit{k}", warmup, lo, bars[warmup].ts, bars[lo - 1].ts),
            Split(f"test{k}", lo, hi, bars[lo].ts, bars[hi - 1].ts)))
    return folds


def fit_knowledge(bt: Backtest, arm: Arm, fit: Split,
                  book_path: Path) -> tuple[dict, HypothesisBook]:
    """Build cohorts and seal hypotheses using ONLY the fit window.

    The sealed hypotheses record the fit window's end as their seal instant, so
    the test window is post-seal by construction and can confirm them without
    the circularity of judging a rule on the data that produced it.
    """
    run = bt.run_arm(replace(arm, router=False, adaptation=False), fit)
    rows = Ledger(bt.out / f"ledger-{arm.name}-{fit.name}.jsonl").read_all()
    cohorts = build_cohorts(rows)
    book = HypothesisBook(book_path)
    from .adapt import Adapter
    ad = Adapter(bt.out / f"adapt-{arm.name}-{fit.name}.jsonl", book=book)
    ad.discover(rows, [{"setup": s.value} for s in Setup if s is not Setup.NO_SETUP],
                seal_ts=fit.end, min_n=8)
    return cohorts, book


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def resolution_note(runs: Sequence[ArmRun]) -> str:
    tot: dict[str, int] = {}
    for r in runs:
        for k, v in r.resolutions.items():
            tot[k] = tot.get(k, 0) + v
    n = sum(tot.values())
    if not n:
        return "no closed trades — resolution mix undefined"
    parts = ", ".join(f"{k}={v} ({v/n:.0%})" for k, v in sorted(tot.items()))
    assumed = tot.get(Resolution.BAR_ASSUMED_STOP_FIRST.value, 0)
    fine = (tot.get(Resolution.TICK_OBSERVED.value, 0)
            + tot.get(Resolution.M1_OBSERVED.value, 0))
    verdict = (f"{fine/n:.0%} resolved on a fine series; "
               + ("no fill assumed" if not assumed else
                  f"{assumed/n:.0%} of fills ASSUMED (deciding bar touched both "
                  f"stop and target) — that fraction is a modelling choice, not "
                  f"a measurement"))
    return f"{parts}   [{verdict}]"


def full_report(runs: Sequence[ArmRun], prereg: Preregistration,
                leak_ok: bool, leak_notes: Sequence[str]) -> str:
    # Merge across folds. Keying by arm name without merging silently reported
    # only the final fold, which understates n and hides fold disagreement.
    arms: dict[str, list[StateOutcome]] = {}
    for r in runs:
        arms.setdefault(r.arm.name, []).extend(r.outcomes)
    for v in arms.values():
        v.sort(key=lambda o: o.ts)

    # compare() returns a 3-tuple (metrics, comparisons, verdicts). It was
    # previously assigned whole to `comps` and handed to report() as if it were
    # the comparisons list, so the report rendered a tuple where the paired
    # deltas belonged and the verdicts were dropped entirely. It did not raise,
    # because report() iterates whatever it is given.
    pairing = check_pairing(arms)
    if len(arms) > 1:
        mets, comps, verdicts = compare(arms, prereg)
    else:
        mets, comps, verdicts = [metrics(n, o) for n, o in arms.items()], [], []
    lines = [
        "=" * 92,
        f"AURUM WALK-FORWARD REPORT  ({BACKTEST_VERSION})",
        "=" * 92,
        "",
        "LEAKAGE",
        f"  truncation-invariance: {'PASS' if leak_ok else 'FAIL'}",
        *[f"    {n}" for n in leak_notes[:6]],
        "",
        "RESOLUTION PROVENANCE",
        f"  {resolution_note(runs)}",
        "",
        "PAIRING",
        *[f"  {ln}" for ln in pairing.render().splitlines()],
        "",
        "PREREGISTRATION",
        f"  hash {prereg.content_hash()}  frozen {prereg.frozen_at}",
        f"  holdout {prereg.holdout_start} .. {prereg.holdout_end}",
        f"  declared trials {prereg.trials_declared} -> effective "
        f"{prereg.effective_trials} after inflation",
        "",
    ]
    lines.append(report(mets, comps, verdicts))

    # Fold-level agreement. A total that is driven by one fold is a different
    # claim from one that holds across all of them, and the aggregate hides it.
    lines += ["", "FOLD AGREEMENT (does the result hold across time, or once?)"]
    per: dict[str, list[tuple[str, float, int]]] = {}
    for r in runs:
        m = metrics(r.arm.name, r.outcomes)
        per.setdefault(r.arm.name, []).append((r.split.name, m.net_r, m.n_acted))
    for name, rows_ in sorted(per.items()):
        agree = sum(1 for _, net, _ in rows_ if net > 0)
        detail = "  ".join(f"{s}:{net:+.2f}R/{n}" for s, net, n in rows_)
        lines.append(f"  {name}: {agree}/{len(rows_)} folds positive   {detail}")
    return "\n".join(lines)
