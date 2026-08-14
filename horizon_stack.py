"""Validating a multi-horizon prediction stack before it reaches the brain.

A daily / session / intraday stack is a good architecture and a dangerous
presentation. The architecture is good because a slow macro layer and a fast
microstructure layer genuinely see different things. The presentation is
dangerous because nested forecasts of the SAME asset, built from OVERLAPPING
information, agree with each other by construction — and displaying them as
separate lines manufactures confirmation that is not in the data.

    Daily model: 72% bullish
    London model: 65% bullish
    1h model: 69% bullish

reads as three agreeing opinions. Measured on real XAUUSD, adjacent horizons
agree 68-78% of the time on direction. Three tightly-spaced views agreeing is
close to one view stated three times, and a reader — human or model — will treat
it as corroboration.

This is the SAME defect as ten Telegram channels reposting one analyst. It
reappears here because the cause is identical: correlated sources presented as
independent. It is measured the same way and discounted the same way.

WHAT THIS MODULE DOES

  * measures directional agreement between horizons on your own series
  * flags pairs too dependent to present separately
  * tests whether a slower layer adds anything CONDITIONAL on a faster one,
    paired on identical timestamps
  * bounds how many "when does this horizon matter" conditions the sample can
    support before the answer is noise

It produces no probabilities and makes no forecast. It decides which layers are
worth showing and how many questions may honestly be asked of them.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

EPS = 1e-12


# --------------------------------------------------------------------------
# Dependence between horizons
# --------------------------------------------------------------------------

def forward_returns(closes: Sequence[float], horizon: int) -> list[float]:
    return [(closes[i + horizon] / closes[i] - 1.0)
            for i in range(len(closes) - horizon)]


def directional_agreement(a: Sequence[float], b: Sequence[float]) -> float:
    """Share of aligned observations where two views agree on sign."""
    n = min(len(a), len(b))
    if not n:
        return float("nan")
    return sum(1 for i in range(n) if (a[i] > 0) == (b[i] > 0)) / n


def effective_views(agreement: float) -> float:
    """Crude linear reading of an agreement rate as a count of opinions.

    1.0 agreement is one view stated twice; 0.5 is two independent views. This
    interpolates linearly between them, which is a rough heuristic and is stated
    as one — it is meant to make the size of the redundancy legible, not to be
    a statistic anyone reports. The agreement rate itself is the measurement.
    """
    return max(1.0, min(2.0, 3.0 - 2.0 * agreement))


@dataclass
class HorizonPair:
    a: str
    b: str
    agreement: float
    effective: float
    verdict: str

    def render(self) -> str:
        return (f"  {self.a:<6} vs {self.b:<6} agree {self.agreement:>5.0%}  "
                f"~{self.effective:.2f} independent view(s)  {self.verdict}")


def dependence(closes: Sequence[float], horizons: dict[str, int],
               redundant_above: float = 0.70) -> list[HorizonPair]:
    """Pairwise directional dependence across the stack, measured not assumed.

    `redundant_above` is a PRESENTATION threshold, not a trading one: above it,
    two layers should be shown as one line with a note rather than as two
    agreeing forecasts. It gates nothing about a trade and is declared here so
    it can be argued with.
    """
    names = list(horizons)
    fwd = {n: forward_returns(closes, h) for n, h in horizons.items()}
    out: list[HorizonPair] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ag = directional_agreement(fwd[a], fwd[b])
            v = ("REDUNDANT — collapse into one line" if ag >= redundant_above
                 else "distinct enough to show separately")
            out.append(HorizonPair(a, b, ag, effective_views(ag), v))
    return out


def stack_summary(pairs: Sequence[HorizonPair], n_layers: int) -> str:
    red = [p for p in pairs if "REDUNDANT" in p.verdict]
    if not red:
        return (f"{n_layers} layers, no redundant pair — the stack carries "
                f"roughly {n_layers} distinct views")
    # crude: each redundant pair collapses about half a view
    approx = max(1.0, n_layers - 0.5 * len(red))
    return (f"{n_layers} layers but {len(red)} redundant pair(s) — the stack "
            f"carries roughly {approx:.1f} distinct views, not {n_layers}. "
            f"Presenting all {n_layers} invites the brain to read agreement "
            f"between a model and itself as confirmation.")


# --------------------------------------------------------------------------
# Does a layer add anything, conditional on the layer below it?
# --------------------------------------------------------------------------

@dataclass
class ConditionalValue:
    layer: str
    given: str
    n_paired: int
    accuracy_alone: float
    accuracy_given: float
    lift: float
    verdict: str

    def render(self) -> str:
        return (f"  {self.layer:<10} given {self.given:<10} n={self.n_paired:<5} "
                f"alone {self.accuracy_alone:>5.1%}  conditional {self.accuracy_given:>5.1%}  "
                f"lift {self.lift:+.1%}\n  {'':<28}{self.verdict}")


def conditional_value(layer_pred: Sequence[int], given_pred: Sequence[int],
                      truth: Sequence[int], layer: str = "slow",
                      given: str = "fast", min_n: int = 100) -> ConditionalValue:
    """Does the slow layer still help WHERE THE FAST LAYER ALREADY SPOKE?

    The question is not "is the daily model accurate" — a daily model can be
    accurate and add nothing, if the intraday layer already carries the same
    call. The operative test is its accuracy on the subset where it DISAGREES
    with the faster layer, because agreement contributes no new information.
    """
    n = min(len(layer_pred), len(given_pred), len(truth))
    lp, gp, t = layer_pred[:n], given_pred[:n], truth[:n]
    alone = sum(1 for i in range(n) if lp[i] == t[i]) / max(n, 1)

    # DEGENERACY GUARD. If the reference layer is (near) perfect on this sample,
    # then every disagreement with it is wrong by construction and the test
    # reports a spectacular negative lift that means nothing. That is a property
    # of the comparison, not of the layer being judged, and it happens whenever
    # the baseline was fitted on the same labels it is being scored against.
    given_acc = sum(1 for i in range(n) if gp[i] == t[i]) / max(n, 1)
    if given_acc > 0.99:
        return ConditionalValue(
            layer, given, 0, alone, float("nan"), float("nan"),
            f"DEGENERATE — {given} is {given_acc:.0%} accurate on this sample, so "
            f"every disagreement is wrong by construction. Score against an "
            f"out-of-sample baseline, not one fitted to these labels")

    dis = [i for i in range(n) if lp[i] != gp[i]]
    if len(dis) < min_n:
        return ConditionalValue(
            layer, given, len(dis), alone, float("nan"), float("nan"),
            f"UNDETERMINED — only {len(dis)} disagreement(s); the layers rarely "
            f"differ, which is itself the finding: it is not adding a second view")
    cond = sum(1 for i in dis if lp[i] == t[i]) / len(dis)
    lift = cond - 0.5
    if lift > 0.02:
        v = (f"ADDS VALUE — when it contradicts {given} it is right {cond:.0%} "
             f"of the time; that is the information it contributes")
    elif lift < -0.02:
        v = (f"HARMFUL — when it contradicts {given} it is right only {cond:.0%}; "
             f"the disagreements are noise and {given} should be preferred")
    else:
        v = (f"NEUTRAL — coin-flip when it disagrees; it duplicates {given} and "
             f"earns no place in the presentation")
    return ConditionalValue(layer, given, len(dis), alone, cond, lift, v)


# --------------------------------------------------------------------------
# How many "when does this horizon matter" questions may be asked?
# --------------------------------------------------------------------------

@dataclass
class ConditionBudget:
    ess: float
    n_layers: int
    n_conditions: int
    cells: int
    obs_per_cell: float
    verdict: str

    def render(self) -> str:
        return (f"  {self.n_layers} layer(s) x {self.n_conditions} condition(s) "
                f"= {self.cells} cells on ESS {self.ess:.0f}\n"
                f"  {self.obs_per_cell:.0f} observations per cell — {self.verdict}")


def condition_budget(ess: float, n_layers: int, n_conditions: int,
                     min_obs_per_cell: float = 30.0) -> ConditionBudget:
    """"Learn which horizon matters when" is a partition, and partitions cost.

    Asking when the daily layer helps means splitting the sample by regime,
    session, volatility state and event proximity — and every split multiplies
    the cells while the sample stays the same size. Gold's monthly regime
    information is roughly 80 effective observations; four regimes across three
    layers is twelve cells and under seven observations each, at which point the
    answer to "when does this horizon matter" is whichever way the noise fell.
    """
    cells = max(n_layers * n_conditions, 1)
    per = ess / cells
    if per >= min_obs_per_cell:
        v = "supportable"
    elif per >= min_obs_per_cell / 2:
        v = ("MARGINAL — treat any conditional finding as a hypothesis to seal, "
             "never as a calibrated weight")
    else:
        v = (f"REFUSE — under {min_obs_per_cell/2:.0f} observations per cell. The "
             f"conditional structure will fit noise; ask fewer questions or "
             f"widen the conditions")
    return ConditionBudget(ess, n_layers, n_conditions, cells, per, v)


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

def presentation_rule(pairs: Sequence[HorizonPair]) -> str:
    """What the brain should actually be shown, given the measured dependence."""
    red = [p for p in pairs if "REDUNDANT" in p.verdict]
    lines = ["PRESENTATION RULE",
             "  Show a layer as its own line ONLY where it is measurably distinct.",
             "  For redundant pairs, show ONE line and state the dependence, so",
             "  agreement between a model and itself cannot be read as evidence."]
    if red:
        lines.append("  Collapse:")
        for p in red:
            lines.append(f"    {p.a} + {p.b}  (agree {p.agreement:.0%})")
    else:
        lines.append("  No collapse required on the measured horizons.")
    lines.append("  Attach the disagreement rate wherever two layers conflict —")
    lines.append("  conflict is information and should not be averaged away.")
    return "\n".join(lines)
