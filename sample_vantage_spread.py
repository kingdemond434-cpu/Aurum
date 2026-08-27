"""Measure the EXECUTION venue's spread, which is not the venue the desk reads prices from.

    python sample_vantage_spread.py --seconds 120        # sample, archive, rebuild the profile
    python sample_vantage_spread.py --report             # what the archive says, write nothing

WHY THIS EXISTS AT ALL

Every launch prints, and it is the desk telling the truth about itself:

    COST basis: THE FEED -- not your execution venue. Every expectancy figure is
    priced against a spread you will not pay.

`calibrate_spread.py` was built to close that, and it closes it for a desk whose data feed and
execution venue are the SAME BROKER: it measures spread from `state/ledger.jsonl`, and the
ledger records the quotes the desk read. Here they are different brokers -- the analyst reads
Fusion through MT5, and the operator executes on Vantage -- so calibrating from the ledger
would produce a precise, well-measured, per-session number for a spread that is never paid.
Precision about the wrong venue is worse than an honest gap, because it retires the warning.

Advertised numbers are not a substitute. Vantage publishes roughly $0.28 on the Standard STP
account and $0.12-0.25 on RAW ECN plus commission, but those are peak-liquidity figures for a
marketing page: gold's ASIA book and its OVERLAP book are different markets, and rollover is a
different market again. `venue.calibrate` already bins by session for exactly that reason and
already refuses a session with fewer than 100 samples rather than reporting a median of twenty.
It needed quotes from the right terminal, and nothing was giving it any.

WHY IT ARCHIVES INSTEAD OF SAMPLING ONCE

A two-minute sample is a two-minute opinion. Each run APPENDS its ticks to a JSONL archive and
rebuilds the profile from everything accumulated, so a task running through the day converges
on the real per-session distribution instead of re-guessing from whatever the last two minutes
looked like. The archive is the evidence; the profile is a view of it.

WHAT IT DOES NOT DO

It never places, modifies or reads an order, and it never touches the Fusion terminal the desk
is trading through. It attaches to the Vantage terminal read-only, takes ticks, detaches. The
account provenance IS recorded with the profile, so a profile measured against the wrong login
is a visible fact rather than an assumption.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent

#: Where the DESK loads its profile from -- golddesk/service.py's own default. Written here
#: rather than to a path of this script's choosing, because a profile the desk does not read is
#: the same as no profile. (calibrate_spread.py defaults to state/spread_profile.json while
#: service.py loads config/spread_profile.json, so its --out help text -- "where the desk loads
#: the profile from" -- has never been true. Fixed there; matched here.)
PROFILE = BASE / "config" / "spread_profile.json"

#: The accumulated evidence. One JSON object per tick: {"ts": iso, "bid": f, "ask": f}.
ARCHIVE = BASE / "state" / "vantage_ticks.jsonl"

#: Sensible install locations for the Vantage terminal, tried in order when --terminal is not
#: given. A wrong guess is reported, never silently substituted with the Fusion terminal --
#: attaching to the wrong broker is precisely the error this whole script exists to prevent.
TERMINAL_GUESSES = (
    r"C:\Program Files\Vantage International MT5\terminal64.exe",
    r"C:\Program Files\Vantage Global Prime MT5\terminal64.exe",
    r"C:\Program Files\Vantage MT5\terminal64.exe",
)

#: A broker whose name does not contain this is not the venue we mean to measure.
EXPECT_BROKER = "vantage"


def _resolve_terminal(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return next((p for p in TERMINAL_GUESSES if Path(p).exists()), None)


def sample(seconds: int, symbol: str, terminal: str | None,
           expect_broker: str = EXPECT_BROKER) -> tuple[list[dict], str]:
    """Take ticks from the EXECUTION terminal. Returns (rows, venue_label).

    Refuses rather than guesses: an unreachable terminal, or one logged into a broker that is
    not the expected venue, raises. Sampling the wrong book and labelling it Vantage would put
    a confident wrong number where an honest gap used to be.
    """
    import time

    import MetaTrader5 as mt5

    path = _resolve_terminal(terminal)
    ok = mt5.initialize(path=path) if path else mt5.initialize()
    if not ok:
        raise SystemExit(
            f"could not attach to the Vantage terminal (path={path!r}): {mt5.last_error()}.\n"
            f"Pass --terminal with the real terminal64.exe path. NOT falling back to whatever "
            f"terminal happens to be open -- that is how the Fusion book gets measured and "
            f"labelled Vantage.")

    info = mt5.account_info()
    venue = f"{info.company} / {info.server}" if info else "UNKNOWN"
    if expect_broker and expect_broker.lower() not in venue.lower():
        mt5.shutdown()
        raise SystemExit(
            f"attached terminal reports {venue!r}, which does not look like "
            f"{expect_broker!r}. Refusing: a profile measured on the wrong venue is worse "
            f"than none, because it retires the UNCALIBRATED warning while still being wrong. "
            f"Pass --expect-broker to override deliberately.")

    if not mt5.symbol_select(symbol, True):
        mt5.shutdown()
        raise SystemExit(f"{symbol} is not available on {venue}")

    rows, seen, deadline = [], set(), time.monotonic() + seconds
    while time.monotonic() < deadline:
        t = mt5.symbol_info_tick(symbol)
        if t and t.time_msc not in seen and t.bid and t.ask:
            seen.add(t.time_msc)
            rows.append({"ts": datetime.fromtimestamp(t.time_msc / 1000, tz=timezone.utc)
                         .isoformat(), "bid": float(t.bid), "ask": float(t.ask)})
        time.sleep(0.2)
    mt5.shutdown()
    return rows, venue


def archive(rows: list[dict], path: Path = ARCHIVE) -> int:
    """Append. Never truncate -- the archive is the evidence, and it only gets better."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return len(rows)


def quotes(path: Path = ARCHIVE):
    """(ts, bid, ask) triples, the shape venue.calibrate consumes."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            yield (datetime.fromisoformat(r["ts"]), float(r["bid"]), float(r["ask"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue                     # one corrupt line must not void the archive


def rebuild(venue: str, statistic: str, out: Path = PROFILE,
            src: Path = ARCHIVE) -> "object":
    from golddesk.venue import calibrate

    prof = calibrate(quotes(src), venue=venue, statistic=statistic,
                     source=f"{src.name} (execution venue, sampled live)")
    if prof.calibrated:
        prof.save(out)
    return prof


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--terminal", default=None,
                    help="path to Vantage's terminal64.exe; guessed if omitted")
    ap.add_argument("--expect-broker", default=EXPECT_BROKER,
                    help="refuse if the attached account's broker does not contain this")
    ap.add_argument("--statistic", default="median", choices=("median", "conservative"),
                    help="conservative charges p75 -- prefer it while the archive is thin, "
                         "because optimism about cost shows up as trades that looked positive "
                         "and were not")
    ap.add_argument("--report", action="store_true",
                    help="rebuild and print from the existing archive; take no new ticks")
    a = ap.parse_args(argv)

    if not a.report:
        rows, venue = sample(a.seconds, a.symbol, a.terminal, a.expect_broker)
        n = archive(rows)
        print(f"sampled {len(rows)} tick(s) from {venue}; {n} appended to {ARCHIVE}")
    else:
        venue = "archive"

    prof = rebuild(venue, a.statistic)
    print(prof.render())
    if not prof.calibrated:
        print("\n  NOT WRITTEN. No session yet has the 100 samples venue.calibrate requires.\n"
              "  That is UNMEASURED, not a spread of zero -- keep sampling; the archive is\n"
              "  cumulative and every run brings the thin sessions closer.")
        return 1
    print(f"\n  written to {PROFILE} -- the path golddesk/service.py loads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
