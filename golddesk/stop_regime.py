"""Was the stop sized for the volatility the trade actually met?

THE COMPLAINT THIS EXISTS TO SETTLE, and it is a good one. On 2026-08-28 gold
fell roughly 140 points in an afternoon. The desk was SHORT repeatedly on the way
down — the direction was right nearly every time — and it lost money doing it,
because each stop was taken out on a small retrace before price continued another
sixty points the trade's way.

THE HYPOTHESIS, stated so it can be wrong. Stops are placed beyond structure by
`stop_atr_buffer * ATR`, and ATR is a TRAILING mean. In a volatility expansion a
trailing mean lags the market by construction, so a stop sized from it is too
tight by roughly the expansion ratio — and it is too tight EXACTLY when the
desk's directional calls are most likely to be right, because expansions are when
trends actually travel. A 21-point stop on a day whose range was 140 is not
conservative; it is a stop scaled to yesterday.

WHY THIS FILE MEASURES AND DOES NOT FIX. "Give gold more room" is the fix that
feels right, is trivial to implement, and is exactly what an overfit looks like
from the inside. The desk has fourteen resolved trades. Widening a stop on the
strength of one dramatic afternoon would be acting on a sample of one regime, and
the same reasoning would have widened stops into every chop day that followed.

So this records the two numbers that let the question be settled later:

    stop_in_atr    what the desk THOUGHT it was risking -- the trailing view
    stop_in_range  the same distance against the CURRENT bar's range -- the
                   regime-adjusted view

When those agree the market is behaving as its trailing average says. When
stop_in_range is far smaller, the stop is narrow relative to what price is doing
RIGHT NOW, and that is the condition the hypothesis is about. Group stopped-out
trades by it, and the answer arrives as a number rather than as anybody's
opinion about one afternoon.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Optional, Sequence

STOP_REGIME_VERSION = "stopregime-2026-08-28-a"

#: Stopped-out trades before the comparison below says anything. Eight is not
#: enough to conclude with; it is enough to stop the report saying UNMEASURED
#: while a real pattern accumulates underneath it.
MIN_STOPPED = 8

#: Above this, the current bar is "expanding" relative to its own recent mean.
#: NOT a threshold anything acts on -- it splits the sample for the report and
#: nothing else. The number is 1.5 because it is halfway to double, and if the
#: finding only exists at one precise cut it is not a finding.
EXPANDING_ABOVE = 1.5


def measure(stop_distance: float, atr: Optional[float],
            bar_range: Optional[float],
            range_vs_mean: Optional[float]) -> dict:
    """The regime context of one stop. Pure; every field Optional.

    `range_vs_mean` comes from candle_character and is the bar's range against
    its trailing mean -- above 1 expanding, below 1 compressing.
    """
    out: dict[str, Any] = {"stop_distance": round(float(stop_distance), 4)}
    if atr:
        out["stop_in_atr"] = round(stop_distance / atr, 3)
    if bar_range:
        # THE REGIME-ADJUSTED VIEW. A stop worth 2 ATR can be worth 0.4 of the
        # bar the market just printed, and it is the second number that says
        # whether an ordinary retrace reaches it.
        out["stop_in_range"] = round(stop_distance / bar_range, 3)
    if range_vs_mean is not None:
        out["range_vs_mean"] = round(float(range_vs_mean), 3)
        out["expanding"] = float(range_vs_mean) > EXPANDING_ABOVE
    return out


@dataclass(frozen=True)
class Verdict:
    n_stopped: int
    n_expanding: int
    verdict: str                                    # UNMEASURED | MEASURED
    stopped_rate_expanding: Optional[float] = None
    stopped_rate_calm: Optional[float] = None
    median_stop_in_range_expanding: Optional[float] = None
    median_stop_in_range_calm: Optional[float] = None

    def render(self) -> str:
        if self.verdict == "UNMEASURED":
            return (f"STOP REGIME: UNMEASURED — {self.n_stopped} stopped trade(s) "
                    f"carrying regime context, under {MIN_STOPPED}. Whether stops "
                    f"are too tight in expansions is an OPEN QUESTION, not a "
                    f"finding, and widening one on a hunch is how a desk overfits "
                    f"to a single afternoon.")
        lines = [f"STOP REGIME ({STOP_REGIME_VERSION}) — {self.n_stopped} stopped, "
                 f"{self.n_expanding} of them in an expansion"]
        if self.stopped_rate_expanding is not None:
            lines.append(
                f"  stopped-out share: {self.stopped_rate_expanding:.0%} in "
                f"expansions vs {self.stopped_rate_calm:.0%} in calm tape")
        if self.median_stop_in_range_expanding is not None:
            lines.append(
                f"  stop as a fraction of the bar's own range: "
                f"{self.median_stop_in_range_expanding:.2f} in expansions vs "
                f"{self.median_stop_in_range_calm:.2f} in calm — the smaller "
                f"number is the tighter stop relative to what price is doing")
        lines.append("  This is a COMPARISON, not a verdict. A wider stop is only "
                     "justified if the trades it saves outweigh the larger loss "
                     "on the ones it does not.")
        return "\n".join(lines)


def _regime(row: dict) -> dict:
    dec = row.get("decision") or {}
    return dec.get("stop_regime") or {}


def assess(rows: Sequence[dict]) -> Verdict:
    """Compare stop-outs in expanding tape against stop-outs in calm tape.

    Reads SIGNAL rows for the regime context and TRADE_CLOSED rows for the
    outcome, joined on entry_t0 the same way the rest of the desk joins them.
    """
    regime_by_t0 = {str(r.get("t0")): _regime(r)
                    for r in rows if r.get("kind") == "SIGNAL" and _regime(r)}
    stopped_exp = stopped_calm = total_exp = total_calm = 0
    ranges_exp: list[float] = []
    ranges_calm: list[float] = []

    for c in rows:
        if c.get("kind") != "TRADE_CLOSED":
            continue
        if c.get("evidence_valid") is False:
            continue                     # quarantined; see opportunity.resolved_outcomes
        reg = regime_by_t0.get(str(c.get("entry_t0")))
        if not reg or "expanding" not in reg:
            continue
        stopped = str(c.get("reason") or "").upper().endswith("STOP")
        if reg["expanding"]:
            total_exp += 1
            stopped_exp += int(stopped)
            if reg.get("stop_in_range") is not None:
                ranges_exp.append(reg["stop_in_range"])
        else:
            total_calm += 1
            stopped_calm += int(stopped)
            if reg.get("stop_in_range") is not None:
                ranges_calm.append(reg["stop_in_range"])

    n_stopped = stopped_exp + stopped_calm
    if n_stopped < MIN_STOPPED or not total_exp or not total_calm:
        return Verdict(n_stopped, stopped_exp, "UNMEASURED")
    return Verdict(
        n_stopped=n_stopped, n_expanding=stopped_exp, verdict="MEASURED",
        stopped_rate_expanding=stopped_exp / total_exp,
        stopped_rate_calm=stopped_calm / total_calm,
        median_stop_in_range_expanding=(statistics.median(ranges_exp)
                                        if ranges_exp else None),
        median_stop_in_range_calm=(statistics.median(ranges_calm)
                                   if ranges_calm else None))
