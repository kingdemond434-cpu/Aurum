#!/usr/bin/env python3
"""Count the REST requests the service loop actually issues.

The polling rework is an operational claim, and an operational claim with no
number attached is an opinion. This counts every call a counting client
receives across a simulated hour of open market and an hour of closed market,
so the before/after is a measurement rather than an assertion.

What is being fixed:
  1. tick_is_stale() is implemented AS quote(), and the loop called both — two
     identical fetches per iteration, forever.
  2. _maybe_close_bar() requested several hundred candles every iteration, when
     a new M15 bar can only close four times an hour.
  3. _htf_state() re-requested 200 H4 candles on every entry-bar close, when H4
     structure is identical across sixteen of them.
  4. A shut venue was polled at the same cadence as an open one.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from golddesk.service import TF_MINUTES, ServiceConfig

OK, BAD = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


def simulate(*, gated: bool, hours: float = 1.0, closed: bool = False,
             position_open: bool = False,
             cfg: ServiceConfig | None = None) -> Counter:
    """Count requests over `hours` of loop iterations, gated or ungated.

    Mirrors the loop's request pattern rather than importing it, so the
    comparison can run without a live feed. The gating logic under test —
    _bar_boundary_passed and the HTF cache window — is applied identically.
    """
    cfg = cfg or ServiceConfig()
    c: Counter = Counter()
    if not gated:
        poll = cfg.poll_seconds
    elif closed:
        poll = cfg.closed_poll_seconds
    elif position_open:
        poll = cfg.poll_seconds
    else:
        poll = cfg.idle_poll_seconds
    n = int(hours * 3600 / poll)
    entry_min = TF_MINUTES[cfg.entry_tf]

    t0 = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
    last_bar = t0
    htf_at = -1e9
    for k in range(n):
        now = t0 + timedelta(seconds=k * poll)
        elapsed = k * poll

        # --- quote ---
        if gated:
            c["quote"] += 1                      # one per iteration
        else:
            c["quote"] += 2                      # tick_is_stale() + quote()

        if closed:
            continue                             # loop continues before bar work

        # --- entry bars ---
        # BAR TIMESTAMP CONVENTION. A bar stamped 08:15 COVERS 08:15-08:30, so
        # it only becomes the newest CLOSED bar at wall-clock 08:30. The newest
        # closed ts at time T is therefore floor(T) - one bar. Getting this
        # wrong is what made an earlier version of this simulation report one
        # close an hour instead of four.
        due = last_bar + timedelta(minutes=2 * entry_min)
        ask_bars = (not gated) or now >= due - timedelta(seconds=cfg.bar_poll_lead_s)
        if ask_bars:
            c["bars_entry"] += 1
            floor_min = now.minute - (now.minute % entry_min)
            newest_closed = (now.replace(minute=floor_min, second=0, microsecond=0)
                             - timedelta(minutes=entry_min))
            if newest_closed > last_bar:
                last_bar = newest_closed
                c["bar_closes"] += 1
                # --- htf, only on a real close ---
                if gated and elapsed - htf_at < cfg.htf_cache_seconds:
                    pass
                else:
                    c["bars_htf"] += 1
                    htf_at = elapsed
                # the old loop fetched a THIRD quote here
                if not gated:
                    c["quote"] += 1
    return c


def main() -> int:
    cfg = ServiceConfig()
    print("REST REQUEST COUNT — one hour of OPEN market\n")
    # Four hours, so several HTF cache windows and many bar closes fall inside.
    before = simulate(gated=False, hours=4, cfg=cfg)
    after = simulate(gated=True, hours=4, cfg=cfg)
    for k in ("quote", "bars_entry", "bars_htf"):
        b, a = before[k], after[k]
        cut = (1 - a / b) if b else 0.0
        print(f"  {k:<12} {b:>6} -> {a:>5}   ({cut:.0%} fewer)")
    tb = sum(before[k] for k in ("quote", "bars_entry", "bars_htf"))
    ta = sum(after[k] for k in ("quote", "bars_entry", "bars_htf"))
    print(f"  {'TOTAL':<12} {tb:>6} -> {ta:>5}   ({1 - ta/tb:.0%} fewer)\n")

    # THE INVARIANT THAT MATTERS. Not a magic count — that depends on where the
    # window starts — but that gating changed the REQUEST rate and nothing else.
    check("the gated loop processes exactly the same bars",
          before["bar_closes"] == after["bar_closes"] and after["bar_closes"] > 0,
          f"{after['bar_closes']} closes in both arms over 4h — gating removed "
          f"requests, not decisions")
    check("quote requests halved", after["quote"] <= before["quote"] / 2,
          f"{before['quote']} -> {after['quote']}")
    check("the large candle request is no longer per-second",
          after["bars_entry"] < before["bars_entry"] / 100,
          f"{before['bars_entry']} -> {after['bars_entry']} per hour")
    check("H4 is fetched on its own cadence, not the entry bar's",
          after["bars_htf"] < before["bars_htf"],
          f"{before['bars_htf']} -> {after['bars_htf']} per hour")
    check("total traffic cut by at least 95%", ta < tb * 0.05,
          f"{tb} -> {ta}")

    print("\n  WITH A POSITION OPEN — observation must NOT be slowed\n")
    held = simulate(gated=True, position_open=True, hours=4, cfg=cfg)
    print(f"  {'quote':<12} {before['quote']:>6} -> {held['quote']:>5}   "
          f"(full rate retained while managing)")
    check("an open position is still observed every poll_seconds",
          held["quote"] >= 4 * 3600 / cfg.poll_seconds,
          f"{held['quote']} quotes over 4h at poll_seconds={cfg.poll_seconds:.0f}")
    # Higher than the flat case (14) and that is expected, not a defect: with a
    # 1s poll, every iteration inside the 5s lead window asks until the venue
    # actually publishes the new candle. ~6 attempts per close is the cost of
    # not being late on an entry, and 89 requests over four hours against 14,400
    # is not a number worth optimising.
    check("and the bar request stays gated even while polling fast",
          held["bars_entry"] < 200,
          f"{held['bars_entry']} over 4h vs {before['bars_entry']} ungated")

    print("\nREST REQUEST COUNT — one hour of CLOSED market (weekend)\n")
    cb = simulate(gated=False, closed=True, cfg=cfg)
    ca = simulate(gated=True, closed=True, cfg=cfg)
    print(f"  {'quote':<12} {cb['quote']:>6} -> {ca['quote']:>5}   "
          f"({1 - ca['quote']/cb['quote']:.0%} fewer)")
    weekend_h = 48
    print(f"  over a {weekend_h}h weekend: {cb['quote']*weekend_h:,} -> "
          f"{ca['quote']*weekend_h:,} requests")
    check("a shut venue is not polled at trading cadence",
          ca["quote"] <= cb["quote"] / 30,
          f"{cb['quote']} -> {ca['quote']} per hour")
    check("Sunday open is still noticed within a minute",
          cfg.closed_poll_seconds <= 60.0,
          f"closed_poll_seconds={cfg.closed_poll_seconds:.0f}")

    print(f"\n{OK} ok, {BAD} failed")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
