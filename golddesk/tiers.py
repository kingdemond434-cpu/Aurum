"""How much evidence stands behind THIS signal, said loudly enough to act on.

WHY THIS EXISTS

A conf-2 NOVEL counter-trend experiment and a conf-4 signal on a mechanism with
eighty resolved trades arrived on the phone looking almost identical: same
header, same price block, same shape. The difference was there -- `conf 2/5`,
`no measured edge yet for this mechanism`, `RISK estimation HIGH`, and a
`why_not` that said in plain words "filed NOVEL and expected to be shadowed
rather than sized" -- but it was scattered across five places, none of them the
first line, and the first line is what gets read on a phone.

An operator took one of those experiments with real money on 2026-08-27. The
message was not WRONG; every caveat was in it. It was UNRANKED, and an unranked
caveat is one the reader has to assemble themselves, at the moment they are
least inclined to.

WHAT THIS IS NOT

It is not a gate. Nothing here refuses a trade, moves a threshold, changes the
firing rate, or alters what reaches the ledger. Every signal that fired before
still fires and is still journalled identically. This changes one thing: the
first line of the message now states which of four evidence tiers the trade sits
in, and why. The desk deliberately fires low-evidence NOVEL trades to GENERATE
evidence -- that is the design -- and it should keep doing so. It should just
never let one look like a proven setup.

THE RANK IS COMPUTED FROM FACTS, NEVER FROM PROSE

Confidence is the model's opinion of itself and is used only to break ties
downward. What decides the tier is: whether the mechanism has resolved history
(the cohort), whether the setup family is a named one or NOVEL, whether the
structure the mechanism claims is CONFIRMED or merely asserted from context
fields, and whether the trade runs with or against higher-timeframe alignment.
Those are all measured elsewhere and simply read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

TIERS_VERSION = "tiers-2026-08-28-a"

#: Resolved trades before a cohort is treated as genuinely measured rather than
#: as a thin sample being shrunk toward a prior. Matches the bar `CohortStat`
#: itself uses for `informative`, and is stated here so a reader of a message
#: can find the number without reading opportunity.py.
MEASURED_N = 30


@dataclass(frozen=True)
class EvidenceTier:
    rank: int          # 1 strongest .. 4 experiment
    label: str
    why: str

    @property
    def banner(self) -> str:
        return f"*[T{self.rank} {self.label}]* _{self.why}_"


def evidence_tier(*, setup: str, mechanism_name: str, confidence: int,
                  sweep_state: str, reclaim_state: str, displacement_state: str,
                  htf_alignment: str, with_trend: bool,
                  cohort_n: int = 0, cohort_ev_r: Optional[float] = None
                  ) -> EvidenceTier:
    """Rank one signal by the evidence actually behind it.

    Every argument is a measured field. `confidence` is the single exception and
    is used only to demote, never to promote -- a model cannot talk its way into
    a higher tier, and the whole point of the ranking is lost if it can.
    """
    # T1 -- the mechanism has been traded enough to have an answer.
    if cohort_n >= MEASURED_N and (cohort_ev_r is None or cohort_ev_r > 0):
        return EvidenceTier(
            1, "MEASURED",
            f"{cohort_n} resolved trades on {mechanism_name!r}"
            + (f", EV {cohort_ev_r:+.2f}R" if cohort_ev_r is not None else ""))

    # T4 -- the experiments. Checked BEFORE T2/T3 because a NOVEL setup or an
    # unconfirmed mechanism is an experiment however clean the geometry looks.
    confirmed_structure = (sweep_state == "CONFIRMED"
                           or reclaim_state == "CONFIRMED"
                           or displacement_state in ("CONFIRMED", "EXCEPTIONAL"))
    if setup == "NOVEL":
        return EvidenceTier(
            4, "EXPERIMENT",
            f"NOVEL mechanism, no resolved history. Fired to GENERATE evidence — "
            f"the desk's own treatment is shadow, not size.")
    if not confirmed_structure:
        return EvidenceTier(
            4, "EXPERIMENT",
            "the mechanism is asserted from context fields — SWEEP, RECLAIM and "
            "DISPLACEMENT all read unconfirmed, so no structure confirms it")
    if not with_trend and htf_alignment == "ALIGNED":
        return EvidenceTier(
            4, "EXPERIMENT",
            "counter-trend into an HTF-ALIGNED move — the worst cohort this "
            "desk measures, taken to price it rather than because it is proven")

    # T2 -- named family, confirmed structure, model not hedging.
    if confidence >= 3:
        return EvidenceTier(
            2, "CONFIRMED",
            f"{setup} on confirmed structure"
            + (f", thin cohort n={cohort_n}" if cohort_n else ", no cohort history yet"))

    # T3 -- everything real but the model itself is unsure.
    return EvidenceTier(
        3, "UNMEASURED",
        f"{setup} on confirmed structure, but the analyst rates it "
        f"{confidence}/5 and no cohort has priced it")
