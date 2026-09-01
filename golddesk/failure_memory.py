"""Self-improving failure library (#6).

The desk records how it consistently loses, not just that it loses. Every
decision row and every lessons row is clustered into repeating archetypes, and
each cluster measures how often (and how expensively) the desk repeated it.
A current read that resembles a cluster the desk has already paid for is
flagged BEFORE the trade is sent — as information in the brief, never as an
auto-refusal (a veto would prevent ever learning whether the pattern broke).

The eight named archetypes the desk recognises are a starting taxonomy, not a
ceiling; every stored cluster carries the count and the R the failures cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# token -> archetype. Every stored row is scanned on these surfaces:
#   kind/decision, reason, decision.direction, decision.setup, context keys.
_PATTERNS: dict[str, tuple[str, str]] = {
    "breakout":      ("missed_breakout_acceleration", "the move that started without me"),
    "break":         ("missed_breakout_acceleration", "the move that started without me"),
    "acceleration":  ("missed_breakout_acceleration", "the move that started without me"),
    "countertrend":  ("wrong_countertrend_reversal", "a reversal into a healthy trend"),
    "counter-trend": ("wrong_countertrend_reversal", "a reversal into a healthy trend"),
    "retrace":       ("wrong_countertrend_reversal", "a reversal into a healthy trend"),
    "fade":          ("wrong_countertrend_reversal", "a reversal into a healthy trend"),
    "late":          ("late_entry", "the trade I arrived at after the move"),
    "slippag":       ("late_entry", "the trade I arrived at after the move"),
    "macro":         ("underestimated_macro_transmission", "a macro print that moved it for me"),
    "fed":           ("underestimated_macro_transmission", "a macro print that moved it for me"),
    "cpi":           ("underestimated_macro_transmission", "a macro print that moved it for me"),
    "nfp":           ("underestimated_macro_transmission", "a macro print that moved it for me"),
    "dxy":           ("ignored_gc_spot_divergence", "the cross-market story I ignored"),
    "gc/spot":       ("ignored_gc_spot_divergence", "the cross-market story I ignored"),
    "divergence":    ("ignored_gc_spot_divergence", "the cross-market story I ignored"),
    "profit":        ("premature_profit_lock", "the winner I closed before it ran"),
    "partial":       ("premature_profit_lock", "the winner I closed before it ran"),
    "tp1":           ("premature_profit_lock", "the winner I closed before it ran"),
    "visual":        ("false_visual_level", "the level I saw on the picture but not the table"),
    "level":         ("false_visual_level", "the level I saw on the picture but not the table"),
    "round number":  ("false_visual_level", "the level I saw on the picture but not the table"),
    "round":         ("false_visual_level", "the level I saw on the picture but not the table"),
    "no_trade":      ("no_trade_before_plus_2r", "refused — then it paid +2R"),
    "no-trade":      ("no_trade_before_plus_2r", "refused — then it paid +2R"),
    "refused":       ("no_trade_before_plus_2r", "refused — then it paid +2R"),
}

ARCHETYPES = ("missed_breakout_acceleration", "wrong_countertrend_reversal",
              "late_entry", "underestimated_macro_transmission",
              "ignored_gc_spot_divergence", "premature_profit_lock",
              "false_visual_level", "no_trade_before_plus_2r")


@dataclass
class FailureCluster:
    archetype: str
    meaning: str
    count: int = 0
    total_r: float = 0.0
    last_as_of: Optional[str] = None

    def add(self, r: Optional[float], as_of: Optional[str]) -> None:
        self.count += 1
        if r is not None and math.isfinite(r):
            self.total_r += r
        if as_of:
            self.last_as_of = as_of

    def render(self) -> str:
        if self.count == 0:
            return ""
        per = self.total_r / self.count
        return (f"  {self.archetype}: {self.count}x {per:+.2f}R avg "
                f"({self.total_r:+.2f}R total, last {self.last_as_of}) — {self.meaning}")


def _scan(row: dict) -> list[tuple[str, str]]:
    surfaces: list[str] = []
    for key in ("decided_by", "reason", "notes"):
        v = row.get(key)
        if isinstance(v, str):
            surfaces.append(v)
    decisions = row.get("decision")
    if isinstance(decisions, dict):
        for key in ("direction", "setup", "setup_tag", "mechanism"):
            v = decisions.get(key)
            if isinstance(v, str):
                surfaces.append(v)
    for key in ("context", "brief_render"):
        v = row.get(key)
        if isinstance(v, dict):
            for inner in v.values():
                if isinstance(inner, str):
                    surfaces.append(inner)
        elif isinstance(v, str):
            surfaces.append(v)
    blob = " ".join(surfaces).lower()
    hits = []
    for token, tagged in _PATTERNS.items():
        if token in blob and tagged not in hits:
            hits.append(tagged)
    return hits


def cluster_rows(rows: Iterable[dict]) -> list[FailureCluster]:
    """Cluster all past decision/lessons rows into the archetype library."""
    _meanings = {
        "missed_breakout_acceleration": "the move that started without me",
        "wrong_countertrend_reversal": "a reversal into a healthy trend",
        "late_entry": "the trade I arrived at after the move",
        "underestimated_macro_transmission": "a macro print that moved it for me",
        "ignored_gc_spot_divergence": "the cross-market story I ignored",
        "premature_profit_lock": "the winner I closed before it ran",
        "false_visual_level": "the level I saw on the picture but not the table",
        "no_trade_before_plus_2r": "refused — then it paid +2R",
    }
    clusters = {a: FailureCluster(a, _meanings[a]) for a in ARCHETYPES}
    for row in rows:
        for arch, meaning in _scan(row):
            c = clusters[arch]
            r = None
            out = row.get("outcome")
            if isinstance(out, dict):
                r = out.get("r") or out.get("realised_r")
            c.add(float(r) if isinstance(r, (int, float)) else None, row.get("as_of") or row.get("t0"))
    return [c for c in clusters.values() if c.count]


def failure_memory_block(rows: Iterable[dict], *, limit: int = 3) -> str:
    """The FAILURE MEMORY block for the brief: only clusters the desk has paid for,
    ranked by how expensive they were per repeat."""
    clusters = cluster_rows(rows)
    clusters.sort(key=lambda c: (c.count * abs(c.total_r) if c.total_r else 0.0), reverse=True)
    if not clusters:
        return ""
    body = "\n".join(c.render() for c in clusters[:limit])
    return f"FAILURE MEMORY\n{body}\n  This is what recurring failures cost. Weigh them."