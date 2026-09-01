"""The cheap wake + information planner (DeepFund planner + AI-Trader).

The watcher already gates "nothing changed". This planner gates "the market is
changing, but is anything information-rich happening", decides which specialist
tools matter for the state, and decides which causal branches to investigate.
It is deterministic and cheap, and its plan is advisory: it can only upgrade
effort (WATCH->ANALYZE), select which evidence to gather, and shape what the
model is asked. It never refuses and it never authorizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .brancher import active_branches

ALL_SEATS = ("atlas", "lumen", "apollo", "argus", "chronos", "orion",
             "mnemosyne", "hephaestus")


@dataclass
class WakePlan:
    tier: str                          # WATCH | ANALYZE | DEEP
    forced: bool                       # upgraded off the event bus
    specialists: tuple[str, ...] = ()
    branches: tuple = ()


def select_specialists(brief, *, deep: bool = False) -> tuple[str, ...]:
    """Which seats matter for THIS state (DeepFund planner)."""
    ctx = getattr(brief, "context", None)
    want: list[str] = ["atlas"]
    if ctx is not None:
        if ctx.displacement_state in ("CONFIRMED", "EXCEPTIONAL") or \
                ctx.sweep_state == "CONFIRMED" or ctx.reclaim_state == "CONFIRMED":
            want.append("lumen")
            want.append("argus")
            want.append("chronos")
        if ctx.volatility_state in ("HIGH", "LOW"):
            want.append("chronos")
    if deep:
        want += ["mnemosyne", "orion", "apollo", "hephaestus"]
    want.append("mnemosyne")           # memory is always relevant to a decision
    seen: list[str] = []
    for k in want:
        if k not in seen:
            seen.append(k)
    return tuple(k for k in ALL_SEATS if k in seen)


def plan(brief, verdict,
         forced: bool = False,
         minutes_to_event: Optional[float] = None,
         driver_divergences: int = 0) -> WakePlan:
    """Compile the wake plan from the cheap triage verdict + the event bus."""
    tier = verdict.mode
    was = tier
    if tier == "WATCH" and forced:
        tier = "ANALYZE"
    deep = tier == "DEEP"
    return WakePlan(
        tier=tier,
        forced=forced and was == "WATCH",
        specialists=() if tier == "WATCH" else select_specialists(brief, deep=deep),
        branches=tuple(active_branches(brief,
                                       minutes_to_event=minutes_to_event,
                                       driver_divergences=driver_divergences))
        if tier != "WATCH" else ())


def plan_render(p: WakePlan) -> str:
    if p.tier == "WATCH":
        return "WATCH — idle, nothing gathered."
    line = f"{p.tier}"
    if p.forced:
        line += " (event-forced)"
    if p.specialists:
        line += " | seats: " + ", ".join(p.specialists)
    if p.branches:
        line += " | branches: " + ", ".join(b.name for b in p.branches)
    return line