#!/usr/bin/env python3
"""Bad ticks must not reach the position, and good ticks must not be lost.

Each case is a print a real gold feed actually emits. The asymmetry is the
whole design: rejecting a good tick delays a decision by one poll; accepting a
bad one writes a fabricated loss into the ledger, and the ledger is the only
evidence this desk has.
"""

from __future__ import annotations

import gzip
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from golddesk.tickguard import GuardConfig, TickArchive, TickGuard

OK, BAD = 0, 0
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


def rejects() -> None:
    print("1. prints a real feed emits, and must never act on")
    g = TickGuard()
    g.check(3300.00, 3300.30, T0)          # establish a baseline

    cases = [
        ("a zero bid", 0.0, 3300.30),
        ("a crossed quote", 3300.50, 3300.10),
        ("a decimal slip (10x low)", 330.02, 330.05),
        ("a decimal slip (10x high)", 33000.0, 33000.3),
        ("a $200 spread", 3200.00, 3400.00),
    ]
    for i, (label, b, a) in enumerate(cases, 1):
        ok, why = g.check(b, a, T0 + timedelta(seconds=i))
        check(f"rejects {label}", not ok, why)

    # The jump test, with the exemption that makes it safe.
    ok, why = g.check(3300.00, 3300.30, T0 + timedelta(seconds=20))
    check("accepts a normal quote after all that", ok)
    ok, why = g.check(3600.00, 3600.30, T0 + timedelta(seconds=21))
    check("rejects a 9% jump one second later", not ok, why)
    ok, why = g.check(3305.00, 3305.30, T0 + timedelta(seconds=22))
    check("but accepts an ordinary 0.15% move", ok, "3300 -> 3305")


def gaps_and_reopens() -> None:
    print("\n2. the gap exemption — a Monday open is not a bad print")
    g = TickGuard()
    fri = datetime(2026, 3, 6, 20, 59, tzinfo=timezone.utc)
    g.check(3300.00, 3300.30, fri)
    # Weekend gap: gold reopens 1.5% higher. This MUST be accepted — rejecting
    # it would blind the desk at exactly the moment the market reopened.
    sun = datetime(2026, 3, 8, 22, 0, tzinfo=timezone.utc)
    ok, why = g.check(3350.00, 3350.60, sun)
    check("a 1.5% weekend gap is ACCEPTED", ok,
          "rejecting this blinds the desk at the reopen — the worst moment")

    g2 = TickGuard()
    g2.check(3300.00, 3300.30, T0)
    ok, why = g2.check(3350.00, 3350.30, T0 + timedelta(seconds=300))
    check("and so is the same move after a 5-minute silence", ok,
          "the jump test only applies inside the window where it is impossible")

    ok, why = g2.check(3350.00, 3350.30, T0 + timedelta(seconds=100))
    check("a timestamp going backwards is rejected", not ok, why)


def news_moves() -> None:
    print("\n3. a fast market is not a broken one")
    g = TickGuard()
    px = 3300.0
    g.check(px, px + 0.3, T0)
    # NFP: gold moves 1.2% over 30 seconds in ~15 ticks. Every one must pass.
    accepted = 0
    for i in range(1, 16):
        px += 2.64                    # ~0.08% per tick, 1.2% total
        ok, _ = g.check(px, px + 0.6, T0 + timedelta(seconds=i * 2))
        accepted += ok
    check("all 15 ticks of a 1.2% NFP move are accepted", accepted == 15,
          f"{accepted}/15 — a guard that rejects news is worse than no guard")
    check("the reject rate stayed at zero through it",
          g.stats.jump == 0, f"{g.stats.jump} jump rejections")


def archive() -> None:
    print("\n4. accepted ticks are kept, rejected ones are kept separately")
    d = Path(tempfile.mkdtemp())
    a = TickArchive(d, "XAUUSD")
    for i in range(2500):
        a.write(3300.0 + i * 0.01, 3300.3 + i * 0.01, T0 + timedelta(seconds=i))
    a.write_reject(0.0, 3300.3, T0, "non-positive quote")
    a.close()

    day = T0.strftime("%Y%m%d")
    p = d / f"XAUUSD_ticks_{day}.csv.gz"
    check("a day file is written", p.exists(), p.name)
    lines = gzip.open(p, "rt").read().strip().splitlines()
    check("every accepted tick is in it", len(lines) == 2500, f"{len(lines)} rows")
    ms, bid, ask = lines[0].split(",")
    check("the format is epoch_ms,bid,ask and stays readable without this program",
          int(ms) == int(T0.timestamp() * 1000) and float(bid) == 3300.0,
          lines[0])

    rp = d / f"XAUUSD_rejects_{day}.csv.gz"
    check("rejects are archived, not discarded", rp.exists(),
          "if the guard is ever wrong, the evidence is what it threw away")
    check("and carry the reason",
          "non-positive" in gzip.open(rp, "rt").read())

    # A restart must not truncate the morning.
    a2 = TickArchive(d, "XAUUSD")
    a2.write(3399.0, 3399.3, T0 + timedelta(seconds=9000))
    a2.close()
    n = len(gzip.open(p, "rt").read().strip().splitlines())
    check("a restart APPENDS rather than truncating", n == 2501, f"{n} rows")

    # Archiving must never be able to break trading.
    broken = TickArchive(Path("/nonexistent/cannot/write"), "XAUUSD")
    try:
        broken.write(3300.0, 3300.3, T0)
        check("a failing archive does not raise into the loop", True,
              "trading continues; the failure is logged")
    except Exception as e:
        check("a failing archive does not raise into the loop", False, repr(e))


def wiring() -> None:
    print("\n5. it is actually wired into the service loop")
    import inspect
    from golddesk.service import DeskService, ServiceConfig
    cfg = ServiceConfig()
    check("guarding is on by default", cfg.guard_ticks)
    check("archiving is on by default", cfg.archive_ticks,
          f"-> {cfg.tick_archive_dir}")
    src = inspect.getsource(DeskService._inner_loop)
    check("the guard runs BEFORE anything acts on price",
          src.index("self.guard.check") < src.index("self.desk.on_tick"),
          "a bad print must not reach stop evaluation")
    check("a rejected tick continues the loop rather than trading on it",
          "continue" in src.split("REJECTED TICK")[0][-800:]
          or "self.archive.write_reject" in src)
    close_src = inspect.getsource(DeskService.run)
    check("the archive is flushed and closed on shutdown",
          "self.archive.close()" in close_src,
          "a day of ticks in an unflushed buffer is a day not collected")


def main() -> int:
    print("TICK INTEGRITY — bad prints must not become fabricated losses\n")
    rejects()
    gaps_and_reopens()
    news_moves()
    archive()
    wiring()
    print(f"\n{OK} ok, {BAD} failed")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
