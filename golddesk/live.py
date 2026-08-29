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

import json
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
from .providers import (AnalystError, AnalystProvider, ClaudeCodeAnalyst,
                        ProviderRead)
from .quant_findings import strength_bucket
from .reentry import PriorTrade
from .runner import RiskLimits, RiskState, build_brief, risk_check
from .watcher import Watcher

log = logging.getLogger(__name__)


def _explain_analyst_error(err: Exception) -> dict:
    """Pull the CLI's own verdict out of its JSON error payload.

    The Claude Code CLI reports failure as a JSON blob, and the informative
    fields (`subtype`, `result`, and the token counts that say whether the API
    was ever reached) sit at arbitrary offsets inside it. Blind truncation of
    the whole string reliably keeps the useless half.

    Returns {} when there is no JSON to read -- an absent explanation, never a
    guessed one.
    """
    text = str(err)
    # THE LOGIN IS CHECKED FIRST AND OUTSIDE THE JSON. When the provider
    # recognises an expired OAuth session it raises a plain sentence with the
    # remedy in it and no JSON payload at all, so a JSON-first reader would have
    # returned {} for the one failure whose cause is fully known. It is also
    # checked on the raw text rather than a parsed field because the CLI reports
    # this with subtype "success" and api_error_status null -- `result` is the
    # only field that says anything true.
    # QUOTA FIRST. A session-limit rejection is byte-identical to a rejected
    # flag and to an expired login on every field below -- exit 1,
    # duration_api_ms 0, zero tokens -- so the generic reading fired and told
    # the operator "this is a LOCAL failure (input, login or binary), NOT a
    # limit or an outage" about a row whose own `result` read "You've hit your
    # session limit - resets 8:10pm". Confidently wrong, in the one field
    # anybody would read.
    # AUTH IS CHECKED FIRST HERE, and the order is the opposite of the
    # provider's on purpose. There the input is the CLI's own output; here it is
    # the desk's own ERROR PROSE, which for the auth case says "THIS IS NOT A
    # FLAG, A RATE LIMIT OR AN OUTAGE" -- and a substring scan for quota markers
    # matched "rate limit" inside the sentence denying one. The desk's
    # explanation of a fault was being read as evidence of a different fault.
    #
    # Auth wins because it is the more specific claim: _auth_failure requires
    # "failed to authenticate" or "oauth session expired", which a quota refusal
    # never says.
    if ClaudeCodeAnalyst._auth_failure(text, "") is not None:
        needs_login = {"needs_login": True,
                       "reading": ("the LOGIN has expired. No retry, restart or "
                                   "flag change clears this -- run `claude` once "
                                   "interactively on the box, as the task's user")}
    elif ClaudeCodeAnalyst._quota_exhausted(text, "") is not None:
        needs_login = {"quota_exhausted": True,
                       "reading": ("the subscription's SESSION LIMIT is reached. "
                                   "Not a login, not a flag, not an outage — and "
                                   "retrying spends against a limit that is "
                                   "already gone. Reads resume by themselves at "
                                   "the reset time in `result`.")}
    else:
        needs_login = {}
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return needs_login
    try:
        d = json.loads(text[i:j + 1])
    except Exception:                                  # noqa: BLE001
        return needs_login
    if not isinstance(d, dict):
        return needs_login
    usage = d.get("usage") or {}
    out = {k: d[k] for k in ("subtype", "result", "stop_reason", "is_error",
                             "num_turns", "duration_api_ms") if k in d}
    for k in ("input_tokens", "output_tokens"):
        if k in usage:
            out[k] = usage[k]
    # THE DISCRIMINATOR. Zero tokens AND zero API time means the CLI failed
    # before it ever called the API -- which rules out a rate limit, a model
    # outage and a timeout, and rules IN something local: the input it was
    # handed, the login, or the binary itself.
    if out.get("duration_api_ms") == 0 and not out.get("input_tokens"):
        out["reading"] = ("the API was never called -- this is a LOCAL failure "
                          "(input, login or binary), not a limit or an outage")
    # A KNOWN cause overrides the generic one. "local failure (input, login or
    # binary)" is three suspects; "the login expired" is a fix. Written last so
    # it wins.
    out.update(needs_login)
    return out


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

#: Consecutive unanswered wakes before the desk says out loud that it is blind.
#: THREE, not one: a single timeout is ordinary and alerting on it trains the
#: operator to ignore the channel. Three consecutive on M15 is ~45 minutes of a
#: desk that cannot see, which is no longer a blip. Not higher, because the
#: whole point is to catch an outage in the hour it starts rather than at the
#: end-of-day cycle.
BLIND_ALARM_AFTER = 3


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
      UNOBSERVED           the exit price is known and the PATH is not. The
                           observer recorded zero observations, so mfe/mae are
                           absent rather than zero and nothing about ordering
                           was seen. See _close: this is forced, never chosen.
    """
    TICK_OBSERVED = "TICK_OBSERVED"
    M1_OBSERVED = "M1_OBSERVED"
    BAR_UNAMBIGUOUS = "BAR_UNAMBIGUOUS"
    BAR_ASSUMED_STOP_FIRST = "BAR_ASSUMED_STOP_FIRST"
    MANAGED_EXIT = "MANAGED_EXIT"
    #: THE ONE THAT MUST NOT ENTER STATISTICS.
    #:
    #: OBSERVED LIVE 2026-08-28, on a real Telegram exit message:
    #:
    #:     SHADOW EXIT LONG NOVEL — STOP
    #:     realised -1.02R net
    #:     MFE +0.00R · MAE +0.00R · capture 0% of MFE
    #:     resolution TICK_OBSERVED · 0 observations
    #:
    #: Those cannot all be true. A position that travelled from entry to a full
    #: stop has an MAE of about -1R by definition, and TICK_OBSERVED asserts the
    #: ordering was seen in a tick stream that recorded nothing. The exit price
    #: is real -- the stop is a price event -- but the PATH is unknown, and the
    #: row was reporting it as measured and zero.
    #:
    #: A zero MAE on a stop-out is not a small error. It says the trade never
    #: went against the operator, which is the exact input every stop-placement
    #: and management question reads.
    UNOBSERVED = "UNOBSERVED"

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
    #: Set once the TP1 partial has been banked, so it fires exactly once per
    #: trade. Distinct from `partials` because the risk-free partial in
    #: management.options() also appends there, and re-banking at TP1 on every
    #: subsequent tick would suffocate the runner.
    tp1_banked: bool = False
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
    #: Reads served by the rule-based fallback because the analyst was
    #: unreachable. Counted SEPARATELY from `reads` on purpose -- folding them
    #: in would make the desk look busiest exactly while its analyst was dead.
    fallback_reads: int = 0
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
    #: Consecutive wakes on which the analyst did not answer; reset by any
    #: successful read. This is the number that separates "gold is quiet" from
    #: "the desk is blind", and nothing tracked it. `analyst_errors` counted the
    #: total and was READ BY NOBODY (III.16 — a counter one caller increments
    #: and none reports is not measurement, it is bookkeeping).
    consecutive_blind: int = 0
    longest_blind_streak: int = 0


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
                 crossmarket_provider=None,
                 calendar=None,
                 regime_history=None,
                 entry_urgency: float = 0.5,
                 forward_bars: int = 480,
                 macro_provider=None,
                 macro_refresh: timedelta = timedelta(hours=6),
                 macro_timeout_s: float = 25.0,
                 wake_on_bar_close: bool = False):
        self.provider, self.ledger = provider, ledger
        self.sink = sink or build_sink(None)
        self.shadow = shadow
        self.thresholds, self.cost_model = thresholds, cost_model
        self.limits, self.policy = limits, policy
        # MAXIMUM FREQUENCY. See Watcher.__init__ for why this is defensible
        # under a subscription and what it still costs.
        self.watcher = Watcher(heartbeat=heartbeat, min_gap=min_gap,
                               wake_on_bar_close=wake_on_bar_close)
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
        # MACRO. A zero-arg callable returning a MacroContext -- normally
        # macro_context.from_drivers(build_drivers(...)). None means no feed and
        # every brief then renders MACRO CONTEXT: UNMEASURED rather than
        # quietly omitting the section.
        #
        # REFRESHED ON A CADENCE, NOT PER BAR: every driver behind it is DAILY,
        # so a per-bar fetch would hammer two free feeds for a number that
        # cannot have moved, and would put an external HTTP call inside the
        # decision path where a slow response becomes a missed bar.
        #
        # A FAILED REFRESH KEEPS THE PREVIOUS READ. Losing macro is a
        # degradation, not an outage, and MacroContext reports its own age --
        # so a kept value says "72h old" where a cleared one would say "absent"
        # and throw away the fact that the fetch ever worked.
        self.macro_provider = macro_provider
        self.macro_refresh = macro_refresh
        # Deadline for ONE refresh attempt. 25s comfortably covers the FRED
        # leg's own 15s timeout plus a slow Yahoo response, and is far below any
        # bar interval, so a stuck fetch can never delay a decision past its bar.
        self.macro_timeout_s = macro_timeout_s
        self._macro = None
        self._macro_at = None
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
        # CROSS-MARKET, read from the terminal that is already connected.
        # Injected as a callable for the same reason macro_provider is: the desk
        # stays testable with no MetaTrader5 installed, and build_brief stays
        # pure. Refreshed on the SAME cadence as macro -- an hourly change
        # figure does not move between two M15 bars, and pulling four symbols on
        # every wake would put four network round trips in front of a decision.
        self.crossmarket_provider = crossmarket_provider
        self._crossmarket: Optional[str] = None
        self._crossmarket_at = None
        #: Size of the brief handed to the analyst on the last wake. Recorded on
        #: BLIND rows because it is the one input that VARIES between a wake that
        #: answers and one that does not -- and a CLI failure with zero tokens
        #: and zero API time is a local rejection, where size is the first
        #: hypothesis to rule in or out.
        self._last_prompt_chars: Optional[int] = None
        #: One login alarm per outage. Separate from consecutive_blind because
        #: the two fire on different rules -- this one on the FIRST failure,
        #: since an expired login is known immediately and cannot self-clear.
        self._login_alarm_sent = False
        #: One degraded-mode alert per outage, and one recovery notice.
        self._degraded_notified = False
        #: The rule-based reader, built on first use. A desk whose analyst
        #: cannot be reached currently produces NOTHING, and nothing is the one
        #: output that is certainly worthless -- see _fallback_read.
        self._fallback: Optional[AnalystProvider] = None
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

        SHADOW. Same deadlock as entry.fallback_min_rr, in a second place. This
        count demotes only when review() prices its counterfactual, review()
        needs resolved outcomes, and the count blocks the trades that would
        produce them -- so it never demotes. Observed 2026-08-27: the desk's
        first-ever signal fired at 11:15 and the next FOUR opportunities, three
        of them in one bar, were refused with "a trade is already open".

        In advisory mode nothing is allocated, so a count limiting concurrent
        exposure limits no exposure -- it only limits what the operator gets to
        SEE and what the ledger gets to measure. And removing it here is not
        removing the limiter: portfolio heat still binds at max_open_risk_r with
        the correlation haircut, which by this docstring's own arithmetic
        already refuses a second same-direction thesis. What becomes possible is
        an INDEPENDENT one -- the opposite-direction and uncorrelated setups
        that were being discarded unmeasured. Armed, the count enforces exactly
        as before.
        """
        from .constitution import is_enforcing
        if is_enforcing("risk.one_position") and not self.shadow:
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

        # TP1 PARTIAL BANK.
        #
        # `tp1` is computed by the compiler under the comment "partial bank",
        # journalled on the SIGNAL row, and printed on the message the operator
        # acts on -- and NOTHING in this package ever compared it to price. It
        # was decoration. `grep -rn "\.tp1" golddesk/` found four sites: compute,
        # journal, render, and the universe mirror. Zero comparisons.
        #
        # The cost is not theoretical. 2026-08-27: a short reached +1.88R with
        # TP1 at +1.78R, price traded THROUGH it, nothing banked, a trail then
        # locked +0.29R, and the pullback took that. 15% of MFE captured on a
        # call that was right.
        #
        # THE MECHANISM of the leak is that the risk-free partial in
        # management.options() is offered ONLY while `guaranteed_now < 0`. A
        # trail that reaches risk-free permanently removes the partial from the
        # option set, after which the entire position rides one stop.
        #
        # Deterministic here rather than an option for the chooser, exactly like
        # the tp2 exit immediately above: reaching a NAMED OBJECTIVE is a price
        # event, not a policy preference -- and an option the chooser may
        # decline is precisely how this got lost. Invariants still bind: the
        # bank goes through apply_option, so I3 (locked profit cannot fall) and
        # I4 (a runner must survive) are enforced exactly as for any partial.
        if (not t.tp1_banked and t.signal.tp1 is not None
                and ((exit_px >= t.signal.tp1) if long else (exit_px <= t.signal.tp1))):
            t.tp1_banked = True          # set BEFORE applying: a rejected bank
                                         # must not retry on every subsequent tick
            # FILL AT THE OBJECTIVE, NOT AT WHEREVER PRICE IS WHEN WE NOTICE.
            #
            # This passed `price`, the live quote at the moment the tick loop
            # got around to checking. Observed live 2026-08-28:
            #
            #     SHADOW TP1 BANK SHORT
            #     TP1 4600.48 reached — banked 25% at 4594.12 (+0.61R)
            #
            # For a short, 4594.12 is SIX POINTS BETTER than the objective. A
            # resting take-profit limit fills AT the limit; it does not wait and
            # then fill further in your favour. The desk was crediting itself
            # with favourable slippage on every partial, systematically, and the
            # error is largest exactly when price is moving fastest -- so the
            # mechanisms it flatters most are the volatile ones.
            #
            # t.signal.tp1 is the number the operator was SHOWN and the number a
            # real resting order carries. Execution cost is charged separately
            # by the cost model, so this is the honest fill, not an optimistic
            # one. If price gapped clean through, a limit still fills at the
            # limit -- being conservative here costs nothing real and protects
            # every downstream expectancy figure.
            self._bank_tp1(t, t.signal.tp1, ts)

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
                                     timeline, timeframe=ENTRY_TF,
                                     macro=self._macro)
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
        self._refresh_macro(ts)
        self._refresh_crossmarket(ts)

        brief = build_brief(bars, i, st, sw, bid, ask, age, htf_state, timeline,
                            macro=self._macro,
                            crossmarket=self._crossmarket,
                            timeframe=ENTRY_TF)
        try:
            self._last_prompt_chars = len(brief.render())
        except Exception:                             # noqa: BLE001
            self._last_prompt_chars = None

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
            self._record_blind(bars, i, brief, ts, "charts", e)
            return

        if self.universe_mode:
            return self._decide_universe(bars, i, brief, imgs, ts, st)

        try:
            pr = self.provider.read(brief, imgs)
            self.stats.reads += 1
            self._analyst_answered()
        except AnalystError as e:
            # DEGRADE BEFORE GOING DARK. BLIND is the only output certain to be
            # worthless; a rule-based read is weaker evidence and vastly better
            # than none. None means the fallback is off or failed too, and then
            # this books BLIND exactly as it always did.
            pr = self._fallback_read(brief, "read", e)
            if pr is None:
                self._record_blind(bars, i, brief, ts, "read", e)
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
                             self.cohorts, shadow=self.shadow)
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

    def _refresh_crossmarket(self, now) -> None:
        """Pull cross-market context if due. NEVER raises, for the same reason
        _refresh_macro does not: losing a context block is a degradation, and a
        desk that stops trading because one symbol read failed has converted a
        missing input into an outage.

        On failure the PREVIOUS block is kept and the timestamp is still
        stamped, so a broken read is retried on the cadence rather than on every
        wake -- and the analyst sees a stale block that says so, rather than a
        silently absent one.
        """
        if self.crossmarket_provider is None:
            return
        if (self._crossmarket_at is not None
                and now - self._crossmarket_at < self.macro_refresh):
            return
        self._crossmarket_at = now
        try:
            self._crossmarket = self.crossmarket_provider()
        except Exception as e:                        # noqa: BLE001
            log.warning("cross-market read failed (%s) — keeping the previous "
                        "block rather than dropping the section", e)

    # -- macro ------------------------------------------------------------
    def _refresh_macro(self, now) -> None:
        """Pull a fresh macro read if the cadence is due. NEVER raises.

        A macro fetch that throws must not take down the decision path. The
        desk's job is to read the market; losing macro is a degradation, and a
        desk that stops trading because FRED is down has converted a missing
        input into an outage.

        On error the PREVIOUS read is kept, so MacroContext can report its true
        age -- "72h old" -- rather than "absent", which would discard the fact
        that the fetch used to work and when it stopped. `_macro_at` is stamped
        even on failure so a broken feed is retried on the cadence rather than
        on every wake.
        """
        if self.macro_provider is None:
            return
        if (self._macro_at is not None
                and now - self._macro_at < self.macro_refresh):
            return
        # RUN IT WITH A HARD DEADLINE. The FRED leg carries timeout=15, but the
        # Yahoo leg goes through yfinance, which has no timeout this code sets --
        # and this call sits immediately before a decision. A FAILURE is handled
        # below; a HANG is not a failure, it is an unbounded wait inside the
        # path that produces signals, and it would look like a quiet market
        # rather than a stuck fetch. A worker thread with a join deadline bounds
        # it: on timeout the thread is abandoned (it is a read-only HTTP fetch,
        # so leaking one is harmless) and the desk carries on with the previous
        # read, which reports its own age.
        import threading                                        # noqa: PLC0415
        box = {}

        def _fetch():
            try:
                box["m"] = self.macro_provider()
            except Exception as e:                    # noqa: BLE001
                box["err"] = e

        th = threading.Thread(target=_fetch, daemon=True, name="macro-refresh")
        th.start()
        th.join(timeout=self.macro_timeout_s)
        if th.is_alive():
            log.warning("macro refresh exceeded %.0fs -- abandoning this attempt and "
                        "keeping the previous read; the next one is due in %s",
                        self.macro_timeout_s, self.macro_refresh)
            self._macro_at = now
            return
        if "err" in box:
            log.warning("macro refresh failed (%s) -- keeping the previous read, "
                        "which reports its own age", box["err"])
            self._macro_at = now
            return
        m = box.get("m")
        if m is not None:
            self._macro = m
        self._macro_at = now

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
            self._analyst_answered()
        except AnalystError as e:
            # Same degrade as the single-read path. DeterministicProvider
            # inherits AnalystProvider.survey, which wraps its read into a
            # one-candidate universe and SAYS SO -- so universe mode returns one
            # honest candidate rather than silently reporting a full enumeration.
            fb = self._fallback_read(brief, "survey", e)
            if fb is None:
                self._record_blind(bars, i, brief, ts, "survey", e)
                return
            from .universe import as_universe
            stamp, uni = fb, as_universe(fb.read)

        cands = compile_universe(brief, uni, self.thresholds, self.cost_model,
                                 self.cohorts, shadow=self.shadow)
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

        # EVIDENCE TIER, ON THE FIRST LINE. Every caveat below was already in
        # this message -- conf 2/5, "no measured edge yet for this mechanism",
        # RISK estimation HIGH, and a why_not saying in plain words "filed NOVEL
        # and expected to be shadowed rather than sized". It was scattered
        # across five places, none of them the first line, and the first line is
        # what gets read on a phone. An operator took one such experiment with
        # real money on 2026-08-27. The message was not WRONG; it was UNRANKED,
        # and an unranked caveat is one the reader has to assemble for
        # themselves at the moment they are least inclined to.
        #
        # NOT A GATE. Nothing here refuses a trade, moves a threshold, or
        # changes what reaches the ledger. Firing rate is unchanged.
        from .tiers import evidence_tier
        _stat = (self.cohorts or {}).get(mech)
        tier = evidence_tier(
            setup=sig.setup.value, mechanism_name=mech,
            confidence=sig.confidence,
            sweep_state=brief.context.sweep_state,
            reclaim_state=brief.context.reclaim_state,
            displacement_state=brief.context.displacement_state,
            htf_alignment=brief.context.htf_alignment,
            with_trend=((sig.direction == "LONG"
                         and brief.context.trend_direction == "UP")
                        or (sig.direction == "SHORT"
                            and brief.context.trend_direction == "DOWN")),
            cohort_n=(_stat.n if _stat else 0),
            cohort_ev_r=(_stat.expected_r(sig.rr_tp2, sig.cost_r)
                         if _stat and _stat.n else None))

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
                      # WAS THE STOP SIZED FOR THE VOLATILITY IT WILL MEET?
                      # Recorded, not acted on. Stops are placed off a TRAILING
                      # ATR, which lags a volatility expansion by construction --
                      # so the stop is narrowest, relative to what price is
                      # actually doing, exactly when trends travel furthest. That
                      # is a hypothesis about 2026-08-28, not a finding, and the
                      # two numbers here are what let it be settled later instead
                      # of widening a stop on the strength of one afternoon.
                      "stop_regime": self._stop_regime(bars, i, brief, sig),
                      # THE EVIDENCE BALANCE, counted rather than narrated. The
                      # analyst already writes good counter-arguments; they were
                      # prose in a paragraph, weighted by nobody. Recorded so
                      # "does a negative balance predict a worse outcome" becomes
                      # answerable -- and if it does, it can earn authority then.
                      # It scores; it does not gate.
                      "evidence_balance": self._evidence_balance(sig.direction,
                                                             brief.context),
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
                      # The tier the operator was shown, on the row itself. A
                      # ranking visible on a phone and absent from the ledger is
                      # one no later analysis can group by -- and "did T4
                      # experiments actually resolve worse than T2 signals" is
                      # the question this ranking exists to make answerable.
                      "evidence_tier": {"rank": tier.rank, "label": tier.label,
                                        "why": tier.why},
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
            f"{tier.banner}\n"
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
        # A RESOLUTION MAY NOT CLAIM WHAT THE OBSERVER DID NOT SEE.
        #
        # The stop and target paths stamp TICK_OBSERVED unconditionally, which
        # asserts the ordering was seen in a tick stream. When the observer
        # recorded ZERO observations that assertion is false, and the row went
        # out saying "MFE +0.00R - MAE +0.00R - resolution TICK_OBSERVED - 0
        # observations" -- an impossible combination on a full stop-out, since a
        # position that reached its stop had an MAE of roughly -1R by
        # definition.
        #
        # The EXIT PRICE is still real: a stop is a price event and realised_r
        # is sound. What is unknown is the PATH, so the label is downgraded and
        # the row is marked as evidence that must not be learned from. Forced
        # here rather than at each call site, because every future exit path
        # would otherwise have to remember.
        if t.observer.ticks == 0 and resolution is not Resolution.MANAGED_EXIT:
            resolution = Resolution.UNOBSERVED
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
            # QUARANTINE FLAG. False means the exit price is trustworthy and the
            # PATH is not, so this row must not reach cohort statistics, the
            # management counterfactual, or any promotion decision. Written
            # explicitly rather than inferred downstream from observations==0,
            # because every consumer would have to remember the same rule and
            # one of them would not.
            "evidence_valid": resolution is not Resolution.UNOBSERVED,
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

    def _evidence_balance(self, direction: str, st) -> dict:
        """Measured evidence for and against this direction. Never fatal."""
        try:
            from .contradiction import weigh
            return weigh(direction, st).to_dict()
        except Exception as e:                        # noqa: BLE001
            log.debug("evidence balance not recorded: %s", e)
            return {}

    def _stop_regime(self, bars, i, brief, sig) -> dict:
        """The stop's distance measured two ways: trailing ATR, and this bar.

        NEVER the reason a signal fails to record. This is measurement attached
        to a decision, and measurement that can take down the decision it
        describes is worse than no measurement.
        """
        try:
            from .candle_character import measure as _character
            from .stop_regime import measure as _regime
            bar = bars[i]
            rng = float(bar.high) - float(bar.low)
            rvm = None
            try:
                rvm = (_character(bars[max(0, i - 40):i + 1]) or {}).get("range_vs_mean")
            except Exception:                         # noqa: BLE001
                rvm = None
            return _regime(abs(sig.entry - sig.stop), brief.atr, rng, rvm)
        except Exception as e:                        # noqa: BLE001
            log.debug("stop regime not recorded: %s", e)
            return {}

    def _bank_tp1(self, t: OpenTrade, price: float, ts: datetime) -> None:
        """Bank part of the position at the objective the operator was shown.

        Routed through apply_option so the same invariants bind as for any other
        partial -- I3 (locked profit may not fall) and I4 (a runner must
        survive). A REJECTION is a legitimate outcome and is logged rather than
        forced: if banking here would leave less than the minimum runner, the
        honest answer is to leave the position alone, not to override the
        invariant that says so.
        """
        from .management import (Action, ManagementOption, apply_option)
        from .partial_policy import tp1_fraction
        pos = t.position
        # HOW MUCH, from live conditions rather than a constant. A fixed half
        # treats a young aligned trend in quiet tape exactly like an exhausted
        # one in an extreme one, and those want opposite treatment. Live
        # structure where it exists, entry context for the higher-timeframe
        # reading, which is not recomputed on the tick path.
        st = self._last_state
        ectx = t.entry_context or {}
        plan = tp1_fraction(
            trend_maturity=(getattr(st, "trend_maturity", None)
                            or ectx.get("trend_maturity") or "MID"),
            volatility_state=(getattr(st, "volatility_state", None)
                              or ectx.get("volatility_state") or "NORMAL"),
            htf_alignment=ectx.get("htf_alignment") or "NEUTRAL",
            with_trend=((pos.direction == "LONG"
                         and getattr(st, "trend_direction", None) == "UP")
                        or (pos.direction == "SHORT"
                            and getattr(st, "trend_direction", None) == "DOWN")),
            rr_tp1=t.signal.rr_tp1, rr_tp2=t.signal.rr_tp2)
        frac = plan.fraction
        exc = Excursion(t.observer.mfe_r, t.observer.mae_r, t.t_mfe, t.t_mae,
                        pos.r_at(price), 0)
        opt = ManagementOption(
            "TP1", Action.PARTIAL, None, frac, None,
            f"TP1 {t.signal.tp1:.2f} reached at {price:.2f} — bank "
            f"{frac:.0%} of the runner ({plan.why})")
        # Default ManagementPolicy: min_runner_fraction is what protects the
        # runner, and it belongs to the invariant layer rather than to whichever
        # chooser happens to be active.
        dec = apply_option(pos, opt, exc)
        if dec.rejected_reason:
            log.info("TP1 bank declined at %s: %s", ts, dec.rejected_reason)
            return
        t.position = dec.position_after
        t.observer.stop = dec.position_after.current_stop
        t.partials.append((frac, price))
        self.stats.partials += 1
        t.mgmt_log.append({"ts": ts.isoformat(), "action": "PARTIAL",
                           "source": "tp1", "at": round(price, 2),
                           "fraction": frac,
                           # The REASONS travel with the decision, so a later
                           # analysis can ask whether banking more in EXHAUSTED
                           # actually beat banking less -- per mechanism, from
                           # evidence rather than from partial_policy's opinion.
                           "fraction_why": plan.why,
                           "partial_policy": plan.version,
                           "banked_r": round(dec.banked_now_r, 4),
                           "locked_r": round(dec.position_after.locked_r, 4)})
        self._notify(
            f"*TP1 BANK* {pos.direction} {t.signal.setup.value}\n"
            f"TP1 `{t.signal.tp1:.2f}` reached — banked "
            f"`{frac:.0%}` at `{price:.2f}` "
            f"(`{dec.banked_now_r:+.2f}R`)\n"
            f"runner `{dec.position_after.remaining_fraction:.0%}` stays on for "
            f"TP2 `{t.signal.tp2:.2f}`, SL `{dec.position_after.current_stop:.2f}`\n"
            f"locked `{dec.position_after.locked_r:+.2f}R`\n"
            f"_size from live conditions: {plan.why}_")

    def _record_blind(self, bars, i, brief, ts, stage: str, err: Exception) -> None:
        """Journal a bar the analyst never answered on.

        Every one of these call sites used to `log.warning(...)` and `return`,
        which left NO ROW. The consequence is not a missing log line — it is that
        `state/ledger.jsonl`, the single artifact every downstream measurement
        reads, could not tell a session the desk spent DECLINING from a session
        it spent BLIND. Three ledger rows over a live window reads as a
        disciplined desk seeing nothing worth taking; it was in fact an analyst
        that timed out. Those are opposite facts and they had the same file.

        Filed as BLIND, never REFUSAL_* — see DecisionKind.BLIND for why the name
        is load-bearing. Direction is "NONE" because nothing formed a view; the
        forward path is still resolved, so an outage window can be priced later
        without pretending the desk chose to sit it out.
        """
        # THE ERROR TEXT, KEPT LONG ENOUGH TO NAME THE CAUSE.
        #
        # This truncated at 500 chars and cut every CLI failure off mid-field --
        # in production the ledger held `..."cache_creation":{...},"inferenc` and
        # nothing after, for a day, while the field that explains the failure sat
        # just past the cut. providers.py already carries 2000 chars WITH A
        # COMMENT saying 300 was doing exactly this; I reintroduced the same
        # defect one layer out, in the row that exists to make the failure
        # diagnosable.
        detail = _explain_analyst_error(err)
        self.stats.analyst_errors += 1
        self.stats.consecutive_blind += 1
        self.stats.longest_blind_streak = max(self.stats.longest_blind_streak,
                                              self.stats.consecutive_blind)
        log.warning("analyst unavailable at %s (%s): %s", ts, stage, err)
        try:
            self._record(bars, i, brief, DecisionKind.BLIND, "NONE",
                         {"stage": stage, "error_type": type(err).__name__,
                          "error": str(err)[:2000],
                          # The CLI's own verdict, lifted out of its JSON so the
                          # cause is readable without hunting through a payload.
                          "cli": detail,
                          # PROMPT SIZE, because it is the variable that changes
                          # between a wake that answers and one that does not.
                          # A failure with zero tokens and zero API time is a
                          # LOCAL rejection, and size is the first thing to rule
                          # in or out -- without it the question stays open for
                          # as long as it takes to reproduce.
                          "prompt_chars": self._last_prompt_chars,
                          "vision": self.vision.value,
                          "consecutive_blind": self.stats.consecutive_blind},
                         f"BLIND: analyst unavailable at {stage} — {type(err).__name__}",
                         "NONE", brief.atr)
        except Exception as e:                        # noqa: BLE001
            # The journal must never be the reason a bar takes the desk down.
            # An unrecorded blind bar is bad; a crashed loop is worse.
            log.warning("blind row not journalled at %s: %s", ts, e)
        # THE ALARM, sent ONCE per outage rather than per bar. A blind desk and
        # a quiet market look identical from the outside — silence — which is
        # how a provider that had been timing out for hours read as discipline.
        # It is not a per-bar alert: on M15 that would be four messages an hour
        # for as long as the outage lasts, and an alert channel that cries every
        # bar is one nobody reads. The recovery notice matters as much as the
        # alarm: without it the last thing the operator ever heard was that the
        # desk was down.
        # AN EXPIRED LOGIN DOES NOT WAIT FOR THE THIRD WAKE. The three-wake
        # threshold exists because a single timeout is ordinary and self-clears;
        # a login does neither. It is known on the FIRST failure, it cannot
        # resolve on its own, and the only person who can clear it is the one
        # holding the browser — so waiting ~45 minutes to say so is 45 minutes
        # of a blind desk bought for nothing. Sent once per outage, like the
        # generic alarm, and reset by _analyst_answered.
        if detail.get("needs_login") and not self._login_alarm_sent:
            self._login_alarm_sent = True
            self._notify(
                "*ANALYST LOGGED OUT* — the Claude CLI's OAuth session expired "
                "and could not refresh. This is NOT a rate limit, an outage or "
                "a bug, and NOTHING the desk does will clear it: it will book "
                "BLIND on every bar until somebody logs in.\n\n"
                "On the VPS, as the user the scheduled task runs as:\n"
                "`claude` — then complete the browser login.\n\n"
                "No restart needed; the next wake reads normally.")
        elif self.stats.consecutive_blind == BLIND_ALARM_AFTER:
            self._notify(
                f"*ANALYST DOWN* — {BLIND_ALARM_AFTER} consecutive wakes with no "
                f"read ({stage}: {type(err).__name__}). The desk is BLIND, not "
                f"quiet: it is not declining trades, it is not seeing them. "
                f"Every bar from here is journalled BLIND until it answers.")

    #: Whether an unreachable analyst falls back to the rule-based reader.
    #:
    #: ON, because BLIND is the one output guaranteed to be worth nothing. When
    #: the CLI's login expires the desk currently books BLIND on every bar and
    #: the operator -- who places every trade by hand on this advisory desk --
    #: gets silence, which is indistinguishable from a quiet market. A
    #: rule-based read is worse evidence than a model read and enormously better
    #: than none.
    #:
    #: WHAT KEEPS IT HONEST. The read is stamped provider="deterministic",
    #: model="rules-v1", degraded=True with the reason, so it never enters the
    #: analyst's cohort, never counts as an answered wake in analyst_health, and
    #: is separable in every later analysis. The operator is told once when the
    #: desk drops to it and once when the analyst returns. It is a FALLBACK, not
    #: a substitution: the moment the analyst answers, this stops being used.
    fallback_when_blind: bool = True

    def _fallback_read(self, brief, stage: str,
                       err: Exception) -> Optional[ProviderRead]:
        """A rule-based read when the analyst cannot be reached, or None.

        Returns None -- and the caller books BLIND exactly as before -- when the
        fallback is disabled or itself fails. A fallback that quietly failed
        would turn an outage into a different outage with no record of either.
        """
        if not self.fallback_when_blind:
            return None
        try:
            if self._fallback is None:
                from .providers import DeterministicProvider
                self._fallback = DeterministicProvider()
            pr = self._fallback.read(brief)
        except Exception as e:                        # noqa: BLE001
            log.warning("fallback reader failed at %s too: %s", stage, e)
            return None
        usage = dict(pr.usage)
        # THE LABEL IS THE WHOLE SAFETY ARGUMENT. Without it a degraded read is
        # byte-identical to a model read in every downstream count, and the desk
        # would look healthiest exactly while its analyst was dead.
        usage.update({"degraded": True,
                      "degraded_from": getattr(self.provider, "name", "?"),
                      "degraded_stage": stage,
                      "degraded_because": str(err)[:300]})
        self.stats.fallback_reads += 1
        if not self._degraded_notified:
            self._degraded_notified = True
            self._notify(
                "*DESK ON THE RULE-BASED ARM* — the analyst is unreachable "
                f"({type(err).__name__} at {stage}), so signals are coming from "
                "the desk's own rules instead of a model read.\n\n"
                "These are WEAKER reads: no context, no macro, no judgement — "
                "structure only. They are labelled `deterministic/rules-v1` in "
                "the ledger and are kept out of the analyst's evidence.\n\n"
                "Better than silence, which is what you were getting. You will "
                "be told the moment the analyst answers again.")
        return ProviderRead(pr.read, pr.provider, pr.model, pr.latency_ms, usage)

    def _analyst_answered(self) -> None:
        """Called on every successful read. Closes an open outage."""
        if (self.stats.consecutive_blind >= BLIND_ALARM_AFTER
                or self._login_alarm_sent or self._degraded_notified):
            self._notify(f"*ANALYST BACK* — reading again after "
                         f"{self.stats.consecutive_blind} blind wakes"
                         + (f" and {self.stats.fallback_reads} rule-based read(s)"
                            if self._degraded_notified else "") + ".")
        self.stats.consecutive_blind = 0
        self._login_alarm_sent = False
        self._degraded_notified = False

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
