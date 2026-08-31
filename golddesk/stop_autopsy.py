"""Was the thesis wrong, or was the stop simply in the way?

WHY THIS EXISTS

A stopped-out trade writes `realised_r: -1.0` and nothing else. That single
number cannot distinguish two situations that demand OPPOSITE fixes:

  THESIS WRONG    price went against the idea and kept going. The mechanism
                  does not work. Fix: stop trading it.

  STOPPED EARLY   price took out the stop and then went where the idea said it
                  would. The mechanism works. Fix: a wider stop, a later entry,
                  or a smaller size — all of which INCREASE capture.

Reading the first as the second builds a desk that keeps a broken mechanism.
Reading the second as the first kills a working one. A ledger that reports only
`-1.0R` invites both errors and warns of neither.

Observed 2026-08-27: a long entered at 4587.18 stopped at 4567, and price then
recovered to 4581.46 without the position. The idea was directionally right and
the trade still lost a full R. Nothing in the ledger said so.

WHERE THE ANSWER ALREADY IS

No new data collection is needed, which is why this is a reader and not a
collector. Every SIGNAL row carries `outcome`, resolved forward from the
DECISION MOMENT over the full forward window and COMPLETELY INDEPENDENT of the
stop -- `resolve_forward` never looks at where the stop was. So the SIGNAL row
already knows the best the idea ever got; the TRADE_CLOSED row knows what the
trade actually kept. The two were simply never joined.

WHAT THIS IS NOT

Not a gate. It reads the ledger and reports. Nothing here refuses a trade,
changes a stop, or moves a threshold -- a verdict here is an input to a decision
a human makes later, and the honest verdict on a single trade is that one trade
decides nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

STOP_AUTOPSY_VERSION = "stopsy-2026-08-28-a"

#: R of favourable excursion, AFTER the trade was stopped out, before the stop
#: is called premature rather than correct. Not tuned: one full risk unit is the
#: point at which the trade would have been scratch-or-better had the stop not
#: been hit, which is the plainest statement of "the stop was what cost it".
PREMATURE_R = 1.0

#: Closes that are stop-outs. A TARGET exit needs no autopsy.
STOP_REASONS = ("STOP", "PROFITABLE_STOP")


@dataclass(frozen=True)
class Autopsy:
    entry_t0: str
    mechanism: str
    direction: str
    realised_r: float
    #: Best the IDEA achieved, from the decision moment, ignoring the stop.
    idea_mfe_r: Optional[float]
    #: Worst it went first. A big MAE with a big MFE is a stop-placement
    #: question; a big MAE with no MFE is just a wrong idea.
    idea_mae_r: Optional[float]
    verdict: str
    why: str

    @property
    def premature(self) -> bool:
        return self.verdict == "STOPPED EARLY"


def _signal_index(rows: Sequence[dict]) -> dict:
    """SIGNAL rows keyed by their decision time, for joining to a close."""
    out = {}
    for r in rows:
        if r.get("kind") == "SIGNAL" and r.get("t0"):
            out[str(r["t0"])] = r
    return out


def autopsy(rows: Sequence[dict]) -> list[Autopsy]:
    """One verdict per stopped-out trade. Silent on trades that hit target."""
    sigs = _signal_index(rows)
    out: list[Autopsy] = []
    for r in rows:
        if r.get("kind") != "TRADE_CLOSED":
            continue
        if str(r.get("reason", "")) not in STOP_REASONS:
            continue
        key = str(r.get("entry_t0") or "")
        sig = sigs.get(key)
        realised = float(r.get("realised_r") or 0.0)
        mech = str(r.get("mechanism_name") or "unnamed")
        direction = str(r.get("direction") or "?")

        if sig is None:
            # UNMEASURED, not "the stop was fine". A close with no signal row to
            # join is a gap in the ledger, and reporting it as a clean verdict
            # is the exact defect this desk has a law against.
            out.append(Autopsy(key, mech, direction, realised, None, None,
                               "UNMEASURED",
                               "no SIGNAL row joins this close — the forward path "
                               "the idea would have taken was never recorded, so "
                               "whether the stop was premature is UNKNOWN"))
            continue

        oc = sig.get("outcome") or {}
        mfe, mae = oc.get("mfe_r"), oc.get("mae_r")
        if mfe is None:
            out.append(Autopsy(key, mech, direction, realised, None, mae,
                               "UNMEASURED",
                               "the SIGNAL row carries no resolved excursion — "
                               "forward resolution did not run or was truncated"))
            continue

        mfe = float(mfe)
        mae = None if mae is None else float(mae)
        if mfe >= PREMATURE_R:
            out.append(Autopsy(
                key, mech, direction, realised, mfe, mae, "STOPPED EARLY",
                f"the idea reached {mfe:+.2f}R after the stop took the trade out "
                f"at {realised:+.2f}R. The direction was right and the stop was "
                f"what cost it — a wider stop, a later entry or a smaller size "
                f"are the fixes, and each of them INCREASES capture"))
        else:
            out.append(Autopsy(
                key, mech, direction, realised, mfe, mae, "THESIS WRONG",
                f"the idea never got beyond {mfe:+.2f}R. Price went against it "
                f"and stayed there, so the stop is not what cost this trade"))
    return out


def render(items: Sequence[Autopsy]) -> str:
    """Report, with the sample size in front of every conclusion."""
    if not items:
        return ("STOP AUTOPSY — no stopped-out trades yet. That is an empty "
                "sample, not a clean bill of health for the stops.")
    lines = [f"STOP AUTOPSY ({STOP_AUTOPSY_VERSION}) — {len(items)} stop-out(s)"]
    early = [a for a in items if a.premature]
    wrong = [a for a in items if a.verdict == "THESIS WRONG"]
    unk = [a for a in items if a.verdict == "UNMEASURED"]
    for a in items:
        lines.append(f"  {a.entry_t0}  {a.direction:<5} {a.mechanism[:34]:<34} "
                     f"{a.verdict}")
        lines.append(f"      kept {a.realised_r:+.2f}R · idea reached "
                     f"{'UNMEASURED' if a.idea_mfe_r is None else f'{a.idea_mfe_r:+.2f}R'}")
    lines.append("")
    lines.append(f"  stopped early : {len(early)}")
    lines.append(f"  thesis wrong  : {len(wrong)}")
    if unk:
        lines.append(f"  UNMEASURED    : {len(unk)} — not counted either way")

    # THE SAMPLE-SIZE SENTENCE, always present. "3 of 4 stops were premature"
    # invites widening every stop on the strength of four trades, and the whole
    # value of this report dies the first time it is acted on that thinly.
    n = len(early) + len(wrong)
    if n < 20:
        lines.append(f"\n  n={n} DECIDES NOTHING. This is a description of what "
                     f"happened, not evidence about where stops belong. Read it "
                     f"per-mechanism at n>=20 before moving a single stop.")
    elif early and len(early) / n >= 0.5:
        lines.append(f"\n  {len(early)}/{n} stop-outs were premature. Worth testing "
                     f"a wider stop or a later entry PER MECHANISM — the pooled "
                     f"number hides that different mechanisms need different room.")
    return "\n".join(lines)
