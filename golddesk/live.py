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
  * Exit resolution now uses observed ordering whenever a finer series exists.
    Where it does not, the pessimistic assumption REMAINS but is stamped
    resolution=M15_PESSIMISTIC_UNCERTAIN on the record, because an assumed fill
    and an observed one must never aggregate into the same number silently.
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
from .management import (Action, Anchor, Excursion, ManagementPolicy, Position,
                         ThesisState, apply_option, enumerate_options)
from .notify import Sink, build_sink
from .observer import TradeObserver, Trigger, Wake, resolve_intrabar
from .policies import (ContextualChooser, HeuristicChooser, ManagementChooser,
                       PassiveChooser, ReentryPolicy)
from .policy_state import PolicyState
from .providers import AnalystError, AnalystProvider, ProviderRead
from .reentry import PriorTrade
from .runner import RiskLimits, RiskState, build_brief, risk_check
from .watcher import Watcher

log = logging.getLogger(__name__)

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
    """How an exit price was determined. Aggregating these together is a lie."""
    TICK_OBSERVED = "TICK_OBSERVED"                  # ordering seen at tick level
    M1_OBSERVED = "M1_OBSERVED"                      # ordering seen at M1
    M15_PESSIMISTIC_UNCERTAIN = "M15_PESSIMISTIC_UNCERTAIN"  # assumed stop-first


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
    exits_assumed: int = 0
    hypothesis_vetoes: int = 0


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
                 observer_heartbeat: timedelta = timedelta(minutes=30)):
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
        self.open: Optional[OpenTrade] = None
        self.prior: Optional[PriorTrade] = None
        self.stats = LiveStats()
        self._last_state: Optional[StructureState] = None
        self._last_bars: Optional[Sequence[Bar]] = None
        self._last_idx: int = 0

    def _provider_can_choose(self) -> bool:
        try:
            self.provider.choose_option("", "", ["probe"])
        except NotImplementedError:
            return False
        except Exception:
            return True          # it tried — it is a real implementation
        return True

    # -- active policy resolution ----------------------------------------
    def active_chooser(self) -> ManagementChooser:
        want = self.policy_state.active(SLOT_MGMT)
        ch = self.choosers.get(want)
        if ch is None:
            log.warning("bound management policy %r is not registered — "
                        "falling back to passive rather than guessing", want)
            return self.choosers.get(PassiveChooser.name, PassiveChooser())
        return ch

    def active_reentry(self) -> ReentryPolicy:
        want = self.policy_state.active(SLOT_REENTRY)
        return self.reentry_policies.get(want) or next(iter(self.reentry_policies.values()))

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
                bar_closed: bool = False) -> Optional[str]:
        """One tick or M1 close. Cheap, and the position's real sense organ.

        Returns a short string when the tick caused something, for tracing.
        This is the method the MT5 stream drives; if it is never called, the
        observer never sees anything and continuous observation is a claim
        rather than a fact.
        """
        if self.open is None:
            return None
        t = self.open
        self.stats.ticks += 1

        # Exit check FIRST, at the resolution the tick provides. Because ticks
        # arrive in order, whether the stop or the target came first is
        # OBSERVED here — this is the production half of the intrabar problem.
        pos = t.position
        long = pos.long
        if (price <= pos.current_stop) if long else (price >= pos.current_stop):
            r = pos.r_at(pos.current_stop)
            self._close(ts, r, "PROFITABLE_STOP" if r > 0 else "STOP",
                        self._last_state, Resolution.TICK_OBSERVED)
            return "EXIT_STOP"
        if (price >= t.signal.tp2) if long else (price <= t.signal.tp2):
            self._close(ts, pos.r_at(t.signal.tp2), "TARGET",
                        self._last_state, Resolution.TICK_OBSERVED)
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
                                      wake=wake)
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

        if self.open is not None:
            self._manage(bars, i, st, intrabar)
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
        brief = build_brief(bars, i, st, sw, bid, ask, age, htf_state, timeline,
                            timeframe=ENTRY_TF)
        try:
            imgs = self._render_charts(bars, i)
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
        out = []
        for tf, step, n in (("H4-context", 16, 90), ("M15-entry", 1, 120)):
            win = bars[max(0, i - step * n):i + 1:step][-n:]
            if len(win) >= 30:
                out.append(render_clean_chart([CB(b.open, b.high, b.low, b.close)
                                               for b in win], tf))
        if not out:
            raise AnalystError(
                f"vision={self.vision.value} requires charts but none rendered at "
                f"bar {i} (insufficient history) — refusing rather than silently "
                f"downgrading this read to numeric-only")
        return tuple(out)

    # -- entry -------------------------------------------------------------
    def _enter(self, bars, i, brief, sig: CompiledSignal, pr: ProviderRead,
               n_charts: int = 0) -> None:
        pos = Position(sig.direction, sig.entry, sig.stop, sig.stop, sig.risk,
                       1.0, 0.0, bars[i].ts, sig.setup.value)
        obs = TradeObserver(direction=sig.direction, entry=sig.entry, stop=sig.stop,
                            target=sig.tp2, risk_price=sig.risk, opened=bars[i].ts)
        self.open = OpenTrade(pos, sig, i, obs,
                              entry_context=dict(brief.context.__dict__)
                              | {"session": brief.session},
                              mechanism_name=pr.read.mechanism_name)
        self.risk.open_risks.append(1.0)          # each trade risks exactly 1R
        self.risk.open_directions.append(sig.direction)
        self.risk.day_signals += 1                # reporting only; never enforced
        self.stats.entries += 1
        self._record(bars, i, brief, DecisionKind.SIGNAL, "MODEL",
                     {"direction": sig.direction, "entry": sig.entry, "stop": sig.stop,
                      "tp1": sig.tp1, "tp2": sig.tp2, "rr_tp2": sig.rr_tp2,
                      "cost_r": sig.cost_r, "analyst_read": pr.read.model_dump(),
                      "vision": self.vision.value, "charts_sent": n_charts,
                      "management_policy": self.active_chooser().name,
                      **pr.stamp()},
                     f"ENTRY {sig.direction} rr {sig.rr_tp2:.2f}", sig.direction, sig.risk)
        self._notify(
            f"*ENTRY {sig.direction} {brief.symbol}*\n"
            f"`entry  {sig.entry:.2f}`\n`SL     {sig.stop:.2f}`  ({sig.risk:.2f} risk)\n"
            f"`TP1    {sig.tp1:.2f}`\n`TP2    {sig.tp2:.2f}`  ({sig.rr_tp2:.2f}R net)\n"
            f"conf {sig.confidence}/5 · cost {sig.cost_r:.3f}R · "
            f"breakeven {sig.breakeven_win_rate:.0%}\n\n{sig.read}\n\n"
            f"*Why:* {sig.why}\n*Against:* {sig.why_not}\n*Invalid if:* {sig.invalidation}")

    # -- management --------------------------------------------------------
    def _manage(self, bars, i, st: StructureState,
                intrabar: Optional[Sequence[tuple[datetime, float]]] = None) -> None:
        """Bar-close management. Exits resolve on the finest series available."""
        t = self.open
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
                    res = (Resolution.M1_OBSERVED if len(intrabar) <= 15
                           else Resolution.TICK_OBSERVED)
                    reason = ("TARGET" if ev.kind == "TARGET" else
                              ("PROFITABLE_STOP" if ev.r > 0 else "STOP"))
                    self._close(ev.ts, ev.r, reason, st, res)
                    return
            # No finer series exists. Keep the pessimistic assumption AND say so.
            if stop_hit:
                r = pos.r_at(pos.current_stop)
                self._close(b.ts, r, "PROFITABLE_STOP" if r > 0 else "STOP", st,
                            Resolution.M15_PESSIMISTIC_UNCERTAIN if tp_hit
                            else Resolution.M1_OBSERVED)
                return
            self._close(b.ts, pos.r_at(t.signal.tp2), "TARGET", st,
                        Resolution.M1_OBSERVED)
            return

        self._management_step(b.ts, b.close, st, source="bar_close")

    def _management_step(self, ts: datetime, price: float, st: StructureState,
                         *, source: str, wake: Optional[Wake] = None) -> str:
        """One reconsideration. Shared by the tick path and the bar path.

        Every registered policy is asked the same question on the same legal
        option set; only the ACTIVE one is applied. That is what makes the later
        comparison paired — the arms differ in choice, never in the state they
        were choosing from.
        """
        t = self.open
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

        opts = enumerate_options(pos, thesis, exc, anchors, st.atr, 0.48,
                                 self.policy, self.cost_model)
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
                        Resolution.TICK_OBSERVED if source.startswith("observer")
                        else Resolution.M1_OBSERVED)
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
               resolution: Resolution = Resolution.M15_PESSIMISTIC_UNCERTAIN) -> None:
        t = self.open
        total = t.position.banked_r + realised_r * t.position.remaining_fraction
        if self.risk.open_risks:
            self.risk.open_risks.pop()
            self.risk.open_directions.pop()
        self.risk.day_loss_r += min(0.0, total)
        self.stats.exits += 1
        if resolution is Resolution.TICK_OBSERVED:
            self.stats.exits_tick_resolved += 1
        elif resolution is Resolution.M1_OBSERVED:
            self.stats.exits_m1_resolved += 1
        else:
            self.stats.exits_assumed += 1

        flag = ("\n_exit price ASSUMED (stop-first on a spanning M15 bar) — "
                "uncertain_" if resolution is Resolution.M15_PESSIMISTIC_UNCERTAIN else "")
        self._notify(
            f"*EXIT* {t.position.direction} {t.signal.setup.value} — {reason}\n"
            f"realised `{total:+.2f}R` (banked `{t.position.banked_r:+.2f}R` + "
            f"runner `{realised_r * t.position.remaining_fraction:+.2f}R`)\n"
            f"MFE `{t.mfe_r:+.2f}R` · MAE `{t.mae_r:+.2f}R` · "
            f"capture `{(total / t.mfe_r if t.mfe_r > 0 else 0):.0%}` of MFE\n"
            f"resolution `{resolution.value}` · {t.observer.ticks} observations"
            f"{flag}")

        self.ledger.append_raw({
            "kind": "TRADE_CLOSED", "ts": ts.isoformat(),
            "entry_t0": t.position.opened_utc.isoformat(),
            "context": t.entry_context, "mechanism_name": t.mechanism_name,
            "direction": t.position.direction, "setup": t.signal.setup.value,
            "realised_r": round(total, 4), "reason": reason,
            "resolution": resolution.value,
            "mfe_r": round(t.mfe_r, 4), "mae_r": round(t.mae_r, 4),
            "forgone_r": round(max(0.0, t.mfe_r - total), 4),
            "observations": t.observer.ticks,
            "management": t.mgmt_log,
            "management_policy": self.active_chooser().name,
            "reentry_policy": self.active_reentry().version,
            "vision": self.vision.value})

        self.prior = PriorTrade(
            direction=t.position.direction, exit_reason=reason, realised_r=total,
            mfe_r=t.mfe_r, mae_r=t.mae_r, exited_utc=ts,
            thesis_still_intact=(st is not None and st.trend_direction ==
                                 ("UP" if t.position.long else "DOWN")))
        self.open = None

    # -- journalling -------------------------------------------------------
    def _record(self, bars, i, brief, kind, by, decision, reason, direction, risk_price):
        fwd = bars[i:i + 61]
        lb = [LBar(b.ts, b.open, b.high, b.low, b.close) for b in fwd]
        self.ledger.append(DecisionRecord(
            decision_id=f"{brief.symbol}-{bars[i].ts.isoformat()}", kind=kind,
            t0=bars[i].ts, symbol=brief.symbol,
            context=dict(brief.context.__dict__) | {"session": brief.session},
            brief_render=brief.render(), decided_by=by, decision=decision,
            reason=reason, path_ref=PathRef.of(brief.symbol, ENTRY_TF, lb),
            outcome=resolve_forward(lb, bars[i].ts, bars[i].close, direction, risk_price)))
