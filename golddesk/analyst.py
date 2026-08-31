"""Gold analyst layer — model reads structure, compiler owns every number.

Drop-in module for golddesk. The split this file enforces:

    code   -> computes all geometry (swings, ATR, session, spread, R:R)
    model  -> reads structure and REFUSES; picks levels by id, never by price
    code   -> prices the proposal, applies the gates, and can veto

The model cannot invent a price. It selects from levels the desk already
computed, by id. Anything numeric in a CompiledSignal came from this file,
not from the model. That is the whole design.

Wired by runner.py: build_brief() assembles a MarketBrief from a BarSource,
call_analyst() needs ANTHROPIC_API_KEY, compile_signal() reads thresholds from
config. The only host-specific work is the BarSource adapter — see runner.py.

Every call produces a row worth journalling, including NO_TRADE. The refusals
are the point: they are the false-negative ledger the charter says the desk
has six of and needs hundreds.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal, Optional, Sequence

import anthropic
from pydantic import BaseModel, Field, ValidationError

from .chart import Chart, estimate_image_tokens

if TYPE_CHECKING:                                                  # pragma: no cover
    from .hierarchical_bias import TimeframeRead
from .costs import CostModel, breakeven_win_rate, cost_in_r, round_trip_cost
from .opportunity import CohortStat, ev_gate
from .router import route

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
EFFORT = "medium"        # low/medium are unusually strong on this model; sweep it
MAX_TOKENS = 8000        # thinking and output share this budget


# --------------------------------------------------------------------------
# What the code hands the model
# --------------------------------------------------------------------------

class LevelKind(str, Enum):
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"
    PRIOR_DAY_HIGH = "PRIOR_DAY_HIGH"
    PRIOR_DAY_LOW = "PRIOR_DAY_LOW"
    DISPLACEMENT_OPEN = "DISPLACEMENT_OPEN"
    RECLAIM = "RECLAIM"


@dataclass(frozen=True)
class Level:
    """A reference point the desk computed. `id` is what the model cites."""
    id: str                  # "L1", "L2", ... stable within one brief
    kind: LevelKind
    price: float
    timeframe: str           # "H4", "M15", ...
    bars_ago: int            # how long ago it was confirmed
    confirmed: bool          # False => not usable, shown for context only


@dataclass(frozen=True)
class Context:
    """Deterministic semantic state. Code measures; the model interprets.

    Prose in a dict cannot carry the market. These are the dimensions the
    desk's own research says discriminate, exposed as discrete states so the
    model reasons over measured facts and the router can gate on them.
    """
    trend_direction: Literal["UP", "DOWN", "NONE"]
    trend_health: Literal["STRONG", "MODERATE", "WEAK"]
    trend_maturity: Literal["YOUNG", "MID", "MATURE", "EXHAUSTED"]
    volatility_state: Literal["LOW", "NORMAL", "ELEVATED", "EXTREME"]
    htf_alignment: Literal["ALIGNED", "CONFLICTED", "NEUTRAL"]
    displacement_state: Literal["NONE", "FORMING", "CONFIRMED", "EXCEPTIONAL"]
    sweep_state: Literal["NONE", "CONFIRMED"]
    reclaim_state: Literal["NONE", "WEAK", "CONFIRMED"]
    pullback_depth: Literal["NONE", "SHALLOW", "MEDIUM", "DEEP"]
    distance_from_session_extreme: Literal["NEAR", "MID", "FAR"]

    def render(self) -> str:
        return "\n".join(f"  {k.upper():<30} {v}" for k, v in self.__dict__.items())


@dataclass(frozen=True)
class MarketBrief:
    """Deterministic snapshot. Every number here was computed by the desk."""
    symbol: str
    as_of_utc: datetime
    session: str                     # ASIA | LONDON | NY | OVERLAP | ROLLOVER
    bid: float
    ask: float
    spread: float
    tick_age_s: float
    atr: float                       # on the entry timeframe
    context: Context
    levels: Sequence[Level]
    # Where the setup was TRIGGERED — the reclaim, the displacement origin, the
    # sweep. Anti-chase drift is measured from here, never from the entry.
    # Without it a MARKET entry has drift 0 by construction and the gate is
    # vacuous. Required whenever a live entry is possible.
    trigger_price: Optional[float] = None
    trigger_utc: Optional[datetime] = None
    timeline: Sequence[str] = field(default_factory=tuple)   # rolling memory
    notes: Sequence[str] = field(default_factory=tuple)
    #: Pre-rendered deterministic blocks — seasonality, supply calendar, and the multi-timeframe
    #: STATES. Verbatim, after the cache breakpoint, so adding one never invalidates the cached
    #: system prefix.
    #:
    #: THE TIMEFRAME STATES GO IN; THE ALIGNMENT VERDICT DOES NOT, AND THAT ORDERING IS FORCED.
    #: `hierarchical_bias.assess` rules on a PROPOSED DIRECTION, and no direction exists until the
    #: model has answered. Rendering a verdict here would mean either computing it for a direction
    #: nobody proposed, or computing it twice and letting the two disagree. The states are the
    #: honest thing to show before the read; the ruling happens in `compile_signal` after it.
    blocks: Sequence[str] = field(default_factory=tuple)
    #: The raw numerical path — the last N closed bars as arithmetic rather than
    #: pixels. Body/upper-wick/lower-wick percentages, close position within the
    #: bar, range relative to ATR and net return give the shape a chart carries
    #: without the picture. Computed by build_brief in runner.py.
    raw_path: Sequence[str] = field(default_factory=tuple)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def level(self, lid: str) -> Optional[Level]:
        return next((l for l in self.levels if l.id == lid), None)

    def render(self) -> str:
        """The volatile half of the prompt. Kept after the cache breakpoint."""
        lines = [
            f"SYMBOL {self.symbol}   AS_OF {self.as_of_utc.isoformat()}",
            f"SESSION {self.session}",
            f"BID {self.bid:.2f}  ASK {self.ask:.2f}  SPREAD {self.spread:.2f}"
            f"  TICK_AGE {self.tick_age_s:.0f}s",
            f"ATR {self.atr:.2f}",
            "",
            "MEASURED CONTEXT (deterministic — these are facts, not opinions)",
            self.context.render(),
        ]
        if self.raw_path:
            lines += ["", "RAW NUMERICAL PATH (last bars — shape without the picture)"]
            lines += [f"  {t}" for t in self.raw_path]
        if self.timeline:
            lines += ["", "HOW THIS DEVELOPED (most recent last)"]
            lines += [f"  {t}" for t in self.timeline]
        if self.trigger_price is not None:
            lines += ["", f"SETUP TRIGGER {self.trigger_price:.2f}"
                          f"  ({self.trigger_utc.isoformat() if self.trigger_utc else 'n/a'})"]
        lines += ["", "LEVELS (cite these by id — never write a price)"]
        for l in self.levels:
            flag = "" if l.confirmed else "  [UNCONFIRMED — not usable]"
            lines.append(
                f"  {l.id}  {l.kind.value:<18} {l.price:>10.2f}"
                f"  {l.timeframe} {l.bars_ago} bars ago{flag}"
            )
        for b in self.blocks:
            lines += ["", b]
        if self.notes:
            lines += ["", "NOTES"] + [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# What the model is allowed to return
# --------------------------------------------------------------------------

class Setup(str, Enum):
    NO_SETUP = "NO_SETUP"
    SWING_REVERSAL = "SWING_REVERSAL"
    TREND_CONTINUATION = "TREND_CONTINUATION"
    NOVEL = "NOVEL"
    """Legacy value kept for old ledgers only. The desk no longer emits it: a
    genuinely novel mechanism is filed under OTHER and given a measured,
    uncertainty-weighted cold start instead of a shadow ban."""
    OTHER = "OTHER"
    """The analytics bucket for mechanisms the desk has not yet named. Filing is
    a POST-HOC action done by the compiler, never a constraint on how the model
    thinks. Mechanism cohorts are keyed by mechanism_name, not by this label."""


class PathForecast(BaseModel):
    """The analyst's forward calibration surface — a belief, resolved later.

    Each probability is a separate measured binary event over the forward path:
    `p_plus_1r` is P(hit +1R before the stop IS hit), and so on. They are
    deliberately not constrained to sum to one: they answer distinct questions.
    The desk resolves these against what actually happened, both directions,
    which is what makes `novelty` and `path` falsifiable rather than prose.
    """
    p_plus_half_r: Optional[float] = Field(None, ge=0, le=1,
        description="P(+0.5R reached before -1R)")
    p_plus_1r: Optional[float] = Field(None, ge=0, le=1,
        description="P(+1R reached before -1R)")
    p_plus_2r: Optional[float] = Field(None, ge=0, le=1,
        description="P(+2R reached before -1R)")
    p_minus_1r_first: Optional[float] = Field(None, ge=0, le=1,
        description="P(-1R hits before +1R is reached)")
    expected_mfe_r: Optional[float] = Field(None,
        description="Expected maximum favourable excursion in R")
    expected_mae_r: Optional[float] = Field(None,
        description="Expected maximum adverse excursion in R")
    expected_r: Optional[float] = Field(None,
        description="The analyst's own net-R expectation — their belief, not the "
                    "desk's measured estimate. Calibrated forward like everything else.")
    expected_holding_hours: Optional[float] = Field(None,
        description="Expected holding time in hours before TP2 or exit")
    path_narrative: Optional[str] = Field(None, max_length=320,
        description="Plain-language path, e.g. 'grind to TP1, stall, retest entry, TP2'")

    def is_complete(self) -> bool:
        return all(v is not None for v in (
            self.p_plus_1r, self.p_plus_2r, self.p_minus_1r_first,
            self.expected_mfe_r, self.expected_mae_r, self.expected_r))


class VisualRegion(BaseModel):
    """A visual concept on the chart. NEVER a price — the compiler owns prices.

    The model names meaning (`compression shelf`, `last-drive-and-dig`) and
    places it in a band relative to a reference; the compiler resolves the band
    to a price zone. A region that carries a number is dropped, not priced.
    """
    concept: str = Field(max_length=90)
    band: Literal["ABOVE", "BELOW", "AT"]
    reference_ref: str = Field(
        description='Level id, "CURRENT_PRICE", or "NEAREST". Never a number.')
    notes: Optional[str] = Field(None, max_length=120)


class AdversarialReview(BaseModel):
    """The mechanism casing. Required on every ACTIONABLE read. Each is short,
    concrete, and load-bearing — boilerplate is indistinguishable from lying."""
    thesis: str = Field(max_length=260, description="The mechanism, one sentence.")
    counter_cases: str = Field(max_length=400,
        description="At most three DISTINCT ways this specific read is wrong, numbered. "
                    "'the market might reverse' is not a case.")
    missing: str = Field(max_length=260,
        description="What information, if you had it, would change this read.")
    forced: str = Field(max_length=260,
        description="Who is forced to act here — the flow that makes price have to move.")
    timing: str = Field(max_length=260,
        description="Why now — the trigger, the pending event, the level density.")
    monetization: str = Field(max_length=260,
        description="Which side of that forced flow you are paid on.")

    def is_complete(self) -> bool:
        return bool(self.thesis and self.counter_cases and self.forced
                    and self.timing and self.monetization)


class AnalystRead(BaseModel):
    """The model's entire output surface. No price fields exist here by design.

    The VERDICT is `action`; `setup` is an optional analytics tag the desk
    classifies from mechanism, never a constraint on how you think.
    """
    model_config = {"extra": "forbid"}

    action: Literal["ACTIONABLE", "NO_TRADE"] = "ACTIONABLE"
    setup: Setup = Setup.OTHER
    direction: Literal["LONG", "SHORT", "NONE"]
    entry_ref: str = Field(description='Level id, or "MARKET" to enter at the live quote.')
    stop_ref: str = Field(description='Level id the stop sits beyond, or "NONE".')
    tp1_ref: str = Field(description='Level id for the first objective (partial bank), or "NONE".')
    tp2_ref: str = Field(description='Level id for the runner objective, or "NONE".')
    mechanism_name: str = Field(max_length=60, description=(
        "Short stable label for the mechanism, e.g. 'failed-breakout-trap'. "
        "Reuse the same label for the same mechanism so cohorts accumulate."))
    setup_tag: Optional[str] = Field(None, max_length=40, description=(
        "Optional free-text analytics tag ('failed-breakout-trap', 'asia-sweep-reclaim'). "
        "The desk always derives its own family label; this is only your phrasing."))
    novelty: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        "LOW", description=(
            "How novel this situation is. Honest uncertainty: HIGH means the desk "
            "widens its epistemic prior until mechanism_name has history. It is a "
            "measured data point, never a ban."))
    entry_intent: Literal["LIMIT", "BREAK", "MARKET", "PULLBACK"] = Field(
        "MARKET", description="How you would get in: a resting limit, a break trigger, live, "
                              "or on a pullback into the level. Never a price.")
    expected_holding_hours: Optional[float] = Field(None,
        description="Expected holding time in hours before exit.")
    path: Optional[PathForecast] = Field(None, description="Required on ACTIONABLE.")
    visual_regions: list[VisualRegion] = Field(default_factory=list,
        description="Visual concepts in bands. Never include a number.")
    adversarial: Optional[AdversarialReview] = Field(None,
        description="Required on ACTIONABLE.")
    confidence: int = Field(ge=1, le=5)
    read: str = Field(max_length=700, description="What price is doing, in plain words.")
    why: str = Field(max_length=500, description="The mechanism. Not the pattern name.")
    why_not: str = Field(max_length=500, description="The strongest case against. Never empty.")
    invalidation: str = Field(max_length=300, description="What would prove this read wrong.")

    def is_no_trade(self) -> bool:
        """A read is a refusal when the model says so, in any of the spellings
        the desk has used. The compiler routes on this, never on `setup`."""
        return self.action == "NO_TRADE" or self.direction == "NONE"


ANALYST_SCHEMA = AnalystRead.model_json_schema()
ANALYST_SCHEMA["additionalProperties"] = False


# --------------------------------------------------------------------------
# Post-hoc analytics classification — the desk's job, not the model's
# --------------------------------------------------------------------------

def classify_family(read: AnalystRead) -> Setup:
    """File a read into an analytics family AFTER the mechanism exists.

    The model never thinks in families. An explicit family on the read is
    honoured; otherwise the mechanism's own words decide the bucket, and a
    genuinely novel mechanism lands in OTHER with a real cohort keyed on its
    mechanism_name — the old blanket NOVEL shadow is gone.
    """
    if read.setup in (Setup.SWING_REVERSAL, Setup.TREND_CONTINUATION):
        return read.setup
    text = f"{read.mechanism_name} {read.setup_tag or ''} {read.why}".lower()
    if any(k in text for k in ("sweep", "reclaim", "reversal", "trap", "engulf")):
        return Setup.SWING_REVERSAL
    if any(k in text for k in ("continuation", "displacement", "breakout",
                               "retest", "pullback", "resume", "follow-through")):
        return Setup.TREND_CONTINUATION
    return Setup.OTHER


_PRICE_IN_CONCEPT = re.compile(r"\d+\.\d{2}")


def resolve_region(brief: MarketBrief, region: VisualRegion) -> Optional[str]:
    """Turn a named visual concept + band + reference into a price zone string.

    The model names meaning; the compiler owns the numbers. A region that
    carries a price is dropped — it is annotation, not load-bearing, and an
    invented price in a vision field would be a leak of exactly the kind the
    single hard rule forbids.
    """
    if _PRICE_IN_CONCEPT.search(region.concept or ""):
        log.debug("visual_region dropped (contains a price): %r", region.concept)
        return None
    if region.reference_ref == "CURRENT_PRICE":
        anchor, ref = brief.mid, "price"
    elif region.reference_ref == "NEAREST":
        lvl = min((l for l in brief.levels if l.confirmed),
                  key=lambda l: abs(l.price - brief.mid), default=None)
        if lvl is None:
            return None
        anchor, ref = lvl.price, lvl.id
    else:
        lvl = brief.level(region.reference_ref)
        if lvl is None or not lvl.confirmed:
            return None
        anchor, ref = lvl.price, lvl.id
    return f"{region.concept} {region.band} {ref}@{anchor:.2f}"


# --------------------------------------------------------------------------
# The prompt contract (stable — this is the cached prefix)
# --------------------------------------------------------------------------

ANALYST_SYSTEM = """\
You are the gold analyst on a signal-only XAUUSD desk. You read structure and \
form an opinion. You never place orders, and you never compute a tradeable number.

## The single hard rule

You do not write prices. Every level you reference is cited by its id from the \
LEVELS table (L1, L2, ...). A deterministic compiler resolves those ids to prices, \
computes R:R, charges spread, and can veto you. If you write a number into a \
reference field, the read is discarded.

## Refusal is a verdict, not a quota

action NO_TRADE is correct when no available trade has positive expected value \
after costs. It is not a target, a virtue, or a sign of discipline. There is no \
quota to fill in either direction: do not suppress a genuinely positive-value \
opportunity to appear selective, and do not lower your standard to produce \
activity.

The desk's objective is maximum realised net value, so a missed profitable \
trade costs it exactly as much as a losing one of the same size. Every refusal \
is resolved forward against what price actually did, in both directions — so \
"I passed on that" is measured, not forgiven. If twelve genuinely positive \
opportunities exist today, twelve is the right answer. If none exist, zero is.

You are not being scored on how rarely you fire. You are scored on the net \
value of everything you did and did not take.

`why_not` is mandatory on every read, including NO_TRADE — on a refusal it \
must say what would make this worth taking, not merely why it looks unclean.

## Mechanism, not family

There are no strategy families for you to file into. SWING_REVERSAL and \
TREND_CONTINUATION are labels the DESK'S COMPILER assigns afterwards for \
analytics; you never think in them and you are never constrained by them. What \
you write is a mechanism — a statable story about who is trapped, who must act, \
and what flow is forced. "Bull flag" is a description; "sellers who chased the \
sweep are trapped below the reclaim and their stops sit above it" is a \
mechanism. If you cannot name the forced flow, there is no trade — say \
action NO_TRADE.

Give every read a mechanism_name and reuse it verbatim for the same mechanism. \
That label is how the desk discovers, months later, that one of your ideas has \
been quietly working — or quietly not.

## Novelty is a measured uncertainty, never a punishment

If the situation is genuinely something you have not named before, say so \
plainly with novelty HIGH — an honest unknown is a data point, not a sin, and \
silently forcing it into a familiar name IS a lie. The desk handles novelty as \
uncertainty: HIGH novelty widens the cold-start prior until your \
mechanism_name accumulates real resolved history. That is a slight, transparent \
charge in exchange for running a trade no one has evidence on — never a shadow \
and never a ban. Each such read is journalled and its forward path resolved, so \
the desk learns whether your novel ideas pay, exactly as it learns about \
anything else.

## Path forecast — beliefs the desk resolves forward

Every ACTIONABLE read must carry a path: what you actually expect to happen, \
as numbers and a short narrative. Each probability answers a separate \
binary-forward question and they are NOT constrained to sum to one:

  p_plus_half_r   P(+0.5R is reached before -1R)
  p_plus_1r       P(+1R is reached before -1R)
  p_plus_2r       P(+2R is reached before -1R)
  p_minus_1r_first  P(-1R happens first)
  expected_mfe_r / expected_mae_r   expected favourable/adverse excursion in R
  expected_r      your own net-R expectation
  expected_holding_hours  how long this takes

The desk resolves every one of these against what actually happens, in both \
directions, so "I expected +1R before the stop" is measured, not forgiven. \
Calibrate yourself: if p_plus_1r has been 0.6 and +1R has shown up less often \
than that, move the number down. These beliefs travel with the signal and are \
scored. Do not make them comfortable; make them honest.

## Visual regions

In visual_regions you may name what the chart's shape means in one band: a \
concept, a band ABOVE/BELOW/AT, and a reference (a level id, CURRENT_PRICE, or \
NEAREST). The compiler resolves the band to a price zone. NEVER put a number in \
a visual_region — any region carrying a price is dropped. Examples of concepts \
that carry meaning: compression shelf, last-drive-and-dig, wick-rejection \
neckline, the break-and-retest zone, an unattended pool of stops.

## Adversarial case (required on every ACTIONABLE read)

Before you claim a trade, argue against yourself in structured pieces:

  thesis        the mechanism, one sentence.
  counter_cases at most three DISTINCT concrete ways THIS read is wrong,
                numbered. "the market might reverse" is not a case.
  missing       what information, if you had it, would change this read.
  forced        WHO is forced to act here — the specific flow that makes price
                have to move, and when.
  timing        why now and not an hour ago or an hour from now.
  monetization  which side of that forced flow you are paid on.

Each field is one or two short sentences. Boilerplate is indistinguishable \
from lying — a generic casing tells the desk nothing and the forward ledger \
will find out anyway.

## The charts

You may be given several chart images, including multiple synchronized zooms \
of the SAME timeframe. They are deliberately unannotated — no drawn levels, no \
trendlines, no indicators, no labels. That is not an oversight. On the same \
bar, an annotated render made this desk report "broken major support, retesting \
from below" while the clean render of the same data reported "range-bound, no \
clean alignment". The annotations wrote the answer. So you get shape and \
nothing else.

Use the close zoom for wick character and whether bodies close at the extremes \
or the middle; the wide view for where price sits inside its range; the higher \
timeframes for context. Use the chart for what a table cannot carry: \
compression and expansion, whether a move looks impulsive or grinding.

Take every number from the LEVELS table. If the chart appears to show a level \
that is not in the table, it is not a level — the table is the desk's own \
confirmed structure and the picture is a rendering of the same bars. Where the \
two seem to disagree, the table wins and you say so in `why_not`.

The RAW NUMERICAL PATH block is the same shape another way: body, upper and \
lower wick, close position, range relative to ATR, net return, one bar per \
line. The picture and the numbers must agree, and where they do not, the \
numbers win.

## Conditions you must weigh

- Unconfirmed levels are context only. Never build a stop or target on one.
- A stale tick means the quote may not be real. Say so and stand down.
- Session matters. Asia ranges; the London-NY overlap trends; the rollover hour \
  is thin and its levels are unreliable.
- Counter-trend into a healthy trend is the worst cohort the desk has measured. \
  Reversal against a STRONG_BULL or STRONG_BEAR regime needs an exceptional \
  mechanism, not an ordinary one.
- Gold has one price history. A pattern you have seen "many times" you have \
  likely seen once, in one regime. Weight mechanism over familiarity.

## Direction and refs

- action NO_TRADE -> direction NONE, all refs "NONE", confidence 1.
- ACTIONABLE is only for a real mechanism you would size yourself. It is not a \
  reward for writing a clever case — state it, or refuse.
- Otherwise: stop_ref and target_ref must be real level ids. entry_ref may be \
  "MARKET" if the setup is live now.
- LONG means stop below entry, target above. SHORT is the mirror. The compiler \
  checks this and rejects an inverted structure, so get it right.

## Confidence

1 = would not take it. 3 = ordinary. 5 = the clearest read available in this \
market, which should be rare. Do not drift upward over a session.

Write plainly. No hedging filler, no emoji, no markdown headers in your prose \
fields. The reader is a trader with the chart already open.
"""


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------

class AnalystError(RuntimeError):
    pass


def call_analyst(
    brief: MarketBrief,
    charts: Sequence[Chart] = (),
    *,
    client: Optional[anthropic.Anthropic] = None,
    model: str = MODEL,
    effort: str = EFFORT,
) -> AnalystRead:
    """One read. Raises AnalystError on refusal, malformed output, or API failure.

    SEAM 2: needs ANTHROPIC_API_KEY in the environment (or an injected client).

    `charts` are clean renders from chart.py. Images go before the text so the
    model sees the shape first, then reads the authoritative numbers.
    """
    client = client or anthropic.Anthropic()

    content: list[dict] = []
    for c in charts:
        content.append({"type": "text", "text": f"Chart — {c.timeframe}, closed bars only:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(c.png).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": brief.render()})

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": ANALYST_SYSTEM,
                # Stable prefix -> cached. The brief goes in the user turn,
                # after the breakpoint, so it never invalidates this.
                "cache_control": {"type": "ephemeral"},
            }],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": ANALYST_SCHEMA},
            },
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.RateLimitError as e:
        raise AnalystError(f"rate limited: {e}") from e
    except anthropic.APIStatusError as e:
        raise AnalystError(f"api error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AnalystError(f"connection failed: {e}") from e

    # Check this before touching content — on a refusal, content is empty.
    if resp.stop_reason == "refusal":
        cat = getattr(resp.stop_details, "category", None)
        raise AnalystError(f"model declined the request (category={cat})")
    if resp.stop_reason == "max_tokens":
        raise AnalystError("output truncated — raise MAX_TOKENS or lower effort")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise AnalystError("no text block in response")

    try:
        return AnalystRead.model_validate_json(text)
    except ValidationError as e:
        raise AnalystError(f"schema violation: {e}") from e

    finally:
        log.debug(
            "analyst usage in=%s cache_read=%s out=%s",
            resp.usage.input_tokens,
            resp.usage.cache_read_input_tokens,
            resp.usage.output_tokens,
        )


# --------------------------------------------------------------------------
# The compiler — owns every number, holds the veto
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Thresholds:
    """SEAM 3: mirror these from config/desk.json."""
    # FALLBACK ONLY — used when a mechanism has no resolved history. The
    # primary gate is expectancy (opportunity.ev_gate), not reward-to-risk.
    fallback_min_rr: float = 1.5
    min_ev_r: float = 0.0            # take anything with positive expected value
    max_entry_drift_r: float = 0.30
    stop_atr_buffer: float = 0.25
    max_spread_frac_of_stop: float = 0.10
    # HOW LONG A SIGNAL STAYS ACTIONABLE.
    #
    # A clock is the wrong instrument for this and 30 minutes was two M15 bars.
    # A setup is dead when its STRUCTURE is dead — the trigger level is lost, the
    # sweep is undone, the displacement origin is traded through — and none of
    # those events keep to a timer. A trade that was valid at minute 29 is not
    # usually invalid at minute 31, and discarding it is a quota on opportunity
    # measured in seconds.
    #
    # This is now a DISPLAY hint on the notification ("valid ~Nm"), long enough
    # not to bin live setups, and registered as entry.signal_ttl so it has to
    # earn its keep. The real invalidation is the analyst's stated one, which
    # travels with the signal and is what a human should act on.
    default_ttl_minutes: int = 240
    max_tick_age_s: float = 120.0


@dataclass(frozen=True)
class CompiledSignal:
    """An entry proposal. This module does NOT manage the trade.

    Profit-lock, partial banking, runner trailing, profitable-stopout and
    re-entry are a separate state machine with its own evidence. Everything
    below is the handoff contract to it — entry, both objectives, and the
    structural anchors it needs to trail against.
    """
    direction: Literal["LONG", "SHORT"]
    setup: Setup
    entry: float
    stop: float
    tp1: float           # partial bank
    tp2: float           # runner objective
    risk: float          # price distance entry->stop
    rr_tp1: float        # net of the canonical cost model
    rr_tp2: float
    cost_r: float
    breakeven_win_rate: float   # computed, never asserted in a prompt
    ttl_minutes: int
    confidence: int
    trigger_price: Optional[float]
    stop_anchor_ref: str        # the level the trail should respect
    router_advisories: list[str]
    read: str
    why: str
    why_not: str
    invalidation: str
    brief_as_of: datetime
    # -- new contract surface. Beliefs travel with the signal so the forward
    # ledger can resolve them; none of them gate the trade. --
    mechanism_name: str = ""
    setup_tag: Optional[str] = None
    novelty: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    entry_intent: Literal["LIMIT", "BREAK", "MARKET", "PULLBACK"] = "MARKET"
    expected_holding_hours: Optional[float] = None
    path: Optional[PathForecast] = None
    visual_zones: tuple[str, ...] = ()      # resolved by the compiler
    adversarial: Optional[AdversarialReview] = None

    def to_management_handoff(self) -> dict:
        """Hand to the position/management engine. It owns everything after fill."""
        return {
            "direction": self.direction,
            "entry": self.entry,
            "initial_stop": self.stop,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "risk_price": self.risk,
            "stop_anchor_ref": self.stop_anchor_ref,
            "setup": self.setup.value,
            "opened_from_trigger": self.trigger_price,
        }


@dataclass(frozen=True)
class Refusal:
    """A read that did not become a signal. Journal these — they are the ledger."""
    reason: str
    read: Optional[AnalystRead]
    brief_as_of: datetime
    vetoed_by_compiler: bool


def compile_signal(
    brief: MarketBrief,
    read: AnalystRead,
    thresholds: Thresholds = Thresholds(),
    cost_model: CostModel = CostModel(),
    cohorts: Optional[dict] = None,
    tf_reads: Sequence["TimeframeRead"] = (),
) -> CompiledSignal | Refusal:
    """Resolve the model's refs to prices and apply every gate.

    The model proposes structure. Nothing it said about magnitude survives to
    here — entry, stop, target, R:R and cost are all computed below.
    """
    def refuse(reason: str, by_compiler: bool = True) -> Refusal:
        return Refusal(reason, read, brief.as_of_utc, by_compiler)

    # THE VERDICT IS `action`, never a family label. Old reads spell refusal
    # with direction="NONE"; the new contract spells it action="NO_TRADE".
    if read.is_no_trade():
        return refuse("analyst: NO_TRADE", by_compiler=False)

    if brief.tick_age_s > thresholds.max_tick_age_s:
        return refuse(f"stale tick ({brief.tick_age_s:.0f}s) — quote not trusted")

    # MULTI-TIMEFRAME VETO. Ruled here rather than in the prompt because a prompt is advice and
    # this is a refusal: a model that reads "H4 is opposed" can still weigh it to zero, and an
    # entry into a confirmed higher-timeframe impulse is not a slightly-worse trade but a
    # different one. Only COUNTER_HARD refuses -- see hierarchical_bias for why COUNTER_SOFT must
    # not, and why an EXHAUSTED opposing trend downgrades to soft.
    #
    # Empty `tf_reads` means the caller did not compute them, which is NOT the same as alignment.
    # `assess` returns NEUTRAL there and nothing is vetoed, so an un-wired caller keeps today's
    # behaviour instead of silently gaining a veto it never asked for.
    if tf_reads:
        from .hierarchical_bias import assess as _assess          # noqa: PLC0415
        _bias = _assess("BUY" if read.direction == "LONG" else "SELL", tf_reads)
        if _bias.vetoed:
            return refuse(f"hierarchical bias: {_bias.why}")

    # Resolve refs. A ref that is not a real, confirmed level is fatal.
    stop_lvl = brief.level(read.stop_ref)
    tp1_lvl = brief.level(read.tp1_ref)
    tp2_lvl = brief.level(read.tp2_ref)
    if stop_lvl is None:
        return refuse(f"stop_ref {read.stop_ref!r} is not a level in this brief")
    if tp2_lvl is None:
        return refuse(f"tp2_ref {read.tp2_ref!r} is not a level in this brief")
    if not stop_lvl.confirmed or not tp2_lvl.confirmed:
        return refuse("refs point at an unconfirmed level")

    long = read.direction == "LONG"

    # ---- Empirical cohort gate. Runs BEFORE geometry: no amount of clean
    # arithmetic rescues a cohort the evidence says is negative, and the model's
    # prose has no vote here.
    with_trend = (
        (long and brief.context.trend_direction == "UP")
        or (not long and brief.context.trend_direction == "DOWN")
    )
    # The family filed is the DESK'S post-hoc classification, never the model's
    # own constraint. Route advisories are advisory; the key just needs to be
    # stable so a router row can target a family cohort at all.
    effective_setup = classify_family(read)
    # NOTE: named `route_verdict`, not `verdict`. It previously shared the name
    # with the expectancy verdict computed below, which silently rebound it and
    # made the final `router_advisories=verdict.advisories` an AttributeError on
    # EVERY signal that got that far — i.e. the compiler could never emit one.
    route_verdict = route({
        "setup": effective_setup.value,
        "trend_health": brief.context.trend_health,
        "trend_direction_vs_trade": "WITH" if with_trend else "AGAINST",
        "session": brief.session,
        "volatility_state": brief.context.volatility_state,
    })
    if not route_verdict.permitted:
        return refuse(f"edge router: {route_verdict.reason}")

    if read.entry_ref == "MARKET":
        entry = brief.mid
    else:
        entry_lvl = brief.level(read.entry_ref)
        if entry_lvl is None or not entry_lvl.confirmed:
            return refuse(f"entry_ref {read.entry_ref!r} unusable")
        entry = entry_lvl.price

    # Stop sits beyond the level by an ATR buffer — the compiler decides how far.
    buf = brief.atr * thresholds.stop_atr_buffer
    stop = stop_lvl.price - buf if long else stop_lvl.price + buf
    tp2 = tp2_lvl.price
    tp1 = tp1_lvl.price if tp1_lvl and tp1_lvl.confirmed else None

    # Geometry sanity. Catches an inverted read before it can cost anything.
    if long and not (stop < entry < tp2):
        return refuse(f"inverted LONG geometry: stop {stop:.2f} entry {entry:.2f} tp2 {tp2:.2f}")
    if not long and not (tp2 < entry < stop):
        return refuse(f"inverted SHORT geometry: stop {stop:.2f} entry {entry:.2f} tp2 {tp2:.2f}")

    risk = abs(entry - stop)
    if risk <= 0:
        return refuse("zero risk distance")
    if tp1 is None:
        tp1 = entry + (tp2 - entry) * 0.5     # default partial at the midpoint

    # ---- Canonical cost. ONE function, shared with research/backtest.py.
    # Prices here are mid, so the round trip is charged exactly once.
    #
    # THE SPREAD CHARGED IS YOUR VENUE'S, NOT THE FEED'S — when you have told
    # the desk what your venue charges. Perception comes from OANDA or MT5; you
    # execute somewhere else by hand. Charging the feed's spread prices every
    # trade against a cost you will not pay, and retail gold brokers are usually
    # WIDER than the feed, so the error runs in the direction that makes the
    # desk trade more. `provenance` is stamped on the signal so a month of
    # decisions priced against the wrong venue is discoverable.
    from .venue import effective_spread
    charged_spread, cost_provenance = effective_spread(
        brief.spread, getattr(cost_model, "spread_profile", None), brief.session)
    cost_price = round_trip_cost(charged_spread, cost_model)
    cost_r = cost_price / risk
    rr_tp1 = (abs(tp1 - entry) - cost_price) / risk
    rr_tp2 = (abs(tp2 - entry) - cost_price) / risk

    if brief.spread > risk * thresholds.max_spread_frac_of_stop:
        return refuse(
            f"spread {brief.spread:.2f} exceeds {thresholds.max_spread_frac_of_stop:.0%} "
            f"of a {risk:.2f} stop — this trade starts {cost_r:.3f}R down"
        )
    # EXPECTANCY GATE. A 1.2R trade at a measured 60% hit rate is +0.26R and is
    # taken; a 3R trade at 20% is -0.20R and is not. Reward-to-risk alone can
    # decide neither, and using it as the gate discarded positive-value trades.
    # Novelty enters as an epistemic datum: HIGH novelty widens the cold-start
    # prior (see opportunity.ev_gate) — a measured uncertainty charge, not a ban.
    ev_verdict = ev_gate(rr_tp2, cost_r, read.mechanism_name, cohorts,
                         fallback_min_rr=thresholds.fallback_min_rr,
                         min_ev_r=thresholds.min_ev_r,
                         novelty_level=read.novelty)
    if not ev_verdict.take:
        return refuse(f"expectancy gate: {ev_verdict.reason}")

    # ---- Anti-chase, measured from the STRUCTURAL TRIGGER, not the entry.
    # Measuring from a MARKET entry gives drift 0 by construction, which made
    # the old gate unreachable for exactly the entries most likely to be chases.
    live = brief.mid
    if brief.trigger_price is None:
        if read.entry_ref == "MARKET":
            return refuse("MARKET entry with no trigger_price — drift is unmeasurable")
        origin = entry
    else:
        origin = brief.trigger_price
    drift_r = (live - origin) / risk if long else (origin - live) / risk
    if drift_r > thresholds.max_entry_drift_r:
        return refuse(
            f"price ran {drift_r:.2f}R from the trigger at {origin:.2f} — do not chase"
        )

    # ---- The new contract's money fields. An ACTIONABLE read is a thesis, and
    # thesis discipline means a stated path and a structured adversarial case.
    # This gate runs LAST so a cheap geometry/refusal verdict keeps priority;
    # it exists to make the analyst degenerate to refusal instead of drifting
    # into confident prose without the beliefs the desk resolves forward.
    if read.adversarial is None or not read.adversarial.is_complete():
        return refuse("ACTIONABLE read missing its adversarial case")
    if read.path is None or not read.path.is_complete():
        return refuse("ACTIONABLE read missing its path forecast")

    # ---- Visual regions -> price zones. The model names meaning; the compiler
    # locates it. A region carrying an invented price is dropped, never priced.
    visual_zones = tuple(
        z for z in (resolve_region(brief, r) for r in read.visual_regions) if z)

    return CompiledSignal(
        direction="LONG" if long else "SHORT",
        setup=effective_setup,
        entry=round(entry, 2),
        stop=round(stop, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        risk=round(risk, 2),
        rr_tp1=round(rr_tp1, 2),
        rr_tp2=round(rr_tp2, 2),
        cost_r=round(cost_r, 4),
        breakeven_win_rate=round(breakeven_win_rate(rr_tp2, cost_r), 3),
        ttl_minutes=thresholds.default_ttl_minutes,
        confidence=read.confidence,
        trigger_price=brief.trigger_price,
        stop_anchor_ref=read.stop_ref,
        router_advisories=(list(route_verdict.advisories) + [ev_verdict.reason]
                           + [f"cost from {cost_provenance}"]),
        read=read.read,
        why=read.why,
        why_not=read.why_not,
        invalidation=read.invalidation,
        brief_as_of=brief.as_of_utc,
        mechanism_name=read.mechanism_name,
        setup_tag=read.setup_tag,
        novelty=read.novelty,
        entry_intent=read.entry_intent,
        expected_holding_hours=read.expected_holding_hours or read.path.expected_holding_hours,
        path=read.path,
        visual_zones=visual_zones,
        adversarial=read.adversarial,
    )


# --------------------------------------------------------------------------
# build_brief lives in runner.py — it is a real implementation over a BarSource,
# not a seam. Import it from there rather than reimplementing it here.
# --------------------------------------------------------------------------


def analyse(
    brief: MarketBrief,
    charts: Sequence[Chart] = (),
    thresholds: Thresholds = Thresholds(),
):
    """Full pass. Returns CompiledSignal | Refusal. Never raises on refusal."""
    try:
        read = call_analyst(brief, charts)
    except AnalystError as e:
        return Refusal(f"analyst unavailable: {e}", None, brief.as_of_utc, True)
    return compile_signal(brief, read, thresholds)
