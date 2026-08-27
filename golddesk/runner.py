"""Aurum v2 shadow runner — the wired path, end to end.

    bars -> features -> watcher -> analyst -> compiler -> router -> risk
         -> ledger -> management -> forward resolution

build_brief() below is a real implementation, not a seam. It needs one thing
from the host desk: a BarSource. Two are provided — ParquetBarSource (used in
the end-to-end run) and MT5BarSource, which wraps the desk's EXISTING Feed
rather than reimplementing MT5.

    ADAPTER CONTRACT — the only integration work left:
    give MT5BarSource a Feed exposing
        feed.bars(timeframe: str, count: int) -> sequence with
            .time/.ts, .open, .high, .low, .close  (CLOSED bars only)
        feed.tick() -> object with .bid, .ask, .time
    If golddesk.feed.Feed already spells these differently, map them in
    MT5BarSource.__init__ — that is a rename, not a redesign.

SHADOW MODE IS THE DEFAULT. ShadowRunner decides nothing and sends nothing to a
live channel; it fills the ledger so the frozen A/B/C/D harness has real states
to evaluate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional, Protocol, Sequence

from .analyst import (AnalystRead, CompiledSignal, Context, Level, LevelKind,
                      MarketBrief, Refusal, Setup, Thresholds, compile_signal)
from .costs import CostModel
from .features import (Bar, StructureState, atr, classify, session_of, swings,
                       visible_swings)
from .candle_character import block as candle_character_block
from .flows import load as flows_load
from .day_state import read as day_state_read
from .gold_trend import read as gold_trend_read
from .macro_context import MacroContext
from .ledger import (Bar as LBar, DecisionKind, DecisionRecord, Ledger, PathRef,
                     resolve_forward)
from .notify import Sink, build_sink
from .watcher import Watcher

log = logging.getLogger(__name__)

#: Where the flows collector leaves its cache. Read on the decision path, never fetched there:
#: a decision must not wait on -- or fail because of -- a third-party website.
FLOWS_CACHE = Path(__file__).resolve().parent.parent / "state" / "flows.json"


# --------------------------------------------------------------------------
# Bar sources
# --------------------------------------------------------------------------

class BarSource(Protocol):
    def bars(self) -> Sequence[Bar]: ...
    def quote(self, at: Bar) -> tuple[float, float, float]: ...   # bid, ask, tick_age_s


@dataclass
class ParquetBarSource:
    """Reads the desk's own cache. Used for the end-to-end run on real gold."""
    path: str
    symbol: str = "XAUUSD"
    timeframe: str = "D1"
    digits: int = 2
    drop_last: bool = True          # the cache was seen persisting forming bars

    def bars(self) -> list[Bar]:
        import pandas as pd
        df = pd.read_parquet(self.path).sort_index()
        out = [Bar(ts.to_pydatetime(), r.open, r.high, r.low, r.close,
                   float(r.volume), float(r.spread) * (10 ** -self.digits))
               for ts, r in df.iterrows()]
        out = out[:-1] if self.drop_last else out
        return _largest_contiguous_block(out)

    def quote(self, at: Bar) -> tuple[float, float, float]:
        sp = at.spread if at.spread > 0 else 0.48     # venue default when unrecorded
        return at.close - sp / 2, at.close + sp / 2, 0.0


def _largest_contiguous_block(bars: list[Bar], max_gap_days: int = 10) -> list[Bar]:
    """Drop orphan fragments. The cache carries 50 bars from 2007 stranded 10.5
    years before the main block; any rolling window spanning that join mixes
    $650 gold with $1,300 gold and silently corrupts ATR, swings and regime."""
    if len(bars) < 2:
        return bars
    blocks, cur = [], [bars[0]]
    for prev, b in zip(bars, bars[1:]):
        if (b.ts - prev.ts).days > max_gap_days:
            blocks.append(cur); cur = []
        cur.append(b)
    blocks.append(cur)
    return max(blocks, key=len)


class MT5BarSource:
    """Wraps the desk's existing Feed. NOT EXERCISED HERE — no MT5 in this env.

    This deliberately does not import MetaTrader5. golddesk.feed.Feed already
    owns the terminal connection, the server-clock offset and closed-bar
    discipline; duplicating that would create a second source of truth.
    """
    def __init__(self, feed, timeframe: str = "M15", count: int = 500):
        self._feed, self._tf, self._count = feed, timeframe, count

    def bars(self) -> list[Bar]:
        raw = self._feed.bars(self._tf, self._count)      # ADAPTER CONTRACT
        out = []
        for r in raw:
            ts = getattr(r, "ts", None) or getattr(r, "time")
            if not isinstance(ts, datetime):
                ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            out.append(Bar(ts, float(r.open), float(r.high), float(r.low),
                           float(r.close)))
        return out

    def quote(self, at: Bar) -> tuple[float, float, float]:
        t = self._feed.tick()                              # ADAPTER CONTRACT
        ts = getattr(t, "time", None)
        age = 0.0
        if isinstance(ts, datetime):
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        return float(t.bid), float(t.ask), age


# --------------------------------------------------------------------------
# build_brief — real, no seam
# --------------------------------------------------------------------------

def _prior_day_bars(bars: Sequence[Bar], i: int) -> list[Bar]:
    """Every bar of the most recent calendar day STRICTLY BEFORE bars[i]'s day.

    CAUSAL, like every other input to a brief: it walks backwards from i and can
    never see a bar the desk has not reached. Returns [] rather than guessing
    when the window holds only one day -- an empty prior day is a real answer,
    and inventing yesterday's range from today's bars would put a fabricated
    level in front of the analyst wearing a confirmed label.
    """
    if i <= 0 or i >= len(bars):
        return []
    today = bars[i].ts.date()
    prior_date = None
    out: list[Bar] = []
    for b in reversed(bars[:i]):
        d = b.ts.date()
        if d == today:
            continue
        if prior_date is None:
            prior_date = d
        if d != prior_date:
            break
        out.append(b)
    return list(reversed(out))


def build_brief(bars: Sequence[Bar], i: int, st: StructureState,
                sw: Sequence, bid: float, ask: float, tick_age_s: float,
                htf: Optional[StructureState] = None,
                timeline: Sequence[str] = (), symbol: str = "XAUUSD",
                timeframe: str = "D1",
                macro: Optional[MacroContext] = None) -> MarketBrief:
    """Assemble the analyst brief from deterministic state. Levels get stable ids.

    `macro` is optional and defaults to None, which renders as UNMEASURED
    rather than as a neutral backdrop -- a caller with no macro feed must
    produce a brief that SAYS so, because omitting the section leaves the model
    unable to tell a missing read from one that was never going to be there.
    """
    vis = visible_swings(sw, i)
    levels: list[Level] = []
    n = 1
    for s in vis[-8:]:
        levels.append(Level(f"L{n}",
                            LevelKind.SWING_HIGH if s.kind == "HIGH" else LevelKind.SWING_LOW,
                            round(s.price, 2), timeframe, i - s.idx, True))
        n += 1
    day = bars[max(0, i - 24):i + 1]
    levels.append(Level(f"L{n}", LevelKind.SESSION_HIGH,
                        round(max(b.high for b in day), 2), timeframe, 0, True)); n += 1
    levels.append(Level(f"L{n}", LevelKind.SESSION_LOW,
                        round(min(b.low for b in day), 2), timeframe, 0, True)); n += 1
    if st.trigger_price is not None:
        levels.append(Level(f"L{n}", LevelKind.RECLAIM, round(st.trigger_price, 2),
                            timeframe, 0, True)); n += 1

    # PRIOR-DAY EXTREMES. LevelKind has carried PRIOR_DAY_HIGH and PRIOR_DAY_LOW
    # since the enum was written and NOTHING EVER BUILT THEM -- two named,
    # confirmed, entirely ordinary reference points that the analyst could see
    # in the vocabulary and never in the table. They matter most in exactly the
    # case that was failing: price making a new session low still has
    # yesterday's low beneath it, so the trade that had "no level below L10 to
    # run to" usually did have one, a day older.
    #
    # Real observed structure, so NOT projected: these may carry a stop.
    prior = _prior_day_bars(bars, i)
    if prior:
        levels.append(Level(f"L{n}", LevelKind.PRIOR_DAY_HIGH,
                            round(max(b.high for b in prior), 2), timeframe,
                            i - len(prior), True)); n += 1
        levels.append(Level(f"L{n}", LevelKind.PRIOR_DAY_LOW,
                            round(min(b.low for b in prior), 2), timeframe,
                            i - len(prior), True)); n += 1

    # ATR PROJECTIONS, past the session extremes only.
    #
    # Deliberately anchored BEYOND the extremes rather than around spot: inside
    # the range there are already real levels to aim at, and adding derived ones
    # there would compete with structure for no gain. Past the extreme is where
    # the table is empty and where a trending market spends its day.
    #
    # Multiples are 1x and 2x ATR from the extreme. Not tuned -- deliberately
    # round, because a fitted multiple would be this desk choosing its own
    # target distribution and calling it measurement.
    #
    # projected=True, so compile_signal will refuse them as a stop or an entry.
    atr = st.atr if getattr(st, "atr", None) else 0.0
    if atr > 0:
        sess_hi = max(b.high for b in day)
        sess_lo = min(b.low for b in day)
        for mult in (1.0, 2.0):
            levels.append(Level(f"L{n}", LevelKind.ATR_PROJECTION,
                                round(sess_lo - mult * atr, 2), timeframe, 0,
                                True, projected=True)); n += 1
            levels.append(Level(f"L{n}", LevelKind.ATR_PROJECTION,
                                round(sess_hi + mult * atr, 2), timeframe, 0,
                                True, projected=True)); n += 1

    if htf is None:
        align = "NEUTRAL"
    elif htf.trend_direction == "NONE" or st.trend_direction == "NONE":
        align = "NEUTRAL"
    else:
        align = "ALIGNED" if htf.trend_direction == st.trend_direction else "CONFLICTED"

    ctx = Context(
        trend_direction=st.trend_direction, trend_health=st.trend_health,
        trend_maturity=st.trend_maturity, volatility_state=st.volatility_state,
        htf_alignment=align, displacement_state=st.displacement_state,
        sweep_state=st.sweep_state, reclaim_state=st.reclaim_state,
        pullback_depth=st.pullback_depth,
        distance_from_session_extreme=st.distance_from_session_extreme)

    # CAUSAL: bars[:i+1], never bars[i+1:] -- gold_trend_read's own leak test
    # asserts the read at a cutoff cannot move when later bars change, and
    # that guarantee only holds if the caller upholds its half by never
    # passing bars the desk has not reached yet.
    trend = gold_trend_read(bars[:i + 1])
    # Same causal contract as trend above -- only bars[:i+1], never a peek
    # forward. See day_state.read()'s own docstring for why that is enough:
    # it only ever reads calendar days strictly before the last bar's date.
    dstate = day_state_read(bars[:i + 1])

    # THE HALF OF A CHART THAT IS NOT A LEVEL, AS NUMBERS. ANALYST_SYSTEM asks the model to read
    # "compression and expansion, wick character, whether bodies are closing at the extremes or
    # the middle, whether a move looks impulsive or grinding" off a chart image -- and this desk
    # runs --numeric-only, because the Claude Code CLI accepts no image input at all. So the
    # analyst was being asked for a reading it had no way to take. These ratios carry that same
    # information at zero marginal cost, on the subscription, and with no rendering through which
    # annotations could write the answer (candle_character.py records the desk's own measurement
    # of exactly that failure).
    #
    # Same causal contract as `trend` and `dstate` above: bars[:i + 1], never a peek forward.
    # FLOWS: who is actually holding the metal. Read from the cache the collector maintains --
    # never fetched on the decision path, because a decision must not wait on, or fail with, a
    # third-party website. An absent or stale cache renders UNMEASURED inside the block itself.
    blocks = (candle_character_block(bars[:i + 1]),
              flows_load(FLOWS_CACHE).to_prompt())

    return MarketBrief(
        symbol=symbol, as_of_utc=bars[i].ts, session=session_of(bars[i].ts),
        bid=round(bid, 2), ask=round(ask, 2), spread=round(ask - bid, 2),
        tick_age_s=tick_age_s, atr=round(st.atr, 2), context=ctx, levels=levels,
        trigger_price=None if st.trigger_price is None else round(st.trigger_price, 2),
        trigger_utc=bars[i].ts, timeline=tuple(timeline), trend=trend,
        bar_close=round(bars[i].close, 2),
        day_state=dstate, macro=macro, blocks=blocks)


# --------------------------------------------------------------------------
# Analysts — the swappable seat
# --------------------------------------------------------------------------

class Analyst(Protocol):
    name: str
    def read(self, brief: MarketBrief) -> AnalystRead: ...


class DeterministicAnalyst:
    """ARM A. The desk's own rules, no model. Not a mock — the baseline itself."""
    name = "A-deterministic"

    def read(self, b: MarketBrief) -> AnalystRead:
        c = b.context
        none = AnalystRead(setup=Setup.NO_SETUP, direction="NONE", entry_ref="NONE",
                           stop_ref="NONE", tp1_ref="NONE", tp2_ref="NONE",
                           mechanism_name="none", confidence=1, read="no rule matched",
                           why="n/a", why_not="n/a", invalidation="n/a")
        highs = [l for l in b.levels if l.kind is LevelKind.SWING_HIGH]
        lows = [l for l in b.levels if l.kind is LevelKind.SWING_LOW]
        if not highs or not lows:
            return none

        if (c.displacement_state in ("CONFIRMED", "EXCEPTIONAL")
                and c.trend_direction in ("UP", "DOWN")
                and c.pullback_depth in ("SHALLOW", "MEDIUM")):
            if c.trend_direction == "UP":
                return AnalystRead(setup=Setup.TREND_CONTINUATION, direction="LONG",
                    entry_ref="MARKET", stop_ref=lows[-1].id, tp1_ref="NONE",
                    tp2_ref=highs[-1].id, mechanism_name="displacement-continuation", confidence=3,
                    read="displacement with shallow pullback in an uptrend",
                    why="unfilled demand at the displacement origin",
                    why_not="rule-based; no contextual check",
                    invalidation=f"close below {lows[-1].id}")
            return AnalystRead(setup=Setup.TREND_CONTINUATION, direction="SHORT",
                entry_ref="MARKET", stop_ref=highs[-1].id, tp1_ref="NONE",
                tp2_ref=lows[-1].id, mechanism_name="displacement-continuation", confidence=3,
                read="displacement with shallow pullback in a downtrend",
                why="unfilled supply at the displacement origin",
                why_not="rule-based; no contextual check",
                invalidation=f"close above {highs[-1].id}")

        if c.sweep_state == "CONFIRMED" and c.reclaim_state == "CONFIRMED":
            if c.trend_direction != "DOWN":
                return AnalystRead(setup=Setup.SWING_REVERSAL, direction="LONG",
                    entry_ref="MARKET", stop_ref=lows[-1].id, tp1_ref="NONE",
                    tp2_ref=highs[-1].id, mechanism_name="sweep-reclaim-trap", confidence=3,
                    read="sweep and reclaim of the swing low",
                    why="sellers trapped below the reclaim",
                    why_not="rule-based; no contextual check",
                    invalidation=f"close below {lows[-1].id}")
        return none


class ClaudeAnalyst:
    """ARM B. Wired. Requires ANTHROPIC_API_KEY — absent in this container."""
    name = "B-claude"

    def __init__(self, effort: str = "medium"):
        self.effort = effort

    def read(self, b: MarketBrief) -> AnalystRead:
        from .analyst import call_analyst
        return call_analyst(b, effort=self.effort)


# --------------------------------------------------------------------------
# Deterministic risk gate
# --------------------------------------------------------------------------

@dataclass
class RiskLimits:
    """RISK INVARIANTS ONLY. There are no quotas here by design.

    A cap on how much can be lost is a solvency constraint. A cap on how OFTEN
    the desk may act is a quota, and a quota discards positive-value trades for
    no economic reason. Everything frequency-related has been removed:
    max_signals_per_day and max_concurrent are gone. Concurrency is governed by
    portfolio heat in opportunity.Heat, which is denominated in risk.
    """
    max_risk_per_trade_pct: float = 0.5
    max_daily_loss_r: float = 3.0        # ruin control, not selectivity
    max_open_risk_r: float = 2.0         # total R live across all positions
    correlation_haircut: float = 0.65    # same-symbol same-direction overlap


@dataclass
class RiskState:
    open_risks: list = field(default_factory=list)   # R at risk per open trade
    open_directions: list = field(default_factory=list)
    day: Optional[str] = None
    day_loss_r: float = 0.0
    day_signals: int = 0            # counted for reporting only, never enforced

    @property
    def open_positions(self) -> int:
        return len(self.open_risks)

    def roll(self, ts: datetime) -> None:
        d = ts.date().isoformat()
        if d != self.day:
            self.day, self.day_loss_r, self.day_signals = d, 0.0, 0


def risk_check(sig: CompiledSignal, st: RiskState, lim: RiskLimits) -> tuple[bool, str]:
    """Solvency only. Nothing here refuses a trade for being the Nth today."""
    if st.day_loss_r <= -lim.max_daily_loss_r:
        return False, f"daily loss {st.day_loss_r:.2f}R at ruin limit"
    same_dir = sum(1 for d in st.open_directions if d == sig.direction)
    new_risk = 1.0                                   # each trade risks 1R by construction
    effective = sum(st.open_risks) + new_risk * (1.0 + lim.correlation_haircut * same_dir)
    if effective > lim.max_open_risk_r:
        return False, (f"portfolio heat {effective:.2f}R would exceed "
                       f"{lim.max_open_risk_r:.2f}R ({st.open_positions} open, "
                       f"{same_dir} same-direction)")
    return True, f"heat {effective:.2f}R of {lim.max_open_risk_r:.2f}R"


# --------------------------------------------------------------------------
# The shadow run
# --------------------------------------------------------------------------

@dataclass
class RunStats:
    bars: int = 0
    states: int = 0
    wakes: int = 0
    reads: int = 0
    signals: int = 0
    refusals_model: int = 0
    refusals_compiler: int = 0
    refusals_router: int = 0
    refusals_risk: int = 0
    analyst_errors: int = 0
    refusal_reasons: dict = field(default_factory=dict)


class ShadowRunner:
    """Decides nothing. Fills the ledger with real states and real forward paths."""

    def __init__(self, source: BarSource, analyst: Analyst, ledger: Ledger,
                 sink: Optional[Sink] = None,
                 thresholds: Thresholds = Thresholds(),
                 cost_model: CostModel = CostModel(),
                 limits: RiskLimits = RiskLimits(),
                 heartbeat: timedelta = timedelta(days=3),
                 # SAME BIAS AS THE LIVE PATH, AND WORSE. At 40 bars a refusal
                 # was resolved over ten hours; anything that paid off on day
                 # two was recorded as forgone 0.0R. Since the constitution
                 # prices a restriction by what refusing COST, a short window
                 # makes every gate look cheap and every gate therefore gets
                 # kept. The cost of a longer window is that the last N bars of
                 # the series produce no decisions, which on a multi-year study
                 # is nothing.
                 forward_bars: int = 480):
        self.source, self.analyst, self.ledger = source, analyst, ledger
        self.sink = sink or build_sink(None)
        self.thresholds, self.cost_model, self.limits = thresholds, cost_model, limits
        self.watcher = Watcher(heartbeat=heartbeat, min_gap=timedelta(0))
        self.forward_bars = forward_bars
        self.stats = RunStats()

    def run(self) -> RunStats:
        bars = list(self.source.bars())
        self.stats.bars = len(bars)
        atrs = atr(bars)
        sw = swings(bars)
        # Higher timeframe: every 5th bar, same classifier. Real, not a stub.
        htf_bars = bars[::5]
        htf_atrs, htf_sw = atr(htf_bars), swings(htf_bars)
        risk = RiskState()
        timeline: list[str] = []

        for i in range(len(bars) - self.forward_bars):
            st = classify(bars, i, sw, atrs)
            if st is None:
                continue
            self.stats.states += 1
            ts = bars[i].ts
            risk.roll(ts)
            sess = session_of(ts)

            timeline.append(f"{ts.date()} {st.trend_direction}/{st.trend_health} "
                            f"disp={st.displacement_state} sweep={st.sweep_state}")
            timeline[:] = timeline[-8:]

            w = self.watcher.observe(st, sess, ts)
            if not w.wake:
                continue
            self.stats.wakes += 1

            hi = i // 5
            htf = classify(htf_bars, hi, htf_sw, htf_atrs) if hi >= 30 else None
            bid, ask, age = self.source.quote(bars[i])
            brief = build_brief(bars, i, st, sw, bid, ask, age, htf, timeline)

            try:
                read = self.analyst.read(brief)
                self.stats.reads += 1
            except Exception as e:
                self.stats.analyst_errors += 1
                log.warning("analyst failed at %s: %s", ts, e)
                continue

            self._record(bars, i, brief, read, risk, w.reason)
        return self.stats

    # -- one decision, fully resolved
    def _record(self, bars, i, brief, read, risk, wake_reason) -> None:
        ts = bars[i].ts
        fwd = bars[i:i + self.forward_bars + 1]
        lbars = [LBar(b.ts, b.open, b.high, b.low, b.close) for b in fwd]
        pref = PathRef.of(brief.symbol, "D1", lbars)

        def note(kind, by, decision, reason, direction, risk_price):
            self.stats.refusal_reasons[reason[:60]] = \
                self.stats.refusal_reasons.get(reason[:60], 0) + 1
            out = resolve_forward(lbars, ts, bars[i].close, direction, risk_price)
            self.ledger.append(DecisionRecord(
                decision_id=f"{brief.symbol}-{ts.isoformat()}", kind=kind, t0=ts,
                symbol=brief.symbol,
                context=brief.context.__dict__ | {"session": brief.session,
                                                  "wake": wake_reason},
                brief_render=brief.render(), decided_by=by, decision=decision,
                reason=reason, path_ref=pref, outcome=out))

        if read.setup is Setup.NO_SETUP:
            self.stats.refusals_model += 1
            note(DecisionKind.REFUSAL_MODEL, "MODEL", {"setup": "NO_SETUP"},
                 "analyst: NO_SETUP", "LONG", brief.atr)
            return

        res = compile_signal(brief, read, self.thresholds, self.cost_model)
        if isinstance(res, Refusal):
            router = "edge router" in res.reason
            self.stats.refusals_router += int(router)
            self.stats.refusals_compiler += int(not router)
            note(DecisionKind.REFUSAL_ROUTER if router else DecisionKind.REFUSAL_COMPILER,
                 "ROUTER" if router else "COMPILER",
                 {"declined": read.direction, "setup": read.setup.value},
                 res.reason, read.direction, brief.atr)
            return

        ok, why = risk_check(res, risk, self.limits)
        if not ok:
            self.stats.refusals_risk += 1
            note(DecisionKind.REFUSAL_COMPILER, "POLICY",
                 {"declined": res.direction}, f"risk: {why}", res.direction, res.risk)
            return

        self.stats.signals += 1
        risk.day_signals += 1
        note(DecisionKind.SIGNAL, "MODEL",
             {"direction": res.direction, "setup": res.setup.value,
              "entry": res.entry, "stop": res.stop, "tp1": res.tp1, "tp2": res.tp2,
              "rr_tp2": res.rr_tp2, "cost_r": res.cost_r,
              "handoff": res.to_management_handoff()},
             f"signal {res.direction} rr {res.rr_tp2:.2f}", res.direction, res.risk)
        self.sink.send(f"{res.direction} {brief.symbol} @ {res.entry} "
                       f"SL {res.stop} TP2 {res.tp2} ({res.rr_tp2:.2f}R)")
