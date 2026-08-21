#!/usr/bin/env python3
"""The six deployment defects, each with a test that FAILS on the old behaviour.

These are not architecture. They are the difference between a repo that looks
deployable and one that starts on a clean VPS, and every one of them was found
by trying to deploy it rather than by reading it.

  1  systemd units used User=%i in an ORDINARY unit -> refuses to start
  2  no dependency manifest -> parquet path unavailable, deploy improvised
  3  OANDA bar spread 100x too small -> every cost study flattered
  4  1s REST polling incl. a full candle history, unchanged when market shut
  5  Telegram signal secrets absent from env.example; empty files passed preflight
  6  management authority was an implicit default nobody chose
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OK, BAD = 0, 0
ROOT = Path(__file__).parent


def check(label: str, cond: bool, detail: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ok   {label}" + (f"  — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  FAIL {label}" + (f"  — {detail}" if detail else ""))


def systemd() -> None:
    print("1. systemd units can actually start")
    for name in ("aurum-desk.service", "aurum-capture.service"):
        p = ROOT / "deploy" / name
        txt = p.read_text(encoding='utf-8')
        directives = [l.strip() for l in txt.splitlines()
                      if l.strip() and not l.strip().startswith("#")]
        user = [d for d in directives if d.startswith("User=")]
        check(f"{name}: User= is a real account, not %i",
              bool(user) and "%i" not in user[0], user[0] if user else "MISSING")
        # StartLimit* are [Unit] directives; systemd ignores them under [Service].
        # Parse SECTIONS properly rather than partitioning on the literal text:
        # a comment mentioning "[Service]" is not a section header, and treating
        # it as one is how this check first reported a false failure.
        section, sections = None, {}
        for line in txt.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("[") and s.endswith("]"):
                section = s
                sections.setdefault(section, [])
            elif section:
                sections[section].append(s)
        for d in ("StartLimitIntervalSec", "StartLimitBurst"):
            where = [sec for sec, body in sections.items()
                     if any(x.startswith(d + "=") for x in body)]
            if not where:
                continue
            check(f"{name}: {d} is under [Unit] where it is honoured",
                  where == ["[Unit]"],
                  f"found in {where} — under [Service] systemd silently ignores "
                  f"it and the restart backoff this file claims does not exist")
        check(f"{name}: runs from the venv, not system python",
              ".venv/bin/python" in txt,
              "system python will not have the pinned dependencies")


def manifest() -> None:
    print("\n2. dependencies are pinned and installable in one command")
    req = ROOT / "requirements.txt"
    check("requirements.txt exists", req.exists())
    if not req.exists():
        return
    txt = req.read_text(encoding='utf-8')
    pins = re.findall(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s#]*)", txt, re.M)
    names = {n.lower() for n, _ in pins}
    check("every requirement is PINNED with ==",
          all("==" in l for l in txt.splitlines()
              if l.strip() and not l.strip().startswith("#") and not l.startswith("-r")),
          f"{len(pins)} pinned")
    for pkg in ("anthropic", "pydantic"):
        check(f"{pkg} is declared in the LIVE requirements", pkg in names)
    check("pandas/pyarrow are NOT in the live requirements",
          "pandas" not in names and "pyarrow" not in names,
          "they are a parquet reader the live desk never touches, and pyarrow "
          "is the usual VPS build failure — a desk that does not use it must "
          "not be blocked by it")
    res = ROOT / "requirements-research.txt"
    check("research deps exist and are separate", res.exists())
    if res.exists():
        rtxt = res.read_text(encoding='utf-8')
        check("and carry pandas + pyarrow EXPLICITLY",
              "pandas" in rtxt and "pyarrow" in rtxt,
              "without pyarrow, to_parquet raises at runtime — which is how the "
              "parquet integration checks silently could not complete")
    cap = ROOT / "requirements-capture.txt"
    check("the collector's deps are SEPARATE so the desk installs without them",
          cap.exists() and "telethon" in cap.read_text(encoding='utf-8'))
    inst = ROOT / "deploy" / "install.sh"
    check("install.sh exists and is executable",
          inst.exists() and (inst.stat().st_mode & 0o111) != 0)


def oanda_spread() -> None:
    print("\n3. OANDA bar spread is in the right unit")
    from golddesk.feed import (MAX_PLAUSIBLE_SPREAD, MIN_PLAUSIBLE_SPREAD, Bar,
                               FeedError, _assert_plausible_spread)
    from golddesk.feed_oanda import DIGITS, POINT

    # The exact case from the report: a real $0.30 gold spread.
    spread_price = 0.30
    spread_pts = int(round(spread_price / POINT))
    round_tripped = spread_pts * (10 ** -DIGITS)
    check("a $0.30 candle spread survives the points round trip",
          abs(round_tripped - 0.30) < 1e-9,
          f"${spread_price:.2f} -> {spread_pts} points -> ${round_tripped:.4f}")
    check("the OLD behaviour would have produced $0.003",
          abs(spread_price * (10 ** -DIGITS) - 0.003) < 1e-9,
          "dollars fed through the points conversion — 100x too small")

    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Cfg:
        digits = 2

    bad = [Bar(now + timedelta(minutes=15 * k), 2000, 2001, 1999, 2000, 10, 0.003)
           for k in range(30)]
    raised = False
    try:
        _assert_plausible_spread(bad, "M15", Cfg())
    except FeedError:
        raised = True
    check("the guard REFUSES a 100x-too-small median spread", raised,
          "raising beats warning: a desk with costs understated 100x takes "
          "trades that cannot pay")

    good = [Bar(now + timedelta(minutes=15 * k), 2000, 2001, 1999, 2000, 10, 0.30)
            for k in range(30)]
    ok = True
    try:
        _assert_plausible_spread(good, "M15", Cfg())
    except FeedError:
        ok = False
    check("and accepts a correct $0.30 spread", ok,
          f"plausible band ${MIN_PLAUSIBLE_SPREAD:.2f}-${MAX_PLAUSIBLE_SPREAD:.2f}")

    thin = [Bar(now, 2000, 2001, 1999, 2000, 10, 0.003) for _ in range(5)]
    quiet = True
    try:
        _assert_plausible_spread(thin, "M15", Cfg())
    except FeedError:
        quiet = False
    check("and stays silent when there is too little to judge", quiet)


def zero_setup_feed() -> None:
    """The no-account backend. Logic is testable offline; the network is not."""
    import json
    from golddesk.feed_yahoo import POINT, YahooClient
    print("\n3b. zero-setup feed (no account, no key)")

    c0 = YahooClient(half_spread=0.0)
    check("refuses to start without a declared spread", not c0.initialize(),
          "it publishes no bid/ask; inventing one invents the number that "
          "decides whether marginal trades pay")

    canned = {"chart": {"result": [{"timestamp": [1772452800, 1772452860],
              "indicators": {"quote": [{"open": [3300.0, 3301.0],
              "high": [3301.0, 3302.0], "low": [3299.0, 3300.5],
              "close": [3300.5, 3301.5], "volume": [10, 12]}]}}]}}

    # THE ROUNDING BUG THIS GUARDS. round() on both sides of the mid pulls each
    # in by half a tick, so a declared $0.45 arrived as $0.44 — a cost cheaper
    # than the one the operator stated they pay, in the direction that makes
    # marginal trades look positive.
    worst = None
    for declared in (0.45, 0.30, 0.25, 0.33, 0.60, 1.00, 0.15):
        cl = YahooClient(half_spread=declared / 2.0)
        cl._get = lambda i, r: canned
        t = cl.symbol_info_tick("XAUUSD")
        got = round(t.ask - t.bid, 2)
        if worst is None or (got - declared) < worst[1]:
            worst = (declared, got - declared, got)
    check("a synthesised spread is NEVER narrower than declared",
          worst[1] >= -1e-9,
          f"worst case ${worst[0]:.2f} declared -> ${worst[2]:.2f} synthesised")

    cl = YahooClient(half_spread=0.225)
    cl._get = lambda i, r: canned
    t = cl.symbol_info_tick("XAUUSD")
    check("the quote is STAMPED synthetic", getattr(t, "synthetic", False) is True,
          "the tick path cannot see a real spread widen on this feed, and "
          "nothing downstream may mistake this for an observed quote")

    rows = cl.copy_rates_from_pos("XAUUSD", 15, 0, 5)
    check("bar spread is in POINTS, matching the Protocol",
          rows[0].spread == int(round(0.45 / POINT)),
          f"{rows[0].spread} pts — same unit contract the OANDA client got wrong")

    holed = json.loads(json.dumps(canned))
    holed["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = None
    cl._get = lambda i, r: holed
    check("a null bar is dropped, not interpolated",
          len(cl.copy_rates_from_pos("XAUUSD", 15, 0, 5)) == 1,
          "interpolating invents a swing that never traded")

    si = cl.symbol_info("XAUUSD")
    check("venue stop limits are 0, never guessed", si.trade_stops_level == 0,
          "that is a fact about YOUR broker, which this feed knows nothing of")


def polling() -> None:
    print("\n4. REST polling is proportionate")
    from golddesk.service import ServiceConfig
    cfg = ServiceConfig()
    check("a closed venue polls far slower than an open one",
          cfg.closed_poll_seconds >= 30 * cfg.poll_seconds,
          f"{cfg.poll_seconds:.0f}s open -> {cfg.closed_poll_seconds:.0f}s closed")
    check("being flat polls slower than managing a position",
          cfg.idle_poll_seconds > cfg.poll_seconds,
          f"{cfg.idle_poll_seconds:.0f}s flat vs {cfg.poll_seconds:.0f}s with a "
          f"position open — the tick path exists to observe an OPEN position")
    check("the HTF cache is a meaningful fraction of the HTF period",
          cfg.htf_cache_seconds >= 1800,
          f"{cfg.htf_cache_seconds:.0f}s; at 300s it expired between every M15 "
          f"close and saved nothing")
    src = (ROOT / "golddesk" / "service.py").read_text(encoding='utf-8')
    check("the loop no longer calls tick_is_stale() and quote() both",
          "self.feed.tick_is_stale()" not in src,
          "tick_is_stale IS quote — calling both doubled every request")
    check("the bar request is clock-gated",
          "_bar_boundary_passed" in src)


def telegram() -> None:
    print("\n5. Telegram deployment is reproducible")
    env = (ROOT / "deploy" / "env.example").read_text(encoding='utf-8')
    check("env.example documents the SIGNAL bot credentials",
          "TELEGRAM_BOT_TOKEN" in env and "TELEGRAM_CHAT_ID" in env,
          "it previously carried only the collector's API ID/hash, so a clean "
          "install passed the env stage then failed preflight with nothing to "
          "point at")
    check("and says files are preferred over environment variables",
          "files are preferred" in env or "0600" in env or "systemctl show" in env)

    import os
    import tempfile
    from golddesk.notify import build_sink, resolve_telegram
    from golddesk.notify import NullSink, TelegramSink

    d = Path(tempfile.mkdtemp())
    (d / "telegram_token").write_text("")
    (d / "telegram_chat_id").write_text("")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(k, None)
    tok, cid, where = resolve_telegram(d)
    check("EMPTY secret files are NOT treated as configured", tok is None,
          f"resolve says {where!r}; install.sh creates these empty and the old "
          f"check tested only .exists()")
    check("and the sink degrades to null rather than pretending",
          isinstance(build_sink(d), NullSink))

    (d / "telegram_token").write_text("123:AA\n")
    (d / "telegram_chat_id").write_text("-100123\n")
    tok, cid, where = resolve_telegram(d)
    check("filled files resolve", tok == "123:AA" and cid == "-100123", where)
    check("and produce a real Telegram sink",
          isinstance(build_sink(d), TelegramSink))

    d2 = Path(tempfile.mkdtemp())
    os.environ["TELEGRAM_BOT_TOKEN"] = "456:BB"
    os.environ["TELEGRAM_CHAT_ID"] = "-100456"
    try:
        tok, cid, where = resolve_telegram(d2)
        check("the environment is honoured as a fallback",
              tok == "456:BB" and "environment" in where, where)
    finally:
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)

    run = (ROOT / "run_desk.py").read_text(encoding='utf-8')
    check("preflight uses the SAME resolver as the sink",
          "resolve_telegram" in run,
          "two implementations of 'do we have credentials' is how a desk "
          "passes preflight and then sends signals nowhere")


def management() -> None:
    print("\n6. management authority is chosen, not inherited")
    run = (ROOT / "run_desk.py").read_text(encoding='utf-8')
    check("--management is a flag", "--management" in run)
    for mode in ("heuristic", "contextual", "passive"):
        check(f"  mode {mode} is offered", f'"{mode}"' in run)
    check("the boot banner prints who manages the position",
          "MANAGEMENT" in run and "Claude does NOT manage" in run,
          "the asymmetry — Claude enters, a heuristic manages — has to be "
          "visible at boot, not discovered by reading the source")
    check("the shipped unit states the mode explicitly",
          "--management heuristic" in (ROOT / "deploy" / "aurum-desk.service").read_text(encoding='utf-8'))

    live = (ROOT / "golddesk" / "live.py").read_text(encoding='utf-8')
    check("an operator override does NOT go through policy_state.bind()",
          "_management_override" in live,
          "bind() records an evidence warrant with a TTL; a flag written "
          "through it would masquerade as a proven policy and then lapse mid-run")
    check("every row records whether authority was operator or evidence",
          '"management_authority"' in live)

    # contextual must refuse rather than silently fall back
    import tempfile
    from golddesk.ledger import Ledger
    from golddesk.live import LiveDesk, Vision
    from golddesk.notify import build_sink
    from golddesk.providers import AnalystProvider, ProviderRead

    class NoChoose(AnalystProvider):
        name, model = "stub", "none"

        def read(self, brief, charts=()):
            raise NotImplementedError

    out = Path(tempfile.mkdtemp())
    desk = LiveDesk(NoChoose(), Ledger(out / "l.jsonl"), build_sink(None),
                    shadow=True, vision=Vision.NUMERIC_ONLY)
    check("heuristic binds", desk.set_management("heuristic") == "heuristic-v1")
    refused = False
    try:
        desk.set_management("contextual")
    except ValueError:
        refused = True
    check("contextual REFUSES when the provider cannot choose", refused,
          "a run labelled contextual that is quietly heuristic contaminates "
          "the arm it is filed under")


def main() -> int:
    print("DEPLOYMENT DEFECTS — six findings, six regressions\n")
    systemd()
    manifest()
    oanda_spread()
    zero_setup_feed()
    polling()
    telegram()
    management()
    print(f"\n{OK} ok, {BAD} failed")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
