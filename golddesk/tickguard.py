"""Bad ticks, and the ticks worth keeping.

TWO THINGS THE DATA PATH WAS MISSING, BOTH PRE-LAUNCH

1. NOTHING REJECTED A BAD PRINT.

   The desk evaluates stops and targets on every tick. A venue that emits one
   bad quote — a stale cross, a decimal slip, a momentary 0.0 bid, a feed
   glitch on the rollover — will trip a stop that never happened. The position
   closes, a loss is written, and the ledger now contains a fabricated outcome
   that looks exactly like a real one.

   That is worse than a trading error. This desk's entire thesis is that its
   forward record is trustworthy enough to promote and demote its own rules
   from. One fabricated stop-out poisons a cohort, and nothing downstream can
   tell it apart from a genuine loss.

   Retail gold feeds emit bad prints. Not often, but "not often" over 24/5 for
   a year is a certainty, and the cost is asymmetric: rejecting a good tick
   delays a decision by one poll, accepting a bad one corrupts the evidence
   permanently.

2. THE TICKS WERE THROWN AWAY.

   The desk saw every tick and persisted none of them. Its own venue's tick
   history is the single dataset it cannot buy, download or reconstruct later —
   Dukascopy has THEIR feed, not yours, and the difference is exactly the
   spread and slippage behaviour that decides whether a strategy pays. It
   starts accumulating the day an archive exists and not one day sooner.

WHAT IS DELIBERATELY NOT DONE HERE

No smoothing, no interpolation, no "corrected" prices. A rejected tick is
DROPPED and COUNTED, never replaced with a guess. A desk that repairs its own
market data has stopped observing the market.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TICKGUARD_VERSION = "tickguard-2026-08-14-a"


@dataclass
class GuardConfig:
    """Every bound is a fact about gold or about the venue, not a preference."""
    # A crossed or zero quote is never real.
    reject_crossed: bool = True
    # Gold trades roughly 1,000-5,000. Anything outside this is a decimal slip
    # or a placeholder, not a price.
    min_price: float = 200.0
    max_price: float = 20000.0
    # Spread bounds in PRICE units. Retail gold is $0.12-$0.60, widening to a
    # few dollars at rollover or on a release. $50 is a broken quote.
    max_spread: float = 50.0
    # A jump larger than this from the last accepted mid, within a short window,
    # is treated as suspect. Gold CAN move a percent in a minute on a release —
    # so the test is deliberately loose and is about impossible prints, not
    # fast markets.
    max_jump_pct: float = 3.0
    # ...but only when the last tick was recent. After a gap (weekend, restart)
    # a large move is expected and must NOT be rejected.
    jump_window_s: float = 120.0
    # Two identical timestamps with different prices means the feed is
    # reordering or duplicating; keep the first and count it.
    reject_duplicate_ts: bool = True


@dataclass
class GuardStats:
    seen: int = 0
    accepted: int = 0
    crossed: int = 0
    out_of_range: int = 0
    wide_spread: int = 0
    jump: int = 0
    duplicate: int = 0
    stale_backwards: int = 0

    @property
    def rejected(self) -> int:
        return self.seen - self.accepted

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.seen if self.seen else 0.0

    def render(self) -> str:
        return (f"  ticks seen {self.seen:,}  accepted {self.accepted:,}  "
                f"rejected {self.rejected:,} ({self.reject_rate:.3%})\n"
                f"    crossed {self.crossed}  out-of-range {self.out_of_range}  "
                f"wide-spread {self.wide_spread}  jump {self.jump}  "
                f"duplicate {self.duplicate}  backwards {self.stale_backwards}")


class TickGuard:
    """Accept or reject a quote. Never repairs one.

    `check(bid, ask, ts)` returns (ok, reason). A rejected tick is dropped by the
    caller and counted here, so the rejection RATE is itself observable — a feed
    that starts rejecting 2% of ticks has developed a problem, and that is worth
    seeing before it shows up as strange trades.
    """

    def __init__(self, cfg: Optional[GuardConfig] = None):
        self.cfg = cfg or GuardConfig()
        self.stats = GuardStats()
        self._last_mid: Optional[float] = None
        self._last_ts: Optional[datetime] = None

    def check(self, bid: float, ask: float,
              ts: Optional[datetime] = None) -> tuple[bool, str]:
        c = self.cfg
        self.stats.seen += 1

        if bid <= 0 or ask <= 0:
            self.stats.out_of_range += 1
            return False, f"non-positive quote bid={bid} ask={ask}"
        if c.reject_crossed and ask < bid:
            self.stats.crossed += 1
            return False, f"crossed quote bid={bid:.2f} > ask={ask:.2f}"
        if not (c.min_price <= bid <= c.max_price
                and c.min_price <= ask <= c.max_price):
            self.stats.out_of_range += 1
            return False, (f"price outside {c.min_price:.0f}-{c.max_price:.0f}: "
                           f"bid={bid:.2f} ask={ask:.2f} — a decimal slip, not a market")
        spread = ask - bid
        if spread > c.max_spread:
            self.stats.wide_spread += 1
            return False, f"spread ${spread:.2f} exceeds ${c.max_spread:.2f}"

        mid = (bid + ask) / 2.0
        if ts is not None and self._last_ts is not None:
            if ts < self._last_ts:
                self.stats.stale_backwards += 1
                return False, (f"timestamp went backwards "
                               f"({ts.isoformat()} < {self._last_ts.isoformat()})")
            if c.reject_duplicate_ts and ts == self._last_ts and mid != self._last_mid:
                self.stats.duplicate += 1
                return False, "duplicate timestamp with a different price"

        # JUMP TEST, and the gap exemption that makes it safe.
        #
        # A large move after a long silence is a WEEKEND GAP or a restart, not a
        # bad print, and rejecting those would blind the desk exactly when the
        # market reopened. So the test only applies when the previous tick was
        # recent enough that a move of this size would be impossible.
        if self._last_mid is not None and self._last_ts is not None and ts is not None:
            gap = (ts - self._last_ts).total_seconds()
            if 0 <= gap <= c.jump_window_s:
                pct = abs(mid - self._last_mid) / self._last_mid * 100.0
                if pct > c.max_jump_pct:
                    self.stats.jump += 1
                    return False, (f"{pct:.2f}% jump in {gap:.1f}s "
                                   f"({self._last_mid:.2f} -> {mid:.2f}) — "
                                   f"beyond {c.max_jump_pct:.1f}% is a print, not a move")

        self._last_mid, self._last_ts = mid, (ts or self._last_ts)
        self.stats.accepted += 1
        return True, "ok"


class TickArchive:
    """Append-only gzipped CSV of every ACCEPTED tick, one file per UTC day.

    Format is boring on purpose: `epoch_ms,bid,ask`. It has to be readable in
    five years by something that is not this program, and gzip+CSV will be.
    Parquet would be smaller and would also make the archive depend on a library
    version to be readable at all.

    REJECTED TICKS ARE ARCHIVED SEPARATELY, not discarded. If the guard ever
    turns out to be wrong about something, the evidence for that is the pile of
    things it threw away, and deleting it would make the guard unfalsifiable.
    """

    def __init__(self, root: Path, symbol: str = "XAUUSD",
                 keep_rejects: bool = True):
        self.root = Path(root)
        self.symbol = symbol
        self.keep_rejects = keep_rejects
        self.root.mkdir(parents=True, exist_ok=True)
        self._day: Optional[str] = None
        self._fh = None
        self._rej = None
        self.written = 0
        self.rejects_written = 0

    def _path(self, day: str, kind: str = "ticks") -> Path:
        return self.root / f"{self.symbol}_{kind}_{day}.csv.gz"

    def _roll(self, ts: datetime) -> None:
        day = ts.strftime("%Y%m%d")
        if day == self._day:
            return
        self.close()
        self._day = day
        # Append mode: a restart mid-day must not truncate the morning.
        self._fh = gzip.open(self._path(day), "at", encoding="ascii")
        if self.keep_rejects:
            self._rej = gzip.open(self._path(day, "rejects"), "at", encoding="ascii")

    def write(self, bid: float, ask: float, ts: datetime) -> None:
        try:
            self._roll(ts)
            self._fh.write(f"{int(ts.timestamp() * 1000)},{bid:.3f},{ask:.3f}\n")
            self.written += 1
            # Flush periodically rather than per tick: per-tick fsync on a 1s
            # loop is wasteful, and losing the last few seconds of ticks to a
            # hard kill costs nothing anyone will miss.
            if self.written % 500 == 0:
                self._fh.flush()
        except Exception as e:                 # archiving must never break trading
            log.warning("tick archive write failed: %s", e)

    def write_reject(self, bid: float, ask: float, ts: datetime, reason: str) -> None:
        if not self.keep_rejects:
            return
        try:
            self._roll(ts)
            self._rej.write(f"{int(ts.timestamp() * 1000)},{bid},{ask},{reason}\n")
            self.rejects_written += 1
            if self.rejects_written % 50 == 0:
                self._rej.flush()
        except Exception as e:
            log.warning("reject archive write failed: %s", e)

    def close(self) -> None:
        for fh in (self._fh, self._rej):
            try:
                if fh:
                    fh.flush()
                    fh.close()
            except Exception:
                pass
        self._fh = self._rej = None

    def render(self) -> str:
        return (f"  tick archive: {self.written:,} written, "
                f"{self.rejects_written:,} rejects kept, -> {self.root}")
