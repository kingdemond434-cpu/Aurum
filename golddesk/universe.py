"""The opportunity universe. Item #1.

WHAT WAS STRUCTURALLY MISSING

The desk asks the analyst one question — "what is the trade here?" — and gets
one answer. That interface caps realised capture at one thesis per wake no
matter what is actually available, and it does something worse than cap it: it
makes the surplus INVISIBLE. A second, independent, positive-expectancy
proposition that existed at the same moment is not refused, not journalled, and
not resolved forward. It never entered the record at all, so no amount of later
analysis can discover it was there.

Under an objective that counts missed positive-EV opportunity as an economic
cost, a one-answer interface is not a conservative choice. It is an unmeasured
restriction with no registry entry, and it is the largest one the desk had.

WHAT THIS ADDS

  ENUMERATE   the analyst returns every proposition it can state a mechanism
              for, in both directions, rather than picking a favourite in its
              own head where the discarded ones cannot be seen.

  COMPILE     every candidate goes through the SAME compile_signal, with the
              same gates, the same cost model and the same expectancy test. No
              candidate gets a discount for being the analyst's first choice.

  SELECT      what to take is decided by portfolio economics — expected value
              against correlation-adjusted risk consumption — not by rank order
              in the model's output.

  RECORD      everything NOT taken is journalled with full geometry, so the
              refusal ledger resolves it forward and the cost of the selection
              rule itself becomes measurable.

THE DISCIPLINE THAT KEEPS THIS FROM BECOMING A SCREENER

Nothing is dropped for being second best. If four candidates clear expectancy
and the heat budget has room for four, four are taken. Ranking only ever
matters when a budget actually binds, and when it binds the selection records
that fact — `budget_bound=True` — so the constitution can ask what the binding
cost. A "top-N" habit that quietly discards positive-EV propositions when
nothing was scarce is exactly the proxy-optimisation the objective forbids.

THE HONEST GAP

Ranking needs expected value, and a NOVEL mechanism with no resolved history
has none. Cold-start candidates therefore cannot be ranked on evidence. Rather
than inventing a proxy and calling it a measurement, the tiebreak is declared,
registered as DISCRETIONARY (`entry.universe_tiebreak`), stamped onto every
selection where it was load-bearing, and measured like any other restriction.
"""

from __future__ import annotations

import base64
import itertools
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError

from .analyst import (AnalystRead, CompiledSignal, MarketBrief, Refusal, Setup,
                      Thresholds, compile_signal)
from .chart import Chart
from .costs import CostModel
from .opportunity import CohortStat, Heat, ev_gate

log = logging.getLogger(__name__)

UNIVERSE_VERSION = "univ-2026-08-14-a"

# How many propositions the analyst may return.
#
# This is an OUTPUT-LENGTH bound and nothing else. It is not a view about how
# many opportunities a market is allowed to contain — an opportunity that
# happens to be the twelfth is worth exactly what it is worth, and a cap that
# silently drops it is a quota wearing an engineering justification.
#
# Set high enough that it should essentially never bind. When it does, the
# analyst says so explicitly (`had_more`) rather than leaving us to infer it
# from a full list, and the selection is stamped CAP BINDING. Two independent
# signals, because this is the one restriction here that cannot be measured
# from the ledger afterwards: an opportunity that was never stated leaves no
# trace at all, so detection has to happen at the moment of truncation.
MAX_CANDIDATES = 12


# --------------------------------------------------------------------------
# What the model returns when asked for the whole universe
# --------------------------------------------------------------------------

class AnalystUniverse(BaseModel):
    """Zero or more propositions plus an account of what was looked at.

    `candidates` may legitimately be empty: a moment with nothing tradeable is
    a real answer and the same one the single-read path gives as NO_SETUP.

    `survey` is not decoration. It is the record of what the analyst CONSIDERED
    and did not propose, which is the only view anyone will ever have of the
    layer above the candidate list. A universe that enumerates three ideas and
    says nothing about the fourth it dismissed has moved the invisible
    discarding one level up rather than removing it.
    """
    model_config = {"extra": "forbid"}

    candidates: list[AnalystRead] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    survey: str = Field(max_length=800, description=(
        "What you examined across timeframes and directions, including "
        "propositions you considered and did not put forward, and why."))
    dominant_context: str = Field(max_length=300, description=(
        "The one structural fact that most constrains everything above."))
    had_more: bool = Field(default=False, description=(
        "True if you had further statable propositions and ran out of slots. "
        "This is the only way a truncated universe can ever be detected: an "
        "opportunity you did not state leaves no trace anywhere, so nothing "
        "downstream can recover it. Say so rather than silently dropping one."))


UNIVERSE_SCHEMA = AnalystUniverse.model_json_schema()
UNIVERSE_SCHEMA["additionalProperties"] = False


UNIVERSE_ADDENDUM = """\

## You are being asked for the UNIVERSE, not your favourite

Return every proposition you can state a mechanism for, up to {cap}. Both \
directions may appear in the same list, on different timeframes, at different \
levels, with different invalidations — that is not a contradiction, it is what \
a real book of ideas looks like. A deterministic selector downstream decides \
which of them get risk, using expected value and the portfolio's available \
heat. That decision is not yours and you should not pre-empt it by omitting \
the ones you privately rank lower.

Omitting a proposition is the one thing that cannot be undone downstream. A \
weak candidate that is enumerated gets refused by a gate, journalled, and \
resolved forward against what price actually did — so the desk learns whether \
the refusal was right. A proposition you leave out is not refused. It is \
invisible, permanently, and nothing can recover it.

So the bar for INCLUDING a candidate is: can you name who is trapped, who must \
act, or what flow is forced? If yes, put it in and let the arithmetic judge it. \
If no, it is not a setup and it does not belong in the list.

Return an empty `candidates` list when nothing qualifies. Zero is a real answer \
and carries no penalty. Do not manufacture a candidate to fill the list, and do \
not withhold one to look disciplined — both corrupt the record in the same way.

`survey` must say what you looked at and what you dismissed. `dominant_context` \
is the single structural fact that most constrains the whole list.

If you run out of slots while you still have statable propositions, set \
`had_more` to true. The cap is an output-length bound, not a view about how many \
opportunities a market may contain, and it is the one limit here whose cost \
cannot be recovered later — an opportunity you never stated leaves no trace \
anywhere. Saying so is what gets the cap raised.
"""


def universe_system(base_system: str, cap: int = MAX_CANDIDATES) -> str:
    return base_system + UNIVERSE_ADDENDUM.format(cap=cap)


# --------------------------------------------------------------------------
# One enumerated proposition, compiled
# --------------------------------------------------------------------------

Disposition = Literal["PENDING", "TAKEN", "GATED", "DEFERRED"]
# PENDING is the default and it matters: a candidate that has compiled but not
# yet been through selection is neither taken nor refused, and defaulting it to
# GATED would have made every viable proposition invisible to the selector — the
# exact blindness this module exists to remove, reintroduced one layer down.


@dataclass
class Candidate:
    """A proposition and everything the desk concluded about it.

    A candidate that never becomes a trade still carries its full compiled
    geometry when it had one, because that is what lets the refusal ledger
    resolve it forward. A refusal without geometry is an opinion; a refusal with
    entry, stop and target is a measurable counterfactual.
    """
    index: int
    read: AnalystRead
    compiled: Optional[CompiledSignal]
    refusal: Optional[Refusal]
    ev_r: Optional[float]              # None when the mechanism has no history
    ev_basis: str
    disposition: Disposition = "PENDING"
    disposition_reason: str = ""
    risk_consumed_r: float = 0.0
    #: The scoreable features of this proposition, in the same units the ledger
    #: records them in. Populated by compile_universe; empty when no context was
    #: available, which is a real state rather than a set of zeros.
    rank_features: dict = field(default_factory=dict)
    #: Votes from `ranker`: +1 for each feature that has DEMONSTRATED it predicts
    #: realised R and on whose better side this candidate falls. Zero whenever
    #: nothing has been demonstrated — which is the desk's state today and will
    #: be for weeks — and in that state ordering is byte-identical to before.
    rank_votes: int = 0

    @property
    def viable(self) -> bool:
        return self.compiled is not None

    @property
    def direction(self) -> str:
        return self.compiled.direction if self.compiled else self.read.direction

    @property
    def mechanism(self) -> str:
        return self.read.mechanism_name

    def zone(self) -> Optional[tuple[float, float]]:
        """The price band this proposition lives in, stop to objective."""
        if not self.compiled:
            return None
        c = self.compiled
        return (min(c.stop, c.tp2), max(c.stop, c.tp2))

    def render(self) -> str:
        ev = "unmeasured" if self.ev_r is None or math.isnan(self.ev_r) \
            else f"{self.ev_r:+.3f}R"
        if self.compiled:
            c = self.compiled
            geom = (f"{c.direction} {c.entry:.2f} sl {c.stop:.2f} tp {c.tp2:.2f} "
                    f"rr {c.rr_tp2:.2f}")
        else:
            geom = f"{self.read.direction} (no geometry)"
        return (f"  [{self.index}] {self.disposition:<9} {self.mechanism[:24]:<24} "
                f"{geom}\n"
                f"      EV {ev} ({self.ev_basis})\n"
                f"      {self.disposition_reason}")

    def to_journal(self) -> dict:
        d = {"index": self.index, "mechanism_name": self.mechanism,
             "direction": self.direction, "setup": self.read.setup.value,
             "disposition": self.disposition,
             "disposition_reason": self.disposition_reason,
             "ev_r": None if self.ev_r is None or math.isnan(self.ev_r) else round(self.ev_r, 4),
             "ev_basis": self.ev_basis,
             "analyst_read": self.read.model_dump(),
             # THE VOTES AND WHAT THEY WERE COMPUTED FROM, on the row itself. A
             # ranking that reordered candidates and left no trace would be
             # unfalsifiable — "did the ordering help" is only answerable if the
             # score each candidate carried is in the record beside what it did.
             "rank_votes": self.rank_votes,
             "rank_features": {k: v for k, v in sorted(self.rank_features.items())
                               if v is not None},
             "universe_version": UNIVERSE_VERSION}
        if self.compiled:
            c = self.compiled
            d |= {"entry": c.entry, "stop": c.stop, "tp1": c.tp1, "tp2": c.tp2,
                  "risk_price": c.risk, "rr_tp2": c.rr_tp2, "cost_r": c.cost_r}
        if self.refusal:
            d["refusal"] = self.refusal.reason
        return d


def compile_universe(brief: MarketBrief, universe: AnalystUniverse,
                     thresholds: Thresholds = Thresholds(),
                     cost_model: CostModel = CostModel(),
                     cohorts: Optional[dict[str, CohortStat]] = None,
                     shadow: bool = False,
                     ) -> list[Candidate]:
    """Compile every proposition through the identical path.

    Deliberately not short-circuited. Compiling all of them costs microseconds
    and produces the geometry that makes a refusal measurable; stopping at the
    first viable candidate would restore exactly the blindness this module
    exists to remove.
    """
    from .ranker import features_for

    out: list[Candidate] = []
    for k, read in enumerate(universe.candidates):
        if read.setup is Setup.NO_SETUP or read.direction == "NONE":
            out.append(Candidate(k, read, None, None, None, "n/a",
                                 "GATED", "analyst returned NO_SETUP in a candidate slot"))
            continue
        res = compile_signal(brief, read, thresholds, cost_model, cohorts,
                             shadow=shadow)
        if isinstance(res, Refusal):
            out.append(Candidate(k, read, None, res, None, "n/a",
                                 "GATED", res.reason))
            continue
        v = ev_gate(res.rr_tp2, res.cost_r, read.mechanism_name, cohorts,
                    fallback_min_rr=thresholds.fallback_min_rr,
                    min_ev_r=thresholds.min_ev_r, shadow=shadow)
        ev = None if (v.ev_r is None or math.isnan(v.ev_r)) else v.ev_r
        c = Candidate(k, read, res, None, ev, v.basis)
        # THE SAME ARITHMETIC THE LEDGER RECORDS, computed here so that what
        # gets measured and what gets ranked are the same quantity. Attached to
        # every compiled candidate whether or not anything currently uses it:
        # a feature nobody scores today is still journalled, which is what makes
        # "did the votes predict anything" answerable later instead of never.
        c.rank_features = features_for(read, res, getattr(brief, "context", None))
        out.append(c)
    return out


# --------------------------------------------------------------------------
# Redundancy — the same bet twice, and the bet against itself
# --------------------------------------------------------------------------

def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Fraction of the smaller band that the two bands share."""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    if hi <= lo:
        return 0.0
    smaller = min(a[1] - a[0], b[1] - b[0])
    return (hi - lo) / smaller if smaller > 0 else 0.0


def redundancy(a: Candidate, b: Candidate, min_overlap: float = 0.6
               ) -> Optional[str]:
    """Is holding both of these one idea, or a fee-paying wash?

    SAME DIRECTION, overlapping band — one thesis expressed twice. Taking both
    is 2R on a single idea while the ledger records it as two independent
    observations, which corrupts the cohort statistics as well as the risk.

    OPPOSITE DIRECTION, overlapping band — each one's stop is close to the
    other's objective. Held simultaneously on a single instrument they carry
    almost no net exposure and pay two spreads for the privilege. That is not a
    hedge, it is a fee.

    NON-overlapping bands are left alone in both cases. A short into a level and
    a long from it are a legitimate sequence, and refusing that pair would be an
    invented restriction, not a risk control.
    """
    za, zb = a.zone(), b.zone()
    if not za or not zb:
        return None
    ov = _overlaps(za, zb)
    if ov < min_overlap:
        return None
    if a.direction == b.direction:
        return (f"same-direction thesis overlapping {ov:.0%} with candidate "
                f"[{a.index}] — one idea, not two")
    return (f"opposite-direction thesis overlapping {ov:.0%} with candidate "
            f"[{a.index}] — near-zero net exposure for two spreads")


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

@dataclass
class Selection:
    candidates: list[Candidate]
    taken: list[Candidate] = field(default_factory=list)
    budget_bound: bool = False
    tiebreak_used: bool = False
    ranking_used: bool = False
    ranking_version: str = ""
    truncated: bool = False
    analyst_had_more: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def deferred(self) -> list[Candidate]:
        return [c for c in self.candidates if c.disposition == "DEFERRED"]

    @property
    def gated(self) -> list[Candidate]:
        return [c for c in self.candidates if c.disposition == "GATED"]

    def render(self) -> str:
        lines = [f"OPPORTUNITY UNIVERSE ({UNIVERSE_VERSION})",
                 f"  enumerated {len(self.candidates)}   viable "
                 f"{sum(1 for c in self.candidates if c.viable)}   "
                 f"taken {len(self.taken)}   deferred {len(self.deferred)}   "
                 f"gated {len(self.gated)}"]
        lines += [c.render() for c in self.candidates]
        if self.budget_bound:
            lines.append("  BUDGET BOUND — at least one positive-EV candidate was "
                         "left because risk was scarce, not because it was weak.")
        else:
            lines.append("  Budget did not bind: nothing was dropped for ranking "
                         "below something else.")
        if self.ranking_used:
            lines.append(f"  MEASURED RANKING WAS LOAD-BEARING ({self.ranking_version}) "
                         f"— candidates were ordered by features that have "
                         f"demonstrated they predict realised R, and the order "
                         f"decided an allocation. Registered as entry.rank_votes.")
        if self.tiebreak_used:
            lines.append("  TIEBREAK WAS LOAD-BEARING — an unmeasured preference "
                         "decided a real allocation. Registered as "
                         "entry.universe_tiebreak and measured like any restriction.")
        if self.analyst_had_more:
            lines.append("  TRUNCATED — the analyst SAID it had further statable "
                         "propositions and ran out of slots. Raise MAX_CANDIDATES; "
                         "an unstated opportunity leaves no trace to recover.")
        elif self.truncated:
            lines.append(f"  CAP BINDING — the analyst filled all "
                         f"{MAX_CANDIDATES} slots without saying it had more; the "
                         f"universe may still be larger than what was enumerated.")
        lines += [f"  {n}" for n in self.notes]
        return "\n".join(lines)

    def to_journal(self) -> dict:
        return {"universe_version": UNIVERSE_VERSION,
                "enumerated": len(self.candidates),
                "taken": len(self.taken),
                "budget_bound": self.budget_bound,
                "tiebreak_used": self.tiebreak_used,
                "ranking_used": self.ranking_used,
                "ranking_version": self.ranking_version,
                "cap_binding": self.truncated,
                "analyst_had_more": self.analyst_had_more,
                "candidates": [c.to_journal() for c in self.candidates]}


def _sort_key(c: Candidate) -> tuple:
    """Ordering for the greedy pass. EV, then measured votes, then the declared
    tiebreak.

    Measured EV always outranks an unmeasured candidate — not because novelty is
    bad, but because an unknown quantity cannot be shown to beat a known
    positive one, and allocating scarce risk on the strength of the unknown is a
    claim the evidence does not support.

    THEN `rank_votes`, and its position is the point of the whole ranker. The
    desk's measured fault is SELECTION — taken trades resolved -0.14R while its
    refusals reached +0.56R — which is a statement about ORDER, not about
    volume or about gates. Votes sit below EV because a per-mechanism expectancy
    is a direct measurement of the same quantity, and above R:R because a
    feature that has cleared sample, Holm, cost and stability has demonstrated
    something R:R never has. Every vote is worth exactly one; there are no
    coefficients here to overfit.

    Among candidates the votes cannot separate, the order is net R:R after cost.
    That is the DECLARED tiebreak. It is a preference, not a measurement: R:R is
    half an expectancy calculation and this module says so everywhere else. It
    is used only when a budget binds, it is flagged when it is load-bearing, and
    the candidates it defers are journalled with geometry so its cost is
    recoverable from the forward record.

    WITH NO PUBLISHED RANKING every `rank_votes` is 0, the term is constant, and
    this key is byte-identical to the one that existed before the ranker did.
    """
    measured = c.ev_r is not None
    ev = c.ev_r if measured else float("-inf")
    rr = c.compiled.rr_tp2 if c.compiled else 0.0
    return (0 if measured else 1, -ev, -c.rank_votes, -rr, c.mechanism)


def select(candidates: Sequence[Candidate], heat: Heat,
           *, open_risks: Sequence[float] = (),
           open_directions: Sequence[str] = (),
           day_loss_r: float = 0.0,
           max_concurrent: int = 1,
           risk_per_trade_r: float = 1.0,
           min_overlap: float = 0.6,
           cap_filled: bool = False,
           analyst_had_more: bool = False,
           ranking=None) -> Selection:
    """Choose which enumerated propositions get risk.

    Order of operations matters and is deliberate:

      1. drop anything a gate already refused — those are not choices
      2. drop anything whose EV is measured and negative — not a budget question
      3. walk the survivors best-first, and for each one ask the portfolio
         whether there is room, in risk terms, after the correlation haircut
      4. skip anything redundant against what has already been taken
      5. record every survivor that was NOT taken, and WHY, distinguishing
         "the budget bound" from "it was the same bet" from "it was refused"

    `max_concurrent` comes from the desk, which reads it from the constitution.
    When `risk.one_position` is enforcing it is 1 and this function degenerates
    to picking a single best candidate — which is the current behaviour, now
    with the discarded alternatives written down.
    """
    sel = Selection(list(candidates), truncated=cap_filled or analyst_had_more,
                    analyst_had_more=analyst_had_more)

    # SCORE BEFORE SORTING, and read the ranking from disk unless one was passed.
    # Resolved here rather than in the signature default because a default
    # argument is evaluated once at import: the desk runs for days and the cycle
    # republishes nightly, so a captured default would freeze the ordering at
    # whatever was on disk when the process booted.
    if ranking is None:
        from .ranker import load as _load_ranking
        ranking = _load_ranking()
    sel.ranking_version = getattr(ranking, "version", "")
    for c in sel.candidates:
        c.rank_votes = ranking.score(c.rank_features) if c.rank_features else 0

    pool = [c for c in sel.candidates if c.viable]
    for c in pool:
        if c.ev_r is not None and c.ev_r <= 0:
            c.disposition = "GATED"
            c.disposition_reason = (f"expected value {c.ev_r:+.3f}R is not positive "
                                    f"on a measured cohort — scarcity is irrelevant")
    pool = [c for c in pool if c.disposition != "GATED"]
    pool.sort(key=_sort_key)

    unmeasured_in_contention = sum(1 for c in pool if c.ev_r is None)
    votes_split = len({c.rank_votes for c in pool}) > 1
    taken_risks = list(open_risks)
    taken_dirs = list(open_directions)

    for c in pool:
        # REDUNDANCY IS CHECKED FIRST, and the order is load-bearing. A
        # candidate that is the same bet as one already taken must be recorded
        # as redundant even when the concurrency ceiling would also have stopped
        # it. Attributing it to the ceiling would tell the refusal ledger that
        # the ceiling cost whatever that band went on to do — when in truth
        # taking it would have been one idea sized twice. The cheaper check
        # first would have quietly mis-billed the more expensive restriction.
        dup = None
        for t in sel.taken:
            dup = redundancy(t, c, min_overlap)
            if dup:
                break
        if dup:
            c.disposition = "DEFERRED"
            c.disposition_reason = dup
            continue

        if len(sel.taken) + len(open_directions) >= max_concurrent:
            # A COUNT SHOULD NEVER BE WHAT STOPS THIS. Heat is the economic
            # limit and normally binds long before any count does. If a count
            # binds first, either an operator set a runaway guard deliberately
            # or the risk arithmetic is not doing its job — both are worth
            # seeing, so it is logged as an anomaly rather than accepted as
            # routine.
            log.warning("concurrency COUNT bound at %d before portfolio heat did "
                        "— a count is a quota and should not be the binding "
                        "constraint on opportunity", max_concurrent)
            c.disposition = "DEFERRED"
            c.disposition_reason = (
                f"concurrency ceiling {max_concurrent} reached "
                f"({len(open_directions)} already open, {len(sel.taken)} taken here) "
                f"— deferred on a COUNT, which has no economics in it")
            sel.budget_bound = True
            continue

        same_dir = sum(1 for d in taken_dirs if d == c.direction)
        ok, why = heat.room_for(taken_risks, same_dir, risk_per_trade_r, day_loss_r)
        if not ok:
            c.disposition = "DEFERRED"
            c.disposition_reason = f"portfolio heat: {why} — deferred on a budget"
            sel.budget_bound = True
            continue

        c.disposition = "TAKEN"
        c.disposition_reason = f"selected — {why}"
        c.risk_consumed_r = risk_per_trade_r
        sel.taken.append(c)
        taken_risks.append(risk_per_trade_r)
        taken_dirs.append(c.direction)

    # The tiebreak was load-bearing only if a budget actually bound AND the
    # ordering among unmeasured candidates decided something. If everything
    # positive was taken, the sort order changed nothing and claiming otherwise
    # would overstate how much unmeasured preference is in the system.
    sel.tiebreak_used = bool(sel.budget_bound and unmeasured_in_contention >= 2)
    # The MEASURED ordering was load-bearing only if a budget bound AND the
    # votes actually differed between candidates. A ranking that scored every
    # candidate identically decided nothing, and recording it as decisive would
    # bill the ranker for an outcome the sort order did not produce.
    sel.ranking_used = bool(sel.budget_bound and votes_split)

    if not sel.taken and sel.candidates:
        sel.notes.append("nothing taken — every candidate was refused by a gate "
                         "or had non-positive measured EV")
    return sel


# --------------------------------------------------------------------------
# Calling for a universe
# --------------------------------------------------------------------------

def call_universe(brief: MarketBrief, charts: Sequence[Chart] = (), *,
                  client=None, model: Optional[str] = None,
                  effort: Optional[str] = None,
                  cap: int = MAX_CANDIDATES) -> AnalystUniverse:
    """One call, many propositions. Same prompt contract plus the addendum.

    The addendum goes AFTER the stable system text so the cached prefix is
    unchanged — a universe run and a single-read run share the same cache entry
    for everything up to the addendum.
    """
    import anthropic
    from .analyst import ANALYST_SYSTEM, MAX_TOKENS, MODEL, EFFORT

    client = client or anthropic.Anthropic()
    content: list[dict] = []
    for c in charts:
        content.append({"type": "text",
                        "text": f"Chart — {c.timeframe}, closed bars only:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(c.png).decode("ascii")}})
    content.append({"type": "text", "text": brief.render()})

    try:
        resp = client.messages.create(
            model=model or MODEL,
            max_tokens=MAX_TOKENS * 2,      # several propositions, not one
            system=[{"type": "text", "text": ANALYST_SYSTEM,
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": UNIVERSE_ADDENDUM.format(cap=cap)}],
            output_config={"effort": effort or EFFORT,
                           "format": {"type": "json_schema",
                                      "schema": UNIVERSE_SCHEMA}},
            messages=[{"role": "user", "content": content}])
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"api error {e.status_code}: {e.message}") from e

    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("universe truncated — raise MAX_TOKENS or lower the cap")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("no text block in response")
    try:
        return AnalystUniverse.model_validate_json(text)
    except ValidationError as e:
        raise RuntimeError(f"schema violation: {e}") from e


def as_universe(read: AnalystRead) -> AnalystUniverse:
    """Wrap a single read so every existing provider works in universe mode.

    A one-candidate universe is not a degenerate case to be apologised for — it
    is the honest representation of what a single-read provider knows. What it
    is NOT is evidence that only one opportunity existed, and the survey text
    says so rather than letting a downstream reader infer it.
    """
    cands = [] if read.setup is Setup.NO_SETUP else [read]
    return AnalystUniverse(
        candidates=cands,
        had_more=False,
        survey=("single-read provider: this is one proposition, not a survey. "
                "The absence of other candidates is a property of the interface, "
                "not a statement about the market."),
        dominant_context=read.read[:300] or "n/a")
