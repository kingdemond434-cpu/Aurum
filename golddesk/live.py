"""The live/shadow desk — full position lifecycle, wired.

    feed -> multi-timeframe state -> watcher -> analyst -> compiler -> router
         -> risk -> ENTRY -> continuous tick observer -> economic wake
         -> contextual management -> intrabar-resolved EXIT -> re-entry eval
         -> ledger, with Telegram at every actionable moment.

SHADOW is the default and it is not a lesser mode: it runs the identical code
path and produces the identical decisions, but marks every notification as
shadow and never claims a fill. Promotion to live is a flag, not a rewrite —
which is the only way the thing you evaluated is the thing you run.

WHAT CHANGED IN THIS REVISION, AND WHY

Four capabilities existed as modules and were not on the executable path. A
module that nothing imports is a plan, not a capability, so each is now wired
and each seam is observable in the ledger:

  * TradeObserver is instantiated per open position and driven by on_tick().
    Between M15 closes the desk was blind; a trade could run +2R and give it
    all back while the code managing it waited for a candle.
  * Exit resolution uses observed ordering whenever a finer series exists, and
    every close is stamped with HOW it was determined. A bar in which only one
    of stop/target was touched is BAR_UNAMBIGUOUS — coarse but not assumed; a
    bar that touched both is BAR_ASSUMED_STOP_FIRST and is the only uncertain
    category. An assumed fill and an observed one must never aggregate into the
    same number silently.
  * Management is a named policy resolved from durable PolicyState, with the
    losing arms evaluated on the identical option set so the comparison is
    paired rather than anecdotal.
  * Re-entry runs through the versioned ReentryPolicy. The old free function
    carried a hardcoded 20-minute cool-off and an MFE>=0.5R ban that no
    evidence had ever justified and no record ever disclosed.

Telegram can fail, hang, or be unconfigured. It is called through a Sink that
swallows everything; a notification failure can never interrupt monitoring or
management. That is enforced by _notify() below, not by hope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

from .analyst import CompiledSignal, Refusal, Setup, Thresholds, compile_signal
from .costs import CostModel
from .features import Bar, StructureState, atr, classify, session_of, swings
from .hypothesis import HypothesisBook
from .ledger import (Bar as LBar, DecisionKind, DecisionRecord, Ledger, PathRef,
                     resolve_forward)
from .management import (Action, Anchor, BrokerLimits, Excursion, ManagementPolicy,
                         Position, ThesisState, apply_option, enumerate_options)
from .notify import Sink, build_sink
from .observer import TradeObserver, Trigger, Wake, resolve_intrabar
from .policies import (ContextualChooser, HeuristicChooser, ManagementChooser,
                       PassiveChooser, ReentryPolicy)
from .policy_state import PolicyState
from .providers import AnalystError, AnalystProvider, ProviderRead
from .quant_findings import strength_bucket
from .reentry import PriorTrade
from .runner import RiskLimits, RiskState, build_brief, risk_check
from .watcher import Watcher

log = logging.getLogger(__name__)


def _downsample_path(path: Sequence, cap: int = 400) -> list:
    """Keep the shape and both extremes, bound the size.

    A tick-driven trade can accumulate tens of thousands of points and the
    ledger has to stay readable. Evenly sampling loses exactly the turning
    points a counterfactual needs, so the min and max are pinned in explicitly
    rather than left to chance.
    """
    if not path:
        return []
    pts = [(ts.isoformat() if hasattr(ts, "isoformat") else str(ts), round(float(r), 4))
           for ts, r in path]
    if len(pts) <= cap:
        return pts
    step = len(pts) / cap
    keep = {0, len(pts) - 1}
    keep.add(max(range(len(pts)), key=lambda i: pts[i][1]))   # MFE
    keep.add(min(range(len(pts)), key=lambda i: pts[i][1]))   # MAE
    keep.update(int(i * step) for i in range(cap))
    return [pts[i] for i in sorted(keep) if i < len(pts)]


ENTRY_TF = "M15"
HTF = "H4"

SLOT_MGMT = "management_chooser"
SLOT_REENTRY = "reentry_policy"


class Vision(str, Enum):
    """What the analyst is actually shown. Never inferred, always declared.

    `charts: bool = False` used to sit in the constructor as a quiet default,
    which meant a configuration intending numeric+visual Claude could run
    numeric-only for weeks and produce results filed under the wrong arm. The
    mode is now explicit and, more importantly, VERIFIED at the call site: if
    NUMERIC_PLUS_CHARTS is declared and no chart renders, the read is refused
    rather than downgraded.
    """
    NUMERIC_ONLY = "NUMERIC_ONLY"
    NUMERIC_PLUS_CHARTS = "NUMERIC_PLUS_CHARTS"


class Resolution(str, Enum):
    """How an exit price was determined. Aggregating these together is a lie.

    The previous version had three values and used one of them dishonestly: a
    close derived from nothing finer than the entry timeframe's OHLC was stamped
    M1_OBSERVED, which asserts that an M1 series was consulted when none existed.
    A provenance label that can be wrong about its own provenance is worse than
    no label, because it survives into the evidence table looking authoritative.

    The categories now distinguish RESOLUTION (what data decided it) from
    AMBIGUITY (whether ordering mattered at all):

      TICK_OBSERVED        ordering seen in a tick stream
      M1_OBSERVED          ordering seen in a real M1 series
      BAR_UNAMBIGUOUS      only one of stop/target was touched in the deciding
                           bar, so ordering could not have changed the outcome.
                           Coarse data, but the answer is not an assumption.
      BAR_ASSUMED_STOP_FIRST
                           the deciding bar touched BOTH. Stop-first is assumed.
                           This is the only genuinely uncertain category and it
                           must never be averaged into the others silently.
      MANAGED_EXIT         closed by a management decision at a known price.
    """
    TICK_OBSERVED = "TICK_OBSERVED"
    M1_OBSERVED = "M1_OBSERVED"
    BAR_UNAMBIGUOUS = "BAR_UNAMBIGUOUS"
    BAR_ASSUMED_STOP_FIRST = "BAR_ASSUMED_STOP_FIRST"
    MANAGED_EXIT = "MANAGED_EXIT"

    @property
    def is_assumption(self) -> bool:
        return self is Resolution.BAR_ASSUMED_STOP_FIRST


@dataclass
class OpenTrade:
    position: Position
    signal: CompiledSignal
    opened_idx: int
    observer: TradeObserver
    partials: list[tuple[float, float]] = field(default_factory=list)
    notified_lock: bool = False
    mgmt_log: list[dict] = field(default_factory=list)
    # carried from the entry so the close row is self-describing and the
    # learning loop needs no correlation step to know what it is looking at
    entry_context: dict = field(default_factory=dict)
    mechanism_name: str = "unnamed"
    # SIZE, in risk units. The ledger's realised_r stays POSITION-R — R measured
    # against this trade's own stop — because every existing comparison, cohort
    # and hypothesis is denominated that way and redefining it mid-stream would
    # silently corrupt all of them. Account-level R is position_r * risk_r, and
    # is written alongside rather than instead.
    risk_r: float = 1.0
    sizing_basis: str = "flat 1R"

    # excursion is owned by the observer — these read through so the rest of
    # the desk does not care whether ticks or bars produced them
    @property
    def mfe_r(self) -> float:
        return self.observer.mfe_r

    @property
    def mae_r(self) -> float:
        return self.observer.mae_r

    @property
    def t_mfe(self) -> float:
        o = self.observer
        return (o.t_mfe - o.opened).total_seconds() if o.t_mfe else 0.0

    @property
    def t_mae(self) -> float:
        o = self.observer
        return (o.t_mae - o.opened).total_seconds() if o.t_mae else 0.0


@dataclass
class LiveStats:
    states: int = 0
    wakes: int = 0
    reads: int = 0
    analyst_errors: int = 0
    entries: int = 0
    exits: int = 0
    stop_moves: int = 0
    partials: int = 0
    reentry_allowed: int = 0
    reentry_blocked: int = 0
    notifications: int = 0
    notify_failures: int = 0
    ticks: int = 0
    observer_wakes: int = 0
    mgmt_reconsiderations: int = 0
    exits_tick_resolved: int = 0
    exits_m1_resolved: int = 0
    exits_bar_unambiguous: int = 0
    exits_assumed: int = 0
    exits_managed: int = 0
    hypothesis_vetoes: int = 0
    states_blocked_position_open: int = 0


class LiveDesk:
    """One symbol, one open position at a time, full lifecycle."""

    def __init__(self, provider: AnalystProvider, ledger: Ledger,
                 sink: Optional[Sink] = None, *, shadow: bool = True,
                 thresholds: Thresholds = Thresholds(),
                 cost_model: CostModel = CostModel(),
                 limits: RiskLimits = RiskLimits(),
                 policy: ManagementPolicy = ManagementPolicy(),
                 heartbeat: timedelta = timedelta(minutes=30),
                 min_gap: timedelta = timedelta(0),   # NO throttle by default
                 vision: Vision = Vision.NUMERIC_ONLY,
                 cohorts: Optional[dict] = None,
                 policy_state: Optional[PolicyState] = None,
                 book: Optional[HypothesisBook] = None,
                 choosers: Optional[dict[str, ManagementChooser]] = None,
                 reentry_policies: Optional[dict[str, ReentryPolicy]] = None,
                 shadow_management: bool = True,
                 shadow_contextual: bool = False,
                 observer_heartbeat: timedelta = timedelta(minutes=30),
                 broker: BrokerLimits = BrokerLimits(),
                 fine_resolution: Resolution = Resolution.M1_OBSERVED,
                 htf_factor: int = 16,
                 measure_position_constraint: bool = True,
                 concurrency_ceiling: Optional[int] = None,
                 universe_mode: bool = False,
                 calendar=None,
                 regime_history=None,
                 entry_urgency: float = 0.5,
                 forward_bars: int = 480):
        self.provider, self.ledger = provider, ledger
        self.sink = sink or build_sink(None)
        self.shadow = shadow
        self.thresholds, self.cost_model = thresholds, cost_model
        self.limits, self.policy = limits, policy
        self.watcher = Watcher(heartbeat=heartbeat, min_gap=min_gap)
        self.vision, self.cohorts = vision, cohorts
        self.book = book
        self.obs_heartbeat = observer_heartbeat
        self.shadow_management = shadow_management
        self.shadow_contextual = shadow_contextual

        # Competing policies. The contextual arm is only constructible when the
        # provider can actually choose; registering it otherwise would let a
        # NotImplementedError masquerade as a management decision.
        self.choosers: dict[str, ManagementChooser] = dict(choosers or {})
        if not self.choosers:
            self.choosers = {PassiveChooser.name: PassiveChooser(),
                             HeuristicChooser.name: HeuristicChooser()}
            if self._provider_can_choose():
                c = ContextualChooser(provider)
                self.choosers[c.name] = c
        self.reentry_policies = dict(reentry_policies or
                                     {ReentryPolicy().version: ReentryPolicy()})

        # Durable, versioned, decay-aware active configuration. The declared
        # defaults are the INCUMBENTS — they hold their slots because they were
        # already running, not because they won anything.
        self.policy_state = policy_state or PolicyState(
            Path("state/policy_state.json"),
            defaults={SLOT_MGMT: HeuristicChooser.name,
                      SLOT_REENTRY: next(iter(self.reentry_policies))})

        self.risk = RiskState()
        # MULTI-THESIS. The desk holds a LIST of open theses, not one trade.
        # `open` remains as a property returning the first, so every existing
        # caller — service checkpointing, the backtester, the tests — keeps
        # working while the concurrency limit becomes a constitutional question
        # rather than a structural one.
        self.open_trades: list[OpenTrade] = []
        self.prior: Optional[PriorTrade] = None
        self.stats = LiveStats()
        # Live quote and venue limits, so management legality is a property of
        # NOW rather than of the entry. Kept as the last observed values because
        # a management step can fire on a tick between bars.
        self.broker = broker
        self.last_bid: Optional[float] = None
        self.last_ask: Optional[float] = None
        self.last_spread: Optional[float] = None
        # What a fine series, if one is attached, is honestly called.
        self._fine_resolution = fine_resolution
        # How many entry-timeframe bars make one HTF candle. 16 M15 = H4.
        # Used for TRUE OHLC aggregation, never for sampling.
        self.htf_factor = htf_factor
        self.measure_position_constraint = measure_position_constraint
        # A hard ceiling on simultaneous theses, independent of heat. Not a
        # quota on opportunity: heat is the economic limit and normally binds
        # first. This only stops a pathological state opening dozens.
        # None = NO COUNT LIMIT. Heat governs. See max_concurrent().
        self.concurrency_ceiling = concurrency_ceiling
        # How far forward a refusal is resolved. 480 M15 bars is five trading
        # days: long enough that "this would have paid" is a fact about gold
        # rather than about the window. See _record for why the old 60 was a
        # bias and not merely an approximation.
        self.forward_bars = forward_bars
        # Ask for the whole opportunity set rather than a single best trade.
        # OFF by default: it changes what the analyst is asked, so it is an ARM
        # to be measured against the single-read arm on identical states, not an
        # improvement to be assumed. Every provider supports it — the base class
        # wraps a single read into a one-candidate universe — so switching arms
        # never changes which providers are available.
        self.universe_mode = universe_mode
        # Event proximity. None means the calendar is not wired, which the
        # uncertainty decomposition reports as UNKNOWN rather than as "no event"
        # — those are different claims and only one of them is true.
        self.calendar = calendar
        # Resolved history to compare the current state against. None means
        # novelty is unmeasurable, reported as UNKNOWN for the same reason.
        self.regime_history = regime_history
        # How fast the edge decays, for the execution planner. An INPUT, not a
        # constant discovered by preference — it is stamped on every plan so its
        # value can be audited against what fills actually happened.
        self.entry_urgency = entry_urgency
        # Set by set_management(); None means "use the durable binding".
        self._management_override: Optional[str] = None
        self._last_state: Optional[StructureState] = None
        self._last_bars: Optional[Sequence[Bar]] = None
        self._last_idx: int = 0

    # -- open positions ---------------------------------------------------
    @property
    def open(self) -> Optional[OpenTrade]:
        """The first open thesis, or None. Compatibility surface."""
        return self.open_trades[0] if self.open_trades else None

    @open.setter
    def open(self, t: Optional[OpenTrade]) -> None:
        self.open_trades = [] if t is None else [t]

    def max_concurrent(self) -> int:
        """How many theses may run at once — decided by the CONSTITUTION.

        This is the seam the audit correctly called out: risk.one_position was
        measured but not removable, because on_bar returned unconditionally
        while a trade was open. Now the limit is read from the registry, so a
        restriction that fails its counterfactual review genuinely stops
        applying instead of being demoted on paper only.

        When it is demoted there is NO COUNT AT ALL. Portfolio heat is the only
        limiter, and it is the correct one: max_open_risk_r bounds total
        exposure and risk_check applies a correlation haircut, so five copies of
        the same bullish idea cannot each claim full independent risk. At the
        default 2.0R ceiling with a 0.65 haircut, a second same-direction thesis
        already cannot fit — the count was never what stopped it.

        A COUNT IS A QUOTA AND A QUOTA HAS NO ECONOMICS IN IT. Four
        simultaneous positive-expectancy opportunities are worth more than
        three, and the fourth is not worse than the third for being fourth.
        Whatever a count would have blocked, heat blocks correctly or does not
        need blocking. `concurrency_ceiling` therefore defaults to None, meaning
        unlimited, and exists only as a runaway-process guard for an operator
        who wants one. If it ever binds it is logged as an anomaly, because a
        count binding before heat means something is wrong with the risk maths,
        not that the desk found too many opportunities.
        """
        from .constitution import is_enforcing
        if is_enforcing("risk.one_position"):
            return 1
        if self.concurrency_ceiling is None:
            return 1 << 30            # unlimited; heat is the only real limit
        return self.concurrency_ceiling

    def _provider_can_choose(self) -> bool:
        """Does this provider implement management choice? Checked by INTERFACE.

        The previous version answered by calling choose_option() with a dummy
        prompt, which for a real vendor is a billed network round trip issued at
        construction time purely to discover whether a method was overridden.
        Capability is a property of the class, so read it from the class.
        """
        return type(self.provider).choose_option is not AnalystProvider.choose_option

    # -- active policy resolution ----------------------------------------
    def active_chooser(self) -> ManagementChooser:
        # Operator override wins over the durable binding. Deliberate: the flag
        # is a production decision made by a human at boot, and it should not be
        # quietly overturned by an adaptation cycle mid-run.
        want = self._management_override or self.policy_state.active(SLOT_MGMT)
        ch = self.choosers.get(want)
        if ch is None:
            log.warning("bound management policy %r is not registered — "
                        "falling back to passive rather than guessing", want)
            return self.choosers.get(PassiveChooser.name, PassiveChooser())
        return ch

    def active_reentry(self) -> ReentryPolicy:
        want = self.policy_state.active(SLOT_REENTRY)
        return self.reentry_policies.get(want) or next(iter(self.reentry_policies.values()))

    def set_management(self, mode: str) -> str:
        """Bind who has AUTHORITY over the open position. Explicit, not implicit.

        The desk's most consequential unstated choice was that Claude forms the
        entry judgement while a deterministic heuristic runs the whole lifecycle
        after fill. That is a reasonable production stance — contextual
        management has not beaten the heuristic on paired states, and granting
        authority before it has is what the evidence standard exists to prevent
        — but it was inherited from a default rather than decided.

        Raises on `contextual` when the provider cannot actually choose, rather
        than silently falling back: a run labelled contextual that is quietly
        heuristic contaminates the arm it is filed under.
        """
        alias = {"heuristic": HeuristicChooser.name,
                 "passive": PassiveChooser.name,
                 "contextual": ContextualChooser.name}
        want = alias.get(mode, mode)
        if want not in self.choosers:
            if mode == "contextual":
                raise ValueError(
                    "--management contextual requires a provider that implements "
                    "choose_option(); this one does not. Refusing rather than "
                    "running heuristic under a contextual label.")
            raise ValueError(f"unknown management mode {mode!r}; "
                             f"have {sorted(self.choosers)}")
        # An operator choice is NOT a warranted promotion, and it must not be
        # written through policy_state.bind(): that path records an evidence
        # warrant with a TTL, so a flag would masquerade as a proven policy and
        # then silently lapse mid-run. The override is held separately, claims
        # no evidence, and is stamped on every row as operator-selected.
        self._management_override = want
        return want

    # -- notification: never allowed to break anything -------------------
    def _notify(self, text: str) -> None:
        tag = "[SHADOW] " if self.shadow else ""
        try:
            ok = self.sink.send(tag + text)
            self.stats.notifications += 1
            if not ok:
                self.stats.notify_failures += 1
        except Exception as e:                       # sinks should not raise; belt and braces
            self.stats.notify_failures += 1
            log.warning("notification failed, continuing: %s", e)

    # ====================================================================
    # TICK PATH — continuous observation between bar closes
    # ====================================================================
    def on_tick(self, price: float, ts: datetime, *,
                bar_closed: bool = False,
                bid: Optional[float] = None,
                ask: Optional[float] = None) -> Optional[str]:
        """One tick or M1 close. Cheap, and the position's real sense organ.

        Returns a short string when the tick caused something, for tracing.
        This is the method the MT5 stream drives; if it is never called, the
        observer never sees anything and continuous observation is a claim
        rather than a fact.
        """
        if not self.open_trades:
            return None
        self.stats.ticks += 1
        results = [r for r in (self._tick_one(t, price, ts, bar_closed, bid, ask)
                               for t in list(self.open_trades)) if r]
        return "; ".join(results) if results else None

    def _tick_one(self, t: OpenTrade, price: float, ts: datetime,
                  bar_closed: bool, bid: Optional[float],
                  ask: Optional[float]) -> Optional[str]:

        # Exit check FIRST, at the resolution the tick provides. Because ticks
        # arrive in order, whether the stop or the target came first is
        # OBSERVED here — this is the production half of the intrabar problem.
        pos = t.position
        long = pos.long
        # EXECUTION SIDE. A long is closed by SELLING at the bid; a short by
        # BUYING at the ask. Evaluating either against the mid delays adverse
        # stop touches by half a spread and brings targets forward by the same
        # amount — a bias that flatters every result and is largest exactly
        # where the spread is widest.
        if bid is not None and ask is not None:
            self.last_bid, self.last_ask = bid, ask
            self.last_spread = max(0.0, ask - bid)
        exit_px = (bid if bid is not None else price) if long else (
            ask if ask is not None else price)
        if (exit_px <= pos.current_stop) if long else (exit_px >= pos.current_stop):
            r = pos.r_at(pos.current_stop)
            self._close(ts, r, "PROFITABLE_STOP" if r > 0 else "STOP",
                        self._last_state, Resolution.TICK_OBSERVED, t=t)
            return "EXIT_STOP"
        if (exit_px >= t.signal.tp2) if long else (exit_px <= t.signal.tp2):
            self._close(ts, pos.r_at(t.signal.tp2), "TARGET",
                        self._last_state, Resolution.TICK_OBSERVED, t=t)
            return "EXIT_TARGET"

        wake = t.observer.observe(price, ts, heartbeat=self.obs_heartbeat,
                                  bar_closed=bar_closed)
        if wake is None:
            return None
        self.stats.observer_wakes += 1
        if self._last_state is None:
            return "WAKE_NO_STATE"           # no closed-bar context yet
        acted = self._management_step(ts, price, self._last_state,
                                      source=f"observer:{'+'.join(x.value for x in wake.triggers)}",
                                      wake=wake, t=t)
        return f"WAKE->{acted}"

    # ====================================================================
    # BAR PATH — structure, entry analysis
    # ====================================================================
    def on_bar(self, bars: Sequence[Bar], i: int, sw, atrs,
               htf_state: Optional[StructureState],
               quote: tuple[float, float, float], timeline: Sequence[str],
               intrabar: Optional[Sequence[tuple[datetime, float]]] = None) -> None:
        st = classify(bars, i, sw, atrs)
        if st is None:
            return
        self.stats.states += 1
        ts = bars[i].ts
        self.risk.roll(ts)
        self._last_state, self._last_bars, self._last_idx = st, bars, i

        for t in list(self.open_trades):
            self._manage(bars, i, st, intrabar, t)

        if len(self.open_trades) >= self.max_concurrent():
            # ONE-POSITION CONSTRAINT — registered as risk.one_position and
            # measured, not assumed. The portfolio-heat engine already supports
            # several concurrent exposures, so holding exactly one is a policy
            # choice that silently discards every independent, add-on and
            # opposite opportunity arriving while a trade is open.
            #
            # No analyst call is made here: the brief is built deterministically
            # and journalled as a refusal carrying its forward path, so the
            # constraint's forgone value is observed rather than modelled, and
            # it costs nothing in inference to keep measuring it.
            if self.measure_position_constraint:
                self.stats.states_blocked_position_open += 1
                bid, ask, age = quote
                try:
                    b2 = build_brief(bars, i, st, sw, bid, ask, age, htf_state,
                                     timeline, timeframe=ENTRY_TF)
                    self._record(bars, i, b2, DecisionKind.REFUSAL_COMPILER, "POLICY",
                                 {"declined": "UNEVALUATED",
                                  "constraint": "one_position"},
                                 "one-position constraint: a trade is already open",
                                 "LONG" if st.trend_direction == "UP" else "SHORT",
                                 st.atr)
                except Exception as e:                # measurement must never break management
                    log.debug("could not journal position-constraint cost: %s", e)
            return

        w = self.watcher.observe(st, session_of(ts), ts)
        if not w.wake:
            return
        self.stats.wakes += 1

        # The prior trade is only relevant while its structural context still
        # exists. Once the trend that defined it has flipped away, it is not a
        # "re-entry" context any more and must not suppress anything.
        if self.prior is not None:
            want = "UP" if self.prior.direction == "LONG" else "DOWN"
            if st.trend_direction != want:
                self.prior = None            # context gone; ordinary trading resumes

        bid, ask, age = quote
        self.last_bid, self.last_ask = bid, ask
        self.last_spread = max(0.0, ask - bid)
        brief = build_brief(bars, i, st, sw, bid, ask, age, htf_state, timeline,
                            timeframe=ENTRY_TF)
        # THE CAUSAL SNAPSHOT OF THIS DECISION MOMENT, built before anything
        # decides. It is what makes the model league real rather than
        # theoretical: a competitor -- another model, a rule, the user -- can be
        # handed this exact state later and scored against what Claude did on
        # it. Built here and not at scoring time, because reconstructing "what
        # was knowable" after the fact is precisely the reconstruction that
        # leaks. Never fatal: a snapshot failure must not cost a trade.
        self._pending_snapshot = None
        try:
            self._pending_snapshot = self._snapshot(bars, i, brief)
        except Exception as e:                        # noqa: BLE001
            log.warning("snapshot skipped at %s: %s", ts, e)

        try:
            imgs = self._render_charts(bars, i)
        except AnalystError as e:
            self.stats.analyst_errors += 1
            log.warning("charts unavailable at %s: %s", ts, e)
            return

        if self.universe_mode:
            return self._decide_universe(bars, i, brief, imgs, ts, st)

        try:
            pr = self.provider.read(brief, imgs)
            self.stats.reads += 1
        except AnalystError as e:
            self.stats.analyst_errors += 1
            log.warning("analyst unavailable at %s: %s", ts, e)
            return

        if pr.read.setup is Setup.NO_SETUP:
            self._record(bars, i, brief, DecisionKind.REFUSAL_MODEL, "MODEL",
                         {"setup": "NO_SETUP", "analyst_read": pr.read.model_dump(),
                          "vision": self.vision.value, "charts_sent": len(imgs),
                          **pr.stamp()}, "analyst: NO_SETUP", "LONG", brief.atr)
            return

        # Re-entry gate applies ONLY to a proposal repeating the prior trade's
        # direction while its context survives. An opposite-direction read is a
        # new idea and is never gated by what happened before it.
        if self.prior is not None and pr.read.direction == self.prior.direction:
            rp = self.active_reentry()
            v = rp.evaluate(self.prior, st, ts)
            if not v.allowed:
                self.stats.reentry_blocked += 1
                self._record(bars, i, brief, DecisionKind.REFUSAL_COMPILER, "POLICY",
                             {"declined": pr.read.direction,
                              "reentry_policy": rp.version,
                              "analyst_read": pr.read.model_dump(), **pr.stamp()},
                             f"re-entry blocked: {v.reason}", pr.read.direction, brief.atr)
                return
            self.stats.reentry_allowed += 1
            self._notify(f"*RE-ENTRY* {pr.read.direction} — {v.reason}")

        res = compile_signal(brief, pr.read, self.thresholds, self.cost_model,
                             self.cohorts)
        if isinstance(res, Refusal):
            router = "edge router" in res.reason
            self._record(bars, i, brief,
                         DecisionKind.REFUSAL_ROUTER if router else DecisionKind.REFUSAL_COMPILER,
                         "ROUTER" if router else "COMPILER",
                         {"declined": pr.read.direction,
                          "analyst_read": pr.read.model_dump(), **pr.stamp()},
                         res.reason, pr.read.direction, brief.atr)
            return

        # Enforcing hypotheses are the ONLY discovered rules permitted to refuse
        # a trade, and only after independent post-seal confirmation.
        if self.book is not None:
            veto = self.book.veto(dict(brief.context.__dict__),
                                  {"setup": res.setup.value,
                                   "direction": res.direction}, on=ts.date())
            if veto is not None:
                self.stats.hypothesis_vetoes += 1
                self._record(bars, i, brief, DecisionKind.REFUSAL_ROUTER, "ROUTER",
                             {"declined": res.direction, "hypothesis": veto.hid,
                              "hypothesis_hash": veto.content_hash(), **pr.stamp()},
                             f"enforcing hypothesis {veto.hid}: {veto.statement}",
                             res.direction, res.risk)
                return

        ok, why = risk_check(res, self.risk, self.limits)
        if not ok:
            self._record(bars, i, brief, DecisionKind.REFUSAL_COMPILER, "POLICY",
                         {"declined": res.direction, **pr.stamp()},
                         f"risk: {why}", res.direction, res.risk)
            return

        self._enter(bars, i, brief, res, pr, len(imgs))

    # -- the opportunity universe -----------------------------------------
    def _decide_universe(self, bars, i, brief, imgs, ts, st) -> None:
        """Enumerate everything available, then decide as a portfolio.

        The single-read path asks "what is the trade" and acts on the answer.
        This path asks "what is available", compiles every proposition through
        the identical gates, and lets portfolio economics choose. The material
        difference is not that it takes more trades — usually it takes the same
        one — it is that what it did NOT take is written down with geometry and
        resolved forward, so the cost of selecting is measurable for the first
        time.
        """
        from .universe import Selection, compile_universe, select, MAX_CANDIDATES
        from .opportunity import Heat
        from .providers import ProviderRead

        try:
            stamp, uni = self.provider.survey(brief, imgs)
            self.stats.reads += 1
        except AnalystError as e:
            self.stats.analyst_errors += 1
            log.warning("analyst unavailable at %s: %s", ts, e)
            return

        cands = compile_universe(brief, uni, self.thresholds, self.cost_model,
                                 self.cohorts)
        heat = Heat(max_open_risk_r=self.limits.max_open_risk_r,
                    correlation_haircut=self.limits.correlation_haircut,
                    max_daily_loss_r=self.limits.max_daily_loss_r)
        sel = select(cands, heat,
                     open_risks=self.risk.open_risks,
                     open_directions=self.risk.open_directions,
                     day_loss_r=self.risk.day_loss_r,
                     max_concurrent=self.max_concurrent(),
                     cap_filled=len(uni.candidates) >= MAX_CANDIDATES,
                     analyst_had_more=bool(getattr(uni, "had_more", False)))

        # The survey itself is journalled BEFORE anything is entered, so the
        # record of what was available does not depend on what was taken.
        self._record(bars, i, brief, DecisionKind.REFUSAL_MODEL, "MODEL",
                     {"universe": sel.to_journal(), "survey": uni.survey,
                      "dominant_context": uni.dominant_context,
                      "vision": self.vision.value, "charts_sent": len(imgs),
                      **stamp.stamp()},
                     f"universe: {len(cands)} enumerated, {len(sel.taken)} selected",
                     "LONG", brief.atr, suffix="universe")

        # NOTE: there is deliberately no `if not sel.taken: return` here. A
        # moment where the desk enumerated live propositions and took NONE of
        # them is the single most valuable refusal in the ledger — it is the
        # daily-loss limit, or the ceiling, or heat, turning away everything
        # that was available. Returning early there would discard precisely the
        # evidence those restrictions are supposed to be reviewed against.
        for c in sel.taken:
            pr = ProviderRead(c.read, stamp.provider, stamp.model,
                              stamp.latency_ms, dict(stamp.usage))
            sig = c.compiled

            # Re-entry, hypothesis veto and solvency apply PER CANDIDATE. A
            # candidate that clears selection has not thereby cleared the gates
            # the single-read path applies after compilation.
            if self.prior is not None and sig.direction == self.prior.direction:
                rp = self.active_reentry()
                v = rp.evaluate(self.prior, st, ts)
                if not v.allowed:
                    self.stats.reentry_blocked += 1
                    c.disposition, c.disposition_reason = "GATED", f"re-entry: {v.reason}"
                    self._record(bars, i, brief, DecisionKind.REFUSAL_COMPILER, "POLICY",
                                 {"declined": sig.direction, "candidate": c.to_journal(),
                                  "reentry_policy": rp.version, **stamp.stamp()},
                                 f"re-entry blocked: {v.reason}", sig.direction,
                                 brief.atr, suffix=f"c{c.index}")
                    continue

            if self.book is not None:
                veto = self.book.veto(dict(brief.context.__dict__),
                                      {"setup": sig.setup.value,
                                       "direction": sig.direction}, on=ts.date())
                if veto is not None:
                    self.stats.hypothesis_vetoes += 1
                    c.disposition = "GATED"
                    c.disposition_reason = f"enforcing hypothesis {veto.hid}"
                    self._record(bars, i, brief, DecisionKind.REFUSAL_ROUTER, "ROUTER",
                                 {"declined": sig.direction, "candidate": c.to_journal(),
                                  "hypothesis": veto.hid,
                                  "hypothesis_hash": veto.content_hash(), **stamp.stamp()},
                                 f"enforcing hypothesis {veto.hid}: {veto.statement}",
                                 sig.direction, sig.risk, suffix=f"c{c.index}")
                    continue

            ok, why = risk_check(sig, self.risk, self.limits)
            if not ok:
                c.disposition, c.disposition_reason = "GATED", f"risk: {why}"
                self._record(bars, i, brief, DecisionKind.REFUSAL_COMPILER, "POLICY",
                             {"declined": sig.direction, "candidate": c.to_journal(),
                              **stamp.stamp()},
                             f"risk: {why}", sig.direction, sig.risk,
                             suffix=f"c{c.index}")
                continue

            self._enter(bars, i, brief, sig, pr, len(imgs), suffix=f"c{c.index}")

        # Every candidate that survived its gates but lost a budget contest is
        # journalled with full geometry. This is the measurement that makes the
        # selection rule accountable: resolve_forward runs on each one, so a
        # month later the ledger can say what deferring cost.
        for c in sel.candidates:
            if c.disposition != "DEFERRED":
                continue
            self._record(bars, i, brief, DecisionKind.REFUSAL_COMPILER, "POLICY",
                         {"declined": c.direction, "candidate": c.to_journal(),
                          "budget_bound": sel.budget_bound,
                          "tiebreak_used": sel.tiebreak_used, **stamp.stamp()},
                         f"deferred: {c.disposition_reason}", c.direction,
                         c.compiled.risk if c.compiled else brief.atr,
                         suffix=f"d{c.index}")

    def _render_charts(self, bars, i):
        """Synchronised multi-timeframe visual context.

        Whether this helps is an OPEN QUESTION measured by the factorial, not
        assumed. What is NOT open to question is that the declared mode and the
        delivered mode must match: a NUMERIC_PLUS_CHARTS run that silently
        posts no images is a numeric run wearing the wrong label, and it would
        contaminate the arm it is filed under.
        """
        if self.vision is Vision.NUMERIC_ONLY:
            return ()
        from .chart import Bar as CB, render_clean_chart
        from .features import aggregate
        out = []
        # TRUE higher-timeframe aggregation. The previous version sliced every
        # 16th M15 bar, which produces fifteen-minute candles spaced four hours
        # apart and labels them H4 — misstating the higher timeframe's range,
        # and therefore its swings, sweeps and displacement, on every chart.
        for label, factor, n in ((f"{HTF}-context", self.htf_factor, 90),
                                 ("H1-context", self.htf_factor // 4, 90),
                                 (f"{ENTRY_TF}-entry", 1, 120)):
            if factor < 1:
                continue
            # Only closed source bars, and enough of them to fill the window.
            src = bars[max(0, i - factor * (n + 2)):i + 1]
            agg = aggregate(src, factor) if factor > 1 else list(src)
            win = agg[-n:]
            if len(win) >= 30:
                out.append(render_clean_chart([CB(b.open, b.high, b.low, b.close)
                                               for b in win], label))
        if not out:
            raise AnalystError(
                f"vision={self.vision.value} requires charts but none rendered at "
                f"bar {i} (insufficient history) — refusing rather than silently "
                f"downgrading this read to numeric-only")
        return tuple(out)

    # -- what the desk knows about a proposal beyond its geometry ----------
    def _edge_r(self, sig: CompiledSignal, mechanism: str) -> Optional[float]:
        """Measured expected value for this mechanism, or None.

        None is the common case and the honest one. A mechanism with no resolved
        history has no edge estimate, and several downstream decisions —
        execution style above all — are only answerable with one. Returning a
        made-up number so those decisions always have an input is how a desk
        ends up with confident arithmetic on top of nothing.
        """
        from .opportunity import ev_gate
        v = ev_gate(sig.rr_tp2, sig.cost_r, mechanism, self.cohorts,
                    fallback_min_rr=self.thresholds.fallback_min_rr,
                    min_ev_r=self.thresholds.min_ev_r)
        if v.basis != "COHORT" or v.ev_r is None:
            return None
        import math
        return None if math.isnan(v.ev_r) else v.ev_r

    def _assess(self, brief, sig: CompiledSignal, mechanism: str,
                views: Optional[dict] = None):
        """The six-component uncertainty decomposition for this proposal."""
        from .uncertainty import assess
        from .regime import similarity_to_history
        stat = (self.cohorts or {}).get(mechanism)
        sim = similarity_to_history(dict(brief.context.__dict__), self.cohorts,
                                    self.regime_history)
        ev = self.calendar.next_event(brief.as_of_utc) if self.calendar else None
        return assess(
            n_resolved=stat.n if stat else 0,
            similarity=sim,
            tick_age_s=brief.tick_age_s,
            max_age_s=self.thresholds.max_tick_age_s,
            views=views or {},
            spread=brief.spread, risk_price=sig.risk,
            minutes_to_event=ev[0] if ev else None,
            event_name=ev[1] if ev else "")

    def _size(self, sig: CompiledSignal, mechanism: str):
        """Allocation for this proposal, and whether it is allowed to bind."""
        from .allocation import default_size
        from .constitution import is_enforcing
        stat = (self.cohorts or {}).get(mechanism)
        alloc = default_size(
            cohort_n=stat.n if stat else 0,
            win_rate=stat.hit_rate_shrunk if stat and stat.n else None,
            rr=sig.rr_tp2, cost_r=sig.cost_r,
            open_risk_r=sum(self.risk.open_risks),
            max_open_risk_r=self.limits.max_open_risk_r,
            same_direction=sum(1 for d in self.risk.open_directions
                               if d == sig.direction),
            haircut=self.limits.correlation_haircut,
            day_loss_r=self.risk.day_loss_r,
            max_daily_loss_r=self.limits.max_daily_loss_r)
        binds = is_enforcing("risk.adaptive_sizing")
        return alloc, (alloc.risk_r if binds else 1.0), binds

    def _execution(self, brief, sig: CompiledSignal, edge_r: Optional[float]):
        """How to get in — advice on the signal, never a gate. None when unmeasured."""
        if edge_r is None or sig.trigger_price is None:
            return None
        from .allocation import plan_entry
        origin = sig.trigger_price
        drift_r = ((brief.mid - origin) / sig.risk if sig.direction == "LONG"
                   else (origin - brief.mid) / sig.risk)
        return plan_entry(spread=brief.spread, risk_price=sig.risk,
                          drift_r=max(0.0, drift_r), atr=brief.atr,
                          edge_r=edge_r, trigger_price=origin, mid=brief.mid,
                          urgency=self.entry_urgency)

    # -- entry -------------------------------------------------------------
    def _enter(self, bars, i, brief, sig: CompiledSignal, pr: ProviderRead,
               n_charts: int = 0, suffix: str = "") -> None:
        mech = pr.read.mechanism_name
        edge_r = self._edge_r(sig, mech)
        unc = self._assess(brief, sig, mech)
        alloc, risk_r, sizing_binds = self._size(sig, mech)
        plan = self._execution(brief, sig, edge_r)

        pos = Position(sig.direction, sig.entry, sig.stop, sig.stop, sig.risk,
                       1.0, 0.0, bars[i].ts, sig.setup.value)
        obs = TradeObserver(direction=sig.direction, entry=sig.entry, stop=sig.stop,
                            target=sig.tp2, risk_price=sig.risk, opened=bars[i].ts)
        self.open_trades.append(OpenTrade(pos, sig, i, obs,
                              entry_context=dict(brief.context.__dict__)
                              | {"session": brief.session} | self._trend_ctx(brief),
                              mechanism_name=mech,
                              risk_r=risk_r, sizing_basis=alloc.basis))
        self.risk.open_risks.append(risk_r)
        self.risk.open_directions.append(sig.direction)
        self.risk.day_signals += 1                # reporting only; never enforced
        self.stats.entries += 1
        self._record(bars, i, brief, DecisionKind.SIGNAL, "MODEL",
                     {"direction": sig.direction, "entry": sig.entry, "stop": sig.stop,
                      "tp1": sig.tp1, "tp2": sig.tp2, "rr_tp2": sig.rr_tp2,
                      "cost_r": sig.cost_r, "analyst_read": pr.read.model_dump(),
                      "vision": self.vision.value, "charts_sent": n_charts,
                      "management_policy": self.active_chooser().name,
                      # WHO will manage this position, and on whose authority.
                      # On the SIGNAL row as well as the close row: a signal is
                      # filed under an arm at the moment it is produced, and
                      # recovering that from the close row only works for trades
                      # that closed.
                      "management_authority": ("operator" if self._management_override
                                               else "durable-binding"),
                      "edge_r": edge_r,
                      "uncertainty": unc.to_dict(),
                      "sizing": {"risk_r": risk_r, "wanted_r": alloc.risk_r,
                                 "basis": alloc.basis, "capped_by": alloc.capped_by,
                                 "enforcing": sizing_binds},
                      "execution": ({"style": plan.style, "price": plan.price,
                                     "expected_cost_r": plan.expected_cost_r,
                                     "fill_probability": plan.fill_probability,
                                     "basis": plan.basis} if plan else None),
                      **pr.stamp()},
                     f"ENTRY {sig.direction} rr {sig.rr_tp2:.2f}", sig.direction,
                     sig.risk, suffix=suffix)
        # HOW TO GET IN, on the message a human acts on. The desk is advisory,
        # so "wait at 2003, fills 63% of the time" is the difference between the
        # signal being actionable and being a direction. Silent when the
        # mechanism has no measured edge, because without one the comparison
        # always favours waiting and the advice would be worse than none.
        if plan is None:
            how = "\n`HOW    ` at market — no measured edge yet for this mechanism"
        else:
            at = f" at {plan.price:.2f}" if plan.price else ""
            how = (f"\n`HOW    ` {plan.style}{at} · fills {plan.fill_probability:.0%}"
                   f"\n         _{plan.basis}_")
        # WHAT IS UNCERTAIN, and which kind. Only the components that are
        # actually elevated — a list of six LOWs is noise on a phone.
        hi = unc.highest
        risk_line = ("\n`RISK   ` " + ", ".join(f"{c.name} {c.level}" for c in hi)
                     if hi else "")
        size_line = ""
        if abs(alloc.risk_r - 1.0) > 0.01:
            verb = "sized" if sizing_binds else "would size"
            size_line = (f"\n`SIZE   ` {verb} {alloc.risk_r:.2f}R"
                         + ("" if sizing_binds else " (advisory — risking 1R)"))
        self._notify(
            f"*ENTRY {sig.direction} {brief.symbol}*\n"
            f"`entry  {sig.entry:.2f}`\n`SL     {sig.stop:.2f}`  ({sig.risk:.2f} risk)\n"
            f"`TP1    {sig.tp1:.2f}`\n`TP2    {sig.tp2:.2f}`  ({sig.rr_tp2:.2f}R net)"
            f"{how}{size_line}{risk_line}\n"
            f"conf {sig.confidence}/5 · cost {sig.cost_r:.3f}R · "
            f"breakeven {sig.breakeven_win_rate:.0%}\n\n{sig.read}\n\n"
            f"*Why:* {sig.why}\n*Against:* {sig.why_not}\n*Invalid if:* {sig.invalidation}")

    # -- management --------------------------------------------------------
    def _manage(self, bars, i, st: StructureState,
                intrabar: Optional[Sequence[tuple[datetime, float]]] = None,
                t: Optional[OpenTrade] = None) -> None:
        """Bar-close management for ONE thesis. Exits resolve on the finest
        series available."""
        t = t or self.open
        if t is None:
            return
        pos, b = t.position, bars[i]
        long = pos.long

        # Feed the observer the bar's range so excursion is maintained even when
        # no tick stream is attached. Order within the bar is unknown, so this
        # updates MFE/MAE and explicitly does not fabricate velocity.
        t.observer.note_extremes(b.low, b.high, b.ts)

        stop_hit = (b.low <= pos.current_stop) if long else (b.high >= pos.current_stop)
        tp_hit = (b.high >= t.signal.tp2) if long else (b.low <= t.signal.tp2)

        if stop_hit or tp_hit:
            if intrabar:
                # Ordering OBSERVED — the whole point of carrying M1/ticks.
                ev = resolve_intrabar(intrabar, pos.entry, pos.current_stop,
                                      t.signal.tp2, pos.direction, pos.risk_price)
                if ev is not None:
                    reason = ("TARGET" if ev.kind == "TARGET" else
                              ("PROFITABLE_STOP" if ev.r > 0 else "STOP"))
                    self._close(ev.ts, ev.r, reason, st, self._fine_resolution, t=t)
                    return
            if stop_hit and tp_hit:
                # The deciding bar touched both and nothing finer exists. The
                # stop is assumed to have come first, and the record says so —
                # this is the ONLY uncertain category.
                r = pos.r_at(pos.current_stop)
                self._close(b.ts, r, "PROFITABLE_STOP" if r > 0 else "STOP", st,
                            Resolution.BAR_ASSUMED_STOP_FIRST, t=t)
                return
            # Exactly one side was touched, so ordering could not have changed
            # the outcome. Coarse, but not an assumption.
            if stop_hit:
                r = pos.r_at(pos.current_stop)
                self._close(b.ts, r, "PROFITABLE_STOP" if r > 0 else "STOP", st,
                            Resolution.BAR_UNAMBIGUOUS, t=t)
                return
            self._close(b.ts, pos.r_at(t.signal.tp2), "TARGET", st,
                        Resolution.BAR_UNAMBIGUOUS, t=t)
            return

        self._management_step(b.ts, b.close, st, source="bar_close", t=t)

    def _management_step(self, ts: datetime, price: float, st: StructureState,
                         *, source: str, wake: Optional[Wake] = None,
                         t: Optional[OpenTrade] = None) -> str:
        """One reconsideration. Shared by the tick path and the bar path.

        Every registered policy is asked the same question on the same legal
        option set; only the ACTIVE one is applied. That is what makes the later
        comparison paired — the arms differ in choice, never in the state they
        were choosing from.
        """
        t = t or self.open
        if t is None:
            return "no_position"
        pos = t.position
        long = pos.long
        self.stats.mgmt_reconsiderations += 1

        o = t.observer
        exc = Excursion(o.mfe_r, o.mae_r, t.t_mfe, t.t_mae, pos.r_at(price),
                        max(0, self._last_idx - t.opened_idx))
        thesis = ThesisState(
            structure_intact=(st.trend_direction == ("UP" if long else "DOWN")),
            trend_health=st.trend_health, volatility_state=st.volatility_state,
            displacement_against=(st.displacement_state in ("CONFIRMED", "EXCEPTIONAL")
                                  and st.trend_direction != ("UP" if long else "DOWN")),
            target_liquidity_taken=False,
            invalidation_touched=False)
        anchors = []
        if st.swing_low:
            anchors.append(Anchor("A_SL", "SWING_LOW", st.swing_low.price, ENTRY_TF, True))
        if st.swing_high:
            anchors.append(Anchor("A_SH", "SWING_HIGH", st.swing_high.price, ENTRY_TF, True))
        if st.trigger_price is not None:
            anchors.append(Anchor("A_TRG", "RECLAIM", st.trigger_price, ENTRY_TF, True))

        spread = self.last_spread if self.last_spread is not None else 0.48
        bid = self.last_bid if self.last_bid is not None else price - spread / 2
        ask = self.last_ask if self.last_ask is not None else price + spread / 2
        opts = enumerate_options(pos, thesis, exc, anchors, st.atr, spread,
                                 self.policy, self.cost_model,
                                 bid=bid, ask=ask, broker=self.broker)
        if not opts:
            return "no_legal_options"

        active = self.active_chooser()
        choice = active.choose(opts, pos, thesis, exc)

        # Paired shadow: what would the other arms have done, here, now?
        shadow: dict[str, Optional[str]] = {}
        if self.shadow_management:
            for name, ch in self.choosers.items():
                if name == active.name:
                    continue
                if isinstance(ch, ContextualChooser) and not self.shadow_contextual:
                    continue          # a shadow arm that costs money is opt-in
                try:
                    alt = ch.choose(opts, pos, thesis, exc)
                    shadow[name] = alt.id if alt else None
                except Exception as e:
                    log.debug("shadow arm %s failed: %s", name, e)
                    shadow[name] = None

        t.mgmt_log.append({
            "ts": ts.isoformat(), "source": source,
            "trigger": [x.value for x in wake.triggers] if wake else [],
            "value_at_stake_r": round(wake.value_at_stake_r, 4) if wake else None,
            "options": [o.id for o in opts],
            "active_policy": active.name,
            "chosen": choice.id if choice else None,
            "shadow": shadow,
            "r_open": round(exc.r_open_now, 4), "mfe_r": round(exc.mfe_r, 4),
            "mae_r": round(exc.mae_r, 4)})

        if choice is None or choice.action is Action.HOLD:
            return "HOLD"

        dec = apply_option(pos, choice, exc, self.policy)
        if not dec.ok:
            log.info("management option %s rejected: %s", choice.id, dec.rejected_reason)
            return f"REJECTED:{dec.rejected_reason}"
        t.position = dec.position_after
        # the stop moved — the observer must manage against the NEW stop
        t.observer.stop = dec.position_after.current_stop

        if choice.action is Action.EXIT:
            self._close(ts, exc.r_open_now, "EXIT_THESIS", st,
                        Resolution.MANAGED_EXIT, t=t)
            return "EXIT"
        if choice.action is Action.PARTIAL:
            t.partials.append((choice.partial_fraction or 0.0, price))
            self.stats.partials += 1
            self._notify(
                f"*PARTIAL BANK* {pos.direction} {t.signal.setup.value}\n"
                f"bank `{(choice.partial_fraction or 0):.0%}` at `{price:.2f}` "
                f"(+{exc.r_open_now:.2f}R open)\n"
                f"runner `{dec.position_after.remaining_fraction:.0%}` stays on, "
                f"SL `{dec.position_after.current_stop:.2f}`\n"
                f"locked `{dec.position_after.locked_r:+.2f}R` · {choice.facts}\n"
                f"_via {active.name} on {source}_")
            return "PARTIAL"
        # PROTECT / TRAIL
        self.stats.stop_moves += 1
        locked = dec.position_after.locked_r
        kind = "PROFIT LOCK" if locked >= 0 else "SL TRAIL"
        self._notify(
            f"*{kind}* {pos.direction} {t.signal.setup.value}\n"
            f"SL `{pos.current_stop:.2f}` -> `{dec.position_after.current_stop:.2f}`\n"
            f"locked `{locked:+.2f}R` · open risk "
            f"`{dec.position_after.open_risk_r:.2f}R` · MFE `{exc.mfe_r:+.2f}R`\n"
            f"{choice.facts}\n_via {active.name} on {source}_")
        return kind.replace(" ", "_")

    # -- exit --------------------------------------------------------------
    def _close(self, ts: datetime, realised_r: float, reason: str,
               st: Optional[StructureState],
               resolution: Resolution = Resolution.BAR_ASSUMED_STOP_FIRST,
               t: Optional[OpenTrade] = None) -> None:
        t = t or self.open
        if t is None:
            return
        # NET of execution cost. Position.r_at() is pure price movement over the
        # risk unit, so everything downstream of it was gross. The compiler
        # already priced the round trip once, on mid, at compile time — that is
        # `signal.cost_r` — and it is charged here so the number the ledger calls
        # realised R is the number the account would actually see. Comparing
        # arms that differ by hundredths of an R against a gross P&L decides
        # which component "wins" on accounting error.
        gross = t.position.banked_r + realised_r * t.position.remaining_fraction
        cost_r = float(getattr(t.signal, "cost_r", 0.0) or 0.0)
        total = gross - cost_r
        # Release THIS thesis's risk, not whichever happened to be last. With
        # one position those were the same object; with several, popping blindly
        # frees the wrong exposure and the heat engine drifts out of step with
        # reality one trade at a time.
        try:
            idx = self.open_trades.index(t)
        except ValueError:
            idx = -1
        if 0 <= idx < len(self.risk.open_risks):
            self.risk.open_risks.pop(idx)
            self.risk.open_directions.pop(idx)
        elif self.risk.open_risks:
            self.risk.open_risks.pop()
            self.risk.open_directions.pop()
        # ACCOUNT R vs POSITION R. `total` is R against this trade's own stop and
        # stays the ledger's realised_r, because every cohort, hypothesis and
        # arm comparison in the desk is denominated that way. What the account
        # actually gained or lost is that scaled by the size the trade was given.
        # The daily-loss limit and portfolio heat are account-level questions, so
        # they use the scaled figure; a 2R-sized loser must consume 2R of the
        # day's budget or the limit does not mean what it says.
        account_r = total * t.risk_r
        self.risk.day_loss_r += min(0.0, account_r)
        self.stats.exits += 1
        if resolution is Resolution.TICK_OBSERVED:
            self.stats.exits_tick_resolved += 1
        elif resolution is Resolution.M1_OBSERVED:
            self.stats.exits_m1_resolved += 1
        elif resolution is Resolution.BAR_UNAMBIGUOUS:
            self.stats.exits_bar_unambiguous += 1
        elif resolution is Resolution.MANAGED_EXIT:
            self.stats.exits_managed += 1
        else:
            self.stats.exits_assumed += 1

        flag = ("\n_exit price ASSUMED (deciding bar touched both stop and "
                "target; stop-first assumed) — uncertain_"
                if resolution.is_assumption else "")
        self._notify(
            f"*EXIT* {t.position.direction} {t.signal.setup.value} — {reason}\n"
            f"realised `{total:+.2f}R` net (gross `{gross:+.2f}R` - cost "
            f"`{cost_r:.3f}R`)\n"
            f"MFE `{t.mfe_r:+.2f}R` · MAE `{t.mae_r:+.2f}R` · "
            f"capture `{(total / t.mfe_r if t.mfe_r > 0 else 0):.0%}` of MFE\n"
            f"resolution `{resolution.value}` · {t.observer.ticks} observations"
            f"{flag}")

        self.ledger.append_raw({
            "kind": "TRADE_CLOSED", "ts": ts.isoformat(),
            "entry_t0": t.position.opened_utc.isoformat(),
            "context": t.entry_context, "mechanism_name": t.mechanism_name,
            "direction": t.position.direction, "setup": t.signal.setup.value,
            "realised_r": round(total, 4), "gross_r": round(gross, 4),
            "account_r": round(account_r, 4), "risk_r": round(t.risk_r, 4),
            "sizing_basis": t.sizing_basis,
            "cost_r": round(cost_r, 4), "reason": reason,
            "resolution": resolution.value,
            "mfe_r": round(t.mfe_r, 4), "mae_r": round(t.mae_r, 4),
            "forgone_r": round(max(0.0, t.mfe_r - total), 4),
            # WHEN the extremes happened, not just how big they were. Path
            # prediction needs the ordering and the timing — a trade that peaks
            # in 20 minutes and one that peaks in 6 hours want different
            # management, and MFE alone cannot tell them apart.
            "t_mfe": round(t.t_mfe, 1), "t_mae": round(t.t_mae, 1),
            "observations": t.observer.ticks,
            # The excursion PATH, downsampled. Without it a management
            # counterfactual is impossible: the shadow log records what each
            # policy would have CHOSEN, and only the path says what that choice
            # would have PRODUCED. Extremes are always kept so the replay cannot
            # miss the moment a level was crossed.
            "path": _downsample_path(t.observer.path),
            "management": t.mgmt_log,
            "management_policy": self.active_chooser().name,
            "management_authority": ("operator" if self._management_override
                                     else "durable-binding"),
            "reentry_policy": self.active_reentry().version,
            "vision": self.vision.value})

        self.prior = PriorTrade(
            direction=t.position.direction, exit_reason=reason, realised_r=total,
            mfe_r=t.mfe_r, mae_r=t.mae_r, exited_utc=ts,
            thesis_still_intact=(st is not None and st.trend_direction ==
                                 ("UP" if t.position.long else "DOWN")))
        if t in self.open_trades:
            self.open_trades.remove(t)

    # -- journalling -------------------------------------------------------
    def _snapshot(self, bars: Sequence[Bar], i: int, brief):
        """A causal snapshot of this decision moment, for the model league.

        Every observation is stamped with the instant it became KNOWABLE, and
        `SnapshotBuilder` refuses anything after `as_of` — so a competitor
        handed this state later is provably seeing what the desk saw and not one
        field more. Bars go in through `add_bars`, which drops the forming
        candle: bar `i` closes AT `bars[i].ts`, and the one after it has not
        happened.
        """
        from golddesk.snapshot import SnapshotBuilder
        as_of = bars[i].ts
        b = SnapshotBuilder(brief.symbol, ENTRY_TF, as_of)
        b.add_bars("entry", bars[:i + 1], ENTRY_TF, count=40)
        for key, val in (("bid", brief.bid), ("ask", brief.ask),
                         ("spread", brief.spread), ("atr", brief.atr)):
            if val is not None:
                b.add(key, float(val), as_of, source="feed")
        b.add("session", brief.session, as_of, source="calendar")
        for lv in getattr(brief, "levels", ()) or ():
            price = getattr(lv, "price", None)
            if price is not None:
                b.add(f"level.{lv.id}", float(price), as_of, source="structure")
        return b.build()

    @staticmethod
    def _trend_ctx(brief) -> dict:
        # Attaches the keys golddesk/quant_findings.py's sealed hypotheses
        # select on (quant-trend-strength-high-v1 and the prior-NY-session
        # finding). Without this a hypothesis's own selector never matches a
        # ledger row and accrues post_n=0 forever — see quant_findings.py's
        # module docstring.
        out = {}
        if brief.trend is not None:
            out["trend_strength_bucket"] = strength_bucket(brief.trend.strength)
        if brief.day_state is not None:
            out["prior_ny_session_state"] = brief.day_state.value
        return out

    def _record(self, bars, i, brief, kind, by, decision, reason, direction,
                risk_price, suffix: str = ""):
        # `suffix` keeps decision ids unique when one bar produces several
        # records — the universe path emits one per candidate at the same
        # timestamp, and a colliding id would silently overwrite the very
        # counterfactuals it exists to preserve.
        did = f"{brief.symbol}-{bars[i].ts.isoformat()}" + (f"-{suffix}" if suffix else "")
        # FORWARD RESOLUTION WINDOW.
        #
        # This was 61 bars — about fifteen hours on M15 — and that was a quiet,
        # systematic bias against the objective. Every refusal is resolved
        # forward to answer "what would saying no have cost", and a truncated
        # window answers it as ZERO for anything that paid off later. Missed
        # positive-EV opportunity is an economic cost in this desk, so
        # understating it understates the price of every DISCRETIONARY
        # restriction, and every restriction's counterfactual review is
        # consequently biased toward KEEPING it. A cap on how far forward we are
        # willing to look is a cap on how expensive a gate is allowed to appear.
        #
        # The window is now long enough that the answer is about the market
        # rather than about the window. It costs bars in the path hash and
        # nothing at decision time — resolve_forward runs on already-loaded
        # data, after the decision, and cannot affect it.
        fwd = bars[i:i + self.forward_bars + 1]
        if len(fwd) < self.forward_bars + 1:
            # Near the end of the available series the window is short, and a
            # short window UNDERSTATES forgone value. Recorded so the analysis
            # can discount it rather than reading a truncated resolution as a
            # genuine zero.
            log.debug("forward window truncated to %d bars at %s — forgone value "
                      "from here is a LOWER BOUND", len(fwd), bars[i].ts)
        lb = [LBar(b.ts, b.open, b.high, b.low, b.close) for b in fwd]
        # THE SNAPSHOT'S TWO IDENTIFIERS TRAVEL WITH THE DECISION. state_id says
        # WHICH MOMENT and is what a paired comparison joins on; content_hash
        # says WHAT WAS SHOWN, and without it two arms can join cleanly having
        # been given different facts. Carried in `context` because that is what
        # the ledger already persists per row.
        snap_keys = {}
        if getattr(self, "_pending_snapshot", None) is not None:
            s = self._pending_snapshot
            snap_keys = {"state_id": s.state_id, "content_hash": s.content_hash}
        self.ledger.append(DecisionRecord(
            decision_id=did, kind=kind,
            t0=bars[i].ts, symbol=brief.symbol,
            context=dict(brief.context.__dict__)
            | {"session": brief.session} | self._trend_ctx(brief)
            | snap_keys,
            brief_render=brief.render(), decided_by=by, decision=decision,
            reason=reason, path_ref=PathRef.of(brief.symbol, ENTRY_TF, lb),
            outcome=resolve_forward(lb, bars[i].ts, bars[i].close, direction, risk_price)))
