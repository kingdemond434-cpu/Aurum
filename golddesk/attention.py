"""Adaptive compute allocation (#5).

Sol High is scarce; idle states spend it on nothing. A cheap deterministic
watcher triages every wake into three tiers:

  WATCH   - nothing information-rich is happening. No model call. The pass is
            journalled (a state fingerprint) so the cost of the skip policy is
            measured, exactly like a refusal.
  ANALYZE - one normal call.
  DEEP    - maximum reasoning: the full chart pack, follow-up requests, a
            second adversarial look, and the specialists' context.

The triage must never *refuse* a genuinely information-rich state, so its
misses are visible: every WATCH carries its score and reasons, and reads from
then on are compared against the triage verdict in the ledger. If WATCH skips
start to correlate with moves, the threshold rises automatically (self-demotion
lives in review, not here — this module only measures).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: a confirmed level within this many ATRs of the live quote counts as "near"
NEAR_LEVEL_ATR = 0.6
#: volatility expansion z-score at which an ANALYZE earns the deep treatment
VOLATILITY_Z_DEEP = 1.5
#: spread above its own rolling 95th percentile "looks like regime"
SPREAD_PCT_DEEP = 0.90


@dataclass(frozen=True)
class AttentionConfig:
    enabled: bool = True
    volatility_z: Optional[float] = None      # None = unmeasured
    spread_pct: Optional[float] = None        # None = unmeasured
    minutes_to_event: Optional[float] = None  # None = no macro event near


@dataclass(frozen=True)
class AttentionVerdict:
    mode: str                                  # "WATCH" | "ANALYZE" | "DEEP"
    score: int
    reasons: tuple[str, ...] = ()
    near_level: bool = False


def _signals(brief, cfg: AttentionConfig) -> tuple[int, list[str], bool]:
    reasons: list[str] = []
    score = 0
    near = False

    ctx = brief.context
    for flag, words in (
        (ctx.displacement_state in ("CONFIRMED", "EXCEPTIONAL"),
         f"displacement {ctx.displacement_state}"),
        (ctx.sweep_state == "CONFIRMED", "confirmed sweep"),
        (ctx.reclaim_state == "CONFIRMED", "confirmed reclaim"),
    ):
        if flag:
            score += 2
            reasons.append(words)

    if brief.levels:
        confirmed = [abs(l.price - brief.mid) for l in brief.levels if l.confirmed]
        near = confirmed and min(confirmed) <= NEAR_LEVEL_ATR * brief.atr
        if near:
            score += 2
            reasons.append("price inside the nearest confirmed level's work zone")

    if cfg.volatility_z is not None and cfg.volatility_z > VOLATILITY_Z_DEEP:
        score += 2
        reasons.append(f"realized-vol z {cfg.volatility_z:.1f}")
    elif cfg.volatility_z is not None and cfg.volatility_z > 0.8:
        score += 1
        reasons.append(f"vol z {cfg.volatility_z:.1f}")

    if cfg.spread_pct is not None and cfg.spread_pct > SPREAD_PCT_DEEP:
        score += 1
        reasons.append("spread at the top of its own distribution")

    if cfg.minutes_to_event is not None and cfg.minutes_to_event <= 30:
        score += 1
        reasons.append(f"macro release in ~{cfg.minutes_to_event:.0f}m")

    return score, reasons, near


def triage(brief, cfg: AttentionConfig = AttentionConfig()) -> AttentionVerdict:
    """Decide how much reasoning this wake deserves."""
    if not cfg.enabled:
        return AttentionVerdict("ANALYZE", 0, ("attention disabled", ))
    try:
        score, reasons, near = _signals(brief, cfg)
    except Exception as e:                       # triage must never cost a trade
        log.warning("triage failed, defaulting to ANALYZE: %s", e)
        return AttentionVerdict("ANALYZE", 0, ("triage error, safe default", ))
    if score <= 0:
        return AttentionVerdict("WATCH", 0, ("idle state — no measured trigger", ), near)
    if score >= 4:
        return AttentionVerdict("DEEP", score, tuple(reasons), near)
    return AttentionVerdict("ANALYZE", score, tuple(reasons), near)