"""What happened last time the desk stood here. Precedent, not statistics.

WHAT THE ANALYST COULD NOT SEE. The brief describes the present in full and the
past not at all. The desk holds a ledger of every state it has traded, what it
predicted, and what the market then did — and none of it reached the reasoning
layer. The one number that did, `similarity_to_history`, is a scalar: it says
"this state is 62% covered by your history" and cannot say WHICH trades, WHAT
happened in them, or WHERE they differ from now. A coverage score is a warning
light; a memory pack is evidence.

    "This resembles the sweep-reclaims from last week, but every one of those
     had the dollar falling and this one does not, and two of the three gave
     back most of their MFE before the target."

That is a sentence a reasoner can produce from precedent and cannot produce from
0.62.

PRECEDENT IS NOT A RATE, AND THIS SAYS SO EVERY TIME IT PRINTS. Eight retrieved
analogues are eight anecdotes. They are genuinely useful — a human trader's
memory works exactly this way, and it is the right shape of input for a
reasoning model — but the moment someone counts them ("5 of 8 worked, so 62%")
they become a statistic computed from the eight most similar trades, which is
selection on the outcome variable's neighbours and is worth nothing. The header
says it, and `barriers.py` is where an actual rate comes from.

ONE DISTANCE FUNCTION, NOT TWO. Similarity comes from `regime.context_similarity`
— the same function `assess_novelty` uses. A second, private notion of "similar"
would eventually disagree with the first, and then the desk would have two
answers to how novel a state is, both computed and neither reconciled.

WHAT EACH ANALOGUE CARRIES. What it was, what the desk did, what it made, how
far it went the right way and the wrong way first — and the dimensions on which
it DIFFERS from now, which is the part that stops a superficial resemblance
being read as a match.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

log = logging.getLogger(__name__)

MEMORY_PACK_VERSION = "mempack-2026-08-29-a"

#: How many analogues to carry. Enough to show a pattern, few enough that the
#: block stays readable inside a prompt that already has a lot in it.
K = 8

#: Below this similarity an analogue is not a precedent, it is a different
#: market with some fields that happen to agree. Not tuned: it is the midpoint,
#: and a threshold chosen to make the pack look fuller would be choosing what
#: the analyst gets to remember.
MIN_SIMILARITY = 0.5

#: Context fields worth naming when they differ. The whole set would be noise;
#: these are the ones that change what a setup means.
NAMED = ("trend_direction", "trend_health", "trend_maturity", "volatility_state",
         "displacement_state", "sweep_state", "reclaim_state", "session",
         "pullback_depth", "distance_from_session_extreme")


@dataclass(frozen=True)
class Analogue:
    similarity: float
    when: str
    direction: str
    mechanism: str
    setup: str
    realised_r: float
    mfe_r: Optional[float]
    mae_r: Optional[float]
    reason: str
    differs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"similarity": round(self.similarity, 3), "when": self.when,
                "direction": self.direction, "mechanism": self.mechanism,
                "setup": self.setup, "realised_r": self.realised_r,
                "mfe_r": self.mfe_r, "mae_r": self.mae_r, "reason": self.reason,
                "differs": list(self.differs)}

    @property
    def line(self) -> str:
        exc = ""
        if self.mfe_r is not None and self.mae_r is not None:
            exc = f" (went {self.mfe_r:+.2f}R best, {self.mae_r:+.2f}R worst)"
        diff = (f"  DIFFERS: {', '.join(self.differs)}" if self.differs
                else "  (no named dimension differs)")
        return (f"  {self.similarity:.0%} alike · {self.when[:16]} · "
                f"{self.direction} {self.mechanism[:22]} · "
                f"{self.realised_r:+.2f}R via {self.reason}{exc}\n   {diff}")


@dataclass
class MemoryPack:
    analogues: tuple[Analogue, ...] = ()
    n_history: int = 0
    n_comparable: int = 0

    @property
    def empty(self) -> bool:
        return not self.analogues

    def to_dict(self) -> dict:
        return {"version": MEMORY_PACK_VERSION, "n_history": self.n_history,
                "n_comparable": self.n_comparable,
                "analogues": [a.to_dict() for a in self.analogues]}

    def render(self) -> str:
        if self.empty:
            return (f"PRECEDENT: none. {self.n_history} resolved trade(s) in the "
                    f"record, {self.n_comparable} comparable to this state, none "
                    f"above {MIN_SIMILARITY:.0%} similar. This is a state the "
                    f"desk has not stood in before — which is information, not a "
                    f"reason to refuse.")
        head = (f"PRECEDENT — the {len(self.analogues)} most similar states this "
                f"desk has actually traded, out of {self.n_comparable} comparable "
                f"({MEMORY_PACK_VERSION})")
        warn = ("  THESE ARE INDIVIDUAL TRADES, NOT A RATE. Do not count them: "
                "they were selected for resembling the present, so any "
                "percentage taken from them is selection on the neighbours of "
                "the thing being predicted. The measured rates are in the "
                "outcome distribution above.")
        return "\n".join([head, warn] + [a.line for a in self.analogues])


def _differs(current: dict, past: dict) -> tuple[str, ...]:
    out = []
    for k in NAMED:
        a, b = current.get(k), past.get(k)
        if a is None or b is None:
            continue
        if str(a) != str(b):
            out.append(f"{k}={b} then, {a} now")
    return tuple(out)


def build(current: dict, rows: Sequence[dict], *, k: int = K,
          min_similarity: float = MIN_SIMILARITY) -> MemoryPack:
    """The k most similar resolved states, with what they did. Pure; never raises.

    Reads `opportunity.resolved_outcomes`, the desk's single reader for resolved
    trades, so quarantined rows — whose paths were never observed and whose mfe
    and mae are zeros rather than measurements — are excluded here exactly as
    they are everywhere else.
    """
    try:
        from .opportunity import resolved_outcomes
        from .regime import context_similarity
        history = list(resolved_outcomes(list(rows)))
    except Exception as e:                                       # noqa: BLE001
        log.debug("memory pack unavailable: %s", e)
        return MemoryPack()

    scored: list[tuple[float, dict]] = []
    for h in history:
        ctx = h.get("context") or {}
        try:
            s = context_similarity(current, ctx)
        except Exception:                                        # noqa: BLE001
            s = None
        if s is None:
            continue
        scored.append((s, h))

    scored.sort(key=lambda t: (-t[0], str(t[1].get("ts") or "")))
    out: list[Analogue] = []
    for s, h in scored:
        if s < min_similarity or len(out) >= k:
            break
        ctx = h.get("context") or {}
        out.append(Analogue(
            similarity=s,
            # `closed_ts` and `t0` are what resolved_outcomes normalises to;
            # reading `ts` here silently produced "?" on every analogue, which
            # is a missing field that looks like a rendering choice.
            when=str(h.get("closed_ts") or h.get("t0") or "?"),
            direction=str(h.get("direction") or "?"),
            mechanism=str(h.get("mechanism_name") or "?"),
            setup=str(h.get("setup") or "?"),
            realised_r=float(h.get("realised_r") or 0.0),
            mfe_r=(float(h["mfe_r"]) if isinstance(h.get("mfe_r"), (int, float))
                   else None),
            mae_r=(float(h["mae_r"]) if isinstance(h.get("mae_r"), (int, float))
                   else None),
            reason=str(h.get("reason") or "?"),
            differs=_differs(current, ctx)))
    return MemoryPack(tuple(out), len(history), len(scored))
