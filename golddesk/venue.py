"""The costs of the venue you actually execute on.

THE PROBLEM THIS FIXES

Aurum's price feed and Aurum's execution venue are DIFFERENT PLACES. Perception
comes from OANDA (or MT5); you place the trade by hand at your own broker. But
`compile_signal` charged `brief.spread` — the FEED's spread — as the cost of the
trade, and there was no way to say otherwise.

So every expectancy calculation, every breakeven win rate, every "is this trade
worth taking" was computed against a spread you will not pay. If your broker is
wider than the feed — and retail gold brokers usually are — the desk
systematically overestimates the value of every trade it considers, and it does
so in the direction that makes it trade more.

That is not a rounding error. On a $6 stop, a $0.30 feed spread is 0.05R and a
$0.60 broker spread is 0.10R. The difference decides marginal trades, and
marginal trades are most of them.

WHAT THIS DOES

A SpreadProfile describes what YOU pay, measured from your own venue's data,
BY SESSION — because gold's spread at the Asia open and at the London-NY
overlap are different numbers and averaging them describes neither.

WHAT IT REFUSES TO DO

Guess. An uncalibrated profile does not silently substitute a plausible default.
It reports UNCALIBRATED, the desk keeps using the feed spread, and every row is
stamped with which venue the cost came from — so a month of decisions priced
against the wrong venue is a discoverable fact rather than an invisible one.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)

VENUE_VERSION = "venue-2026-08-14-a"

SESSIONS = ("ASIA", "LONDON", "OVERLAP", "NY", "ROLLOVER")


@dataclass
class SpreadProfile:
    """What your broker charges, by session, measured rather than assumed.

    `by_session` maps session -> the spread to charge, in PRICE units. The
    statistic is the MEDIAN by default and the choice matters: the mean is
    dragged upward by rollover spikes, and a cost model that charges the mean
    refuses trades all day to pay for a few minutes at 22:00.

    `conservative` charges an upper quantile instead. Use it when the profile is
    thin — being slightly too pessimistic about cost is a much cheaper error
    than being optimistic, because optimism shows up as trades that looked
    positive and were not.
    """
    venue: str = "unknown"
    by_session: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    statistic: str = "median"
    calibrated_from: Optional[str] = None
    version: str = VENUE_VERSION

    @property
    def calibrated(self) -> bool:
        return bool(self.by_session)

    def spread_for(self, session: str) -> Optional[float]:
        """Your cost in this session, or None if it was never measured."""
        if not self.calibrated:
            return None
        s = self.by_session.get(session)
        if s is not None:
            return float(s)
        # An unmeasured session falls back to the WIDEST measured one, not the
        # average. An unknown session is most likely a thin one, and guessing
        # cheap in a thin market is the error that costs money.
        return max(self.by_session.values()) if self.by_session else None

    def render(self) -> str:
        if not self.calibrated:
            return ("  SPREAD PROFILE: UNCALIBRATED — costs are being taken from "
                    "the FEED, which is not where you execute.\n"
                    "  Every expectancy figure is therefore priced against a "
                    "spread you will not pay.\n"
                    "  Fix: calibrate from your own broker export (see "
                    "export_mt5.py), or declare one explicitly.")
        out = [f"  SPREAD PROFILE ({self.venue}, {self.statistic}, "
               f"{self.version})"]
        for s in SESSIONS:
            v = self.by_session.get(s)
            n = self.samples.get(s, 0)
            if v is None:
                out.append(f"    {s:<10} not measured — falls back to the widest")
            else:
                out.append(f"    {s:<10} ${v:.3f}   (n={n:,})")
        if self.calibrated_from:
            out.append(f"    from {self.calibrated_from}")
        return "\n".join(out)

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SpreadProfile":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls(**json.loads(p.read_text(encoding='utf-8')))
        except Exception as e:
            log.warning("could not load spread profile from %s (%s) — "
                        "treating as UNCALIBRATED rather than guessing", p, e)
            return cls()

    @classmethod
    def declared(cls, venue: str, spread: float) -> "SpreadProfile":
        """A single flat number you assert. Honest, blunt, better than nothing.

        Use this when you know your broker charges roughly $0.45 and you have no
        export yet. It is worse than measurement and much better than silently
        using someone else's spread.
        """
        return cls(venue=venue,
                   by_session={s: float(spread) for s in SESSIONS},
                   samples={s: 0 for s in SESSIONS},
                   statistic="declared (not measured)",
                   calibrated_from="operator declaration")


def calibrate(quotes: Iterable, *, venue: str, statistic: str = "median",
              quantile: float = 0.75, source: str = "") -> SpreadProfile:
    """Measure a spread profile from your own venue's quotes.

    `quotes` yields (timestamp, bid, ask). That is what export_mt5.py writes and
    what the live tick archive records, so this works against either — including
    the archive the desk is building for you from launch, which means the
    profile gets more accurate the longer it runs.
    """
    from .features import session_of

    buckets: dict = {}
    for ts, bid, ask in quotes:
        sp = float(ask) - float(bid)
        if sp <= 0 or sp > 50:
            continue                     # a broken quote is not a cost
        buckets.setdefault(session_of(ts), []).append(sp)

    by, n = {}, {}
    for sess, vals in buckets.items():
        if len(vals) < 100:
            # Too few to characterise a session. Recorded in samples so the gap
            # is visible, but not used — a median of twenty quotes is noise.
            n[sess] = len(vals)
            continue
        vals.sort()
        if statistic == "conservative":
            by[sess] = vals[min(len(vals) - 1, int(quantile * len(vals)))]
        else:
            by[sess] = statistics.median(vals)
        n[sess] = len(vals)
    return SpreadProfile(venue=venue, by_session=by, samples=n,
                         statistic=statistic, calibrated_from=source or None)


def effective_spread(feed_spread: float, profile: Optional[SpreadProfile],
                     session: str) -> tuple[float, str]:
    """The spread to CHARGE, and where it came from. Never silent.

    Returns (spread, provenance). Provenance is stamped onto the signal so a
    decision priced against the feed rather than your venue is a visible fact
    months later, not an archaeological dig.
    """
    if profile is None or not profile.calibrated:
        return feed_spread, "FEED (not your execution venue — UNCALIBRATED)"
    mine = profile.spread_for(session)
    if mine is None:
        return feed_spread, "FEED (profile has no measurement for this session)"
    # Charge the WIDER of the two. The feed occasionally widens dramatically —
    # a release, a liquidity hole — and in those moments your broker is not
    # tighter than the feed no matter what the median says. Charging the max
    # keeps the venue profile as a FLOOR on cost rather than a licence to
    # ignore live conditions.
    if feed_spread > mine:
        return feed_spread, f"FEED (live ${feed_spread:.2f} wider than venue ${mine:.2f})"
    return mine, f"VENUE {profile.venue} {session} (${mine:.2f})"
