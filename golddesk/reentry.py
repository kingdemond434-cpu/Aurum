"""Re-entry as a fresh conditional decision — never 'stopped out, go again'.

A re-entry is a new setup that must clear the same bar as any other, PLUS
conditions specific to having just been in this trade. The previous trade's
path is evidence, not permission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

from .features import StructureState


@dataclass(frozen=True)
class PriorTrade:
    direction: Literal["LONG", "SHORT"]
    exit_reason: Literal["STOP", "PROFITABLE_STOP", "TARGET", "EXIT_THESIS", "TTL"]
    realised_r: float
    mfe_r: float
    mae_r: float
    exited_utc: datetime
    thesis_still_intact: bool


@dataclass(frozen=True)
class ReentryVerdict:
    allowed: bool
    reason: str


# `may_reenter()` USED TO LIVE HERE AND HAS BEEN DELETED.
#
# It was a free function carrying two restrictions that nothing had ever
# justified and no record ever disclosed: a 20-minute cool-off and a refusal to
# re-enter any trade whose prior attempt stopped out below +0.5R MFE. Both were
# written as if they were facts about gold. Both suppressed positive-EV
# opportunity, which the objective counts as a real economic cost.
#
# It is deleted rather than deprecated because it remained importable long after
# LiveDesk stopped calling it, and an importable gate with a hardcoded cool-off
# is one careless import away from being live again. The constitutional build
# check flagged exactly that.
#
# Superseded by policies.ReentryPolicy.evaluate, where every one of those values
# is a named, versioned, defaulted-to-zero hypothesis stamped onto each verdict
# so the ledger can say whether it ever earned its place.
