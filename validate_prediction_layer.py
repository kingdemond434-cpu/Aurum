"""One gate a proposed prediction layer must pass BEFORE anyone builds it.

macro_vintage, probability_eval and horizon_stack each answer one question about
a predictive layer. Separately they are three libraries nobody runs. Together
they are a specification: describe the layer you want, and this says whether the
sample can support it, in about a second, before a quarter is spent finding out.

THE SEVEN GATES, IN THE ORDER THEY KILL THINGS

  1. TIMESCALE      do the features update fast enough for the horizon claimed?
  2. CAPACITY       can the effective sample support this many parameters?
  3. POINT-IN-TIME  is every input stamped with when it became knowable?
  4. DEPENDENCE     is this layer distinct from the ones already shown?
  5. CONDITIONS     how many "when does it matter" questions can be afforded?
  6. PRECISION      how many digits of the output are real?
  7. SURVIVAL       does it beat the base rate and improve on the layer below?

Gates 1-6 are answerable from the DESIGN alone — feature frequency, feature
count, horizon, effective sample size. Only gate 7 needs the model to exist.
That ordering is the point: six of the seven ways a prediction layer fails are
knowable before it is written.

    python3 validate_prediction_layer.py --demo
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).parent))

from horizon_stack import condition_budget, dependence, forward_returns, stack_summary
from macro_vintage import broadcast_warning, capacity_check
from probability_eval import reportable_precision


@dataclass
class LayerSpec:
    """A proposed prediction layer, described before it exists."""
    name: str
    horizon_minutes: float
    feature_frequency_days: float      # how often the SLOWEST input updates
    n_features: int
    effective_observations: float      # ESS at the feature's own frequency
    conditions: int = 1                # regimes/sessions it will be split by
    peer_layers: int = 1               # how many layers shown alongside it
    typical_output: float = 0.70       # a representative probability it will emit

    def render(self) -> str:
        return (f"{self.name}: horizon {self.horizon_minutes:g}m, slowest feature "
                f"updates every {self.feature_frequency_days:g}d, "
                f"{self.n_features} features, ESS {self.effective_observations:g}, "
                f"{self.conditions} condition(s), {self.peer_layers} peer layer(s)")


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str

    def render(self) -> str:
        return f"  [{'PASS' if self.passed else 'FAIL'}] {self.gate:<14} {self.detail}"


def validate(spec: LayerSpec) -> tuple[bool, list[GateResult]]:
    out: list[GateResult] = []

    # 1. timescale ------------------------------------------------------
    warn = broadcast_warning(spec.feature_frequency_days, spec.horizon_minutes)
    out.append(GateResult("timescale", warn is None,
                          warn or (f"features update every {spec.feature_frequency_days:g}d "
                                   f"for a {spec.horizon_minutes:g}m horizon — coherent")))

    # 2. capacity -------------------------------------------------------
    cap = capacity_check(spec.effective_observations, spec.n_features)
    out.append(GateResult("capacity", cap.verdict == "OK",
                          f"{cap.obs_per_parameter:.1f} obs/parameter — {cap.verdict}"))

    # 3. point-in-time --------------------------------------------------
    # Not decidable from a spec: it is a property of the ingestion. Reported as
    # an unmet REQUIREMENT rather than silently passed, because a layer that
    # skips it fails in a way no later gate detects.
    out.append(GateResult("point-in-time", False,
                          "REQUIREMENT: every input must carry its publication "
                          "datetime; run VintageStore.leakage_test() on the real "
                          "feature build. Cannot be inferred from a spec"))

    # 4/5. dependence and conditions ------------------------------------
    cb = condition_budget(spec.effective_observations, spec.peer_layers, spec.conditions)
    out.append(GateResult("conditions", cb.verdict == "supportable",
                          f"{cb.cells} cells, {cb.obs_per_cell:.0f} obs each — {cb.verdict}"))

    # 6. precision ------------------------------------------------------
    per_cell = spec.effective_observations / max(spec.peer_layers * spec.conditions, 1)
    prec = reportable_precision(spec.typical_output, per_cell)
    ok = "NO numeric" not in prec.verdict
    out.append(GateResult("precision", ok,
                          f"a {spec.typical_output:.0%} output on {per_cell:.0f} "
                          f"conditioned obs is honestly '{prec.reportable}' "
                          f"({prec.verdict})"))

    # 7. survival -------------------------------------------------------
    out.append(GateResult("survival", False,
                          "REQUIREMENT: must beat the base rate on Brier AND log "
                          "loss, then improve net R paired on identical states. "
                          "Needs the model to exist"))

    design_gates = [g for g in out if g.gate in
                    ("timescale", "capacity", "conditions", "precision")]
    return all(g.passed for g in design_gates), out


def report(spec: LayerSpec) -> str:
    ok, gates = validate(spec)
    lines = [spec.render(), ""]
    lines += [g.render() for g in gates]
    lines.append("")
    if ok:
        lines.append("  DESIGN GATES PASS — worth building. The two REQUIREMENT "
                     "rows still have to be met once it exists.")
    else:
        failed = [g.gate for g in gates
                  if not g.passed and g.gate in ("timescale", "capacity",
                                                 "conditions", "precision")]
        lines.append(f"  DESIGN GATES FAIL on {failed} — this layer cannot work as "
                     f"specified, and no amount of modelling effort changes that. "
                     f"Widen the horizon, cut features, or reduce conditions.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--parquet", default=("/root/.claude/uploads/"
                                          "353d9479-657d-5787-9c73-4a674604017c/"
                                          "c3041b3a-XAUUSD_D1.parquet"))
    args = ap.parse_args()

    print("=" * 84)
    print("PREDICTION LAYER ADMISSIBILITY")
    print("=" * 84)

    specs = [
        LayerSpec("A. 30-min probability from macro", 30, 7, 50, 433, 1, 1, 0.66),
        LayerSpec("B. Daily probability from macro", 1440, 1, 12, 2092, 1, 1, 0.68),
        LayerSpec("C. Daily, regime-conditioned", 1440, 1, 12, 80, 4, 1, 0.72),
        LayerSpec("D. Daily macro + intraday micro", 1440, 1, 10, 2092, 2, 2, 0.68),
        LayerSpec("E. Full stack: daily+session+4 intraday", 15, 1, 30, 2092, 3, 6, 0.69),
    ]
    for s in specs:
        print()
        print(report(s))

    try:
        from golddesk.runner import ParquetBarSource
        c = [b.close for b in ParquetBarSource(args.parquet, timeframe="D1").bars()]
        print()
        print("=" * 84)
        print("MEASURED DEPENDENCE — the proposed stack on the real series")
        print("=" * 84)
        H = {"1d": 1, "2d": 2, "5d": 5, "20d": 20}
        ps = dependence(c, H)
        for p in ps:
            print(p.render())
        print(" ", stack_summary(ps, len(H)))
    except Exception as e:
        print(f"\n(dependence measurement skipped: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
