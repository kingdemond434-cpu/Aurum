"""Measure YOUR venue's spread from the ledger the desk is already writing.

    python3 calibrate_spread.py                     # report only, writes nothing
    python3 calibrate_spread.py --write             # write the profile the desk loads
    python3 calibrate_spread.py --statistic conservative   # p75 instead of median

WHY THIS EXISTS RATHER THAN --declared-spread

Running without a spread profile prints, on every launch:

    NO SPREAD PROFILE - costs will be taken from the FEED, which is not your
    execution venue. Every expectancy figure is priced against a spread you
    will not pay.

The advertised remedy is `--declared-spread 0.45`: one flat number, asserted by
the operator, applied to every session. `venue.SpreadProfile.declared()` is
honest about what that is -- `statistic="declared (not measured)"` -- and it is
genuinely better than silently using the feed's. But it is still a number
somebody had to guess, it cannot vary by session, and gold's spread is not
remotely constant across the day: the ASIA book and the OVERLAP book are
different markets.

`venue.calibrate()` already exists to do this properly, from real quotes, per
session. Its docstring says it works against "the archive the desk is building
for you from launch". IT HAD NO CALLERS, AND THERE IS NO SUCH ARCHIVE -- nothing
in the package writes ticks to disk. So the good path was written, tested, and
wired to nothing (III.16), while the operator was pointed at the guess.

THE QUOTES WERE THERE THE WHOLE TIME. `MarketBrief.render()` emits

    BID 4515.14  ASK 4515.21  SPREAD 0.07  TICK_AGE 3s

and `runner._record` journals `brief_render` on EVERY decision -- signals and
refusals alike. So `state/ledger.jsonl` is already a timestamped record of your
venue's bid/ask, growing on every wake, and a refusal is as good a spread
observation as a signal. No new archive, no new writer, no extra I/O on the
live path: the measurement is a read of a file that is already being written.

WHAT IT WILL NOT DO

It will not invent a session it cannot measure. `calibrate()` needs 100 quotes
in a session before it will characterise one, and this script refuses to write a
profile in which NO session cleared that bar rather than emitting a plausible
half-empty file. An uncalibrated profile is a real answer; a profile calibrated
from twenty observations is a fabricated one wearing the same name (L1.28a).

A caveat that belongs on the number and not in a footnote: a DEMO feed's spread
is not a live account's. FusionMarkets-Demo quoting $0.07 on XAUUSD tells you
what the demo server publishes, and the live book is routinely wider. The
profile records `calibrated_from`, so the venue and the account type travel with
the figure -- read it before trusting an expectancy priced against it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parent))

from golddesk.venue import SESSIONS, calibrate  # noqa: E402

#: Matches the BID/ASK/SPREAD line in a rendered brief. Anchored on all three
#: labels so a line that merely contains a number cannot satisfy it.
QUOTE_LINE = re.compile(
    r"BID\s+([0-9]+(?:\.[0-9]+)?)\s+ASK\s+([0-9]+(?:\.[0-9]+)?)\s+SPREAD\s+([0-9]+(?:\.[0-9]+)?)")

#: `calibrate()` discards any session with fewer than this many quotes. Repeated
#: here only so the report can explain a thin session instead of showing a gap.
MIN_QUOTES_PER_SESSION = 100


def quotes_from_ledger(rows: list[dict]) -> Iterator[tuple[datetime, float, float]]:
    """Yield (timestamp, bid, ask) for every row whose brief carries a quote.

    Reads `brief_render`, which is the rendered prompt text rather than a
    structured field. That is a real fragility and it is guarded rather than
    ignored: `main()` treats "rows present but nothing parsed" as a FAILURE,
    because the only way that happens is the render format changing, and the
    alternative is reporting "no data" for a ledger that is full of it.
    """
    for row in rows:
        text = row.get("brief_render") or ""
        m = QUOTE_LINE.search(text)
        if not m:
            continue
        ts_raw = row.get("t0")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            continue
        bid, ask = float(m.group(1)), float(m.group(2))
        if ask <= bid:
            continue                     # a crossed or stale quote is not a cost
        yield ts, bid, ask


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default="state/ledger.jsonl")
    ap.add_argument("--out", default="state/spread_profile.json",
                    help="where the desk loads the profile from")
    ap.add_argument("--venue", default="",
                    help="venue label stored with the profile; defaults to the "
                         "broker recorded in the ledger, else 'unknown'")
    ap.add_argument("--statistic", default="median",
                    choices=("median", "conservative"),
                    help="'conservative' takes the p75 spread, which prices a "
                         "trade at a worse-than-typical fill rather than a "
                         "typical one")
    ap.add_argument("--write", action="store_true",
                    help="write the profile. Without it this only reports, so "
                         "the numbers can be read before they price anything")
    args = ap.parse_args(argv)

    led = Path(args.ledger)
    if not led.exists():
        print(f"REFUSED: no ledger at {led}. The desk writes it from launch — "
              f"run the desk first, then calibrate from what it saw.")
        return 2

    rows = [json.loads(line) for line in
            led.read_text(encoding="utf-8").splitlines() if line.strip()]
    quotes = list(quotes_from_ledger(rows))

    if rows and not quotes:
        # THE FORMAT-DRIFT GUARD. A full ledger that yields nothing means the
        # brief's render changed, not that the desk saw no quotes. Reporting
        # "no data" here would be absence read as a clean answer (WS-005).
        print(f"REFUSED: {len(rows)} ledger rows and NOT ONE parsed a quote.\n"
              f"  This is not 'no data' — it is the BID/ASK/SPREAD line in\n"
              f"  MarketBrief.render() having changed shape. Fix QUOTE_LINE in\n"
              f"  this file against the current render before trusting any\n"
              f"  spread number that comes out of it.")
        return 3

    if not quotes:
        print(f"UNMEASURED: {led} has no rows yet. Nothing to calibrate.")
        return 2

    venue = args.venue
    if not venue:
        for row in reversed(rows):
            v = (row.get("context") or {}).get("broker") or ""
            if v:
                venue = str(v)
                break
    venue = venue or "unknown"

    profile = calibrate(quotes, venue=venue, statistic=args.statistic,
                        source=f"{led} ({len(quotes)} quotes)")

    span = f"{min(q[0] for q in quotes).isoformat()} -> {max(q[0] for q in quotes).isoformat()}"
    print(f"venue      : {venue}")
    print(f"quotes     : {len(quotes)} from {len(rows)} ledger rows")
    print(f"span       : {span}")
    print(f"statistic  : {args.statistic}")
    print()
    print(f"{'session':<10} {'spread':>9} {'quotes':>8}   status")
    for sess in SESSIONS:
        n = profile.samples.get(sess, 0)
        if sess in profile.by_session:
            print(f"{sess:<10} {profile.by_session[sess]:9.3f} {n:8d}   measured")
        else:
            print(f"{sess:<10} {'—':>9} {n:8d}   UNMEASURED "
                  f"(needs {MIN_QUOTES_PER_SESSION})")

    if not profile.calibrated:
        print(f"\nREFUSED TO WRITE: no session reached {MIN_QUOTES_PER_SESSION} "
              f"quotes.\n"
              f"  A profile built from fewer is not a measurement with a small\n"
              f"  error bar, it is a guess that has stopped announcing itself.\n"
              f"  Let the desk run longer, or pass --declared-spread meanwhile —\n"
              f"  that at least records itself as 'declared (not measured)'.")
        return 4

    if not args.write:
        print("\n(report only — pass --write to store this profile)")
        return 0

    out = Path(args.out)
    profile.save(out)
    print(f"\n-> {out}")
    print("   Restart the desk, or point --spread-profile at this file, for the "
          "cost model to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
