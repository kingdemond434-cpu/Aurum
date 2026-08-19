"""Start the live signal desk. One command, signals only, never an order.

    python3 run_desk.py --preflight      # check everything, start nothing
    python3 run_desk.py --shadow         # run, mark every message [SHADOW]
    python3 run_desk.py --live           # run, unmarked advisory signals

WHAT "LIVE" MEANS HERE, AND WHAT IT DOES NOT

It means the desk analyses the real market with the real model and sends you
real Telegram messages. It does NOT mean it trades. There is no order placement
anywhere in this package, and `--assert-no-orders` proves that by scanning the
executable path for the MT5 trading calls rather than asking you to trust it.
You place every order by hand. That is the charter and it is checked, not
assumed.

WHY A PREFLIGHT

A 24/5 process that starts half-configured is worse than one that refuses to
start. It looks alive, produces nothing, and you find out days later. Every
check below either passes or stops the launch, and each says exactly what to do.
"""

from __future__ import annotations

import argparse
import ast
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

# MT5 trading entry points. Their ABSENCE is the safety property.
ORDER_CALLS = {"order_send", "order_check", "order_calc_margin", "positions_modify"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True

    def render(self) -> str:
        mark = "PASS" if self.ok else ("FAIL" if self.fatal else "WARN")
        return f"  [{mark}] {self.name:<22} {self.detail}"


def assert_no_orders(pkg: Path = Path("golddesk")) -> Check:
    """Prove the package cannot place an order, by reading it."""
    hits: list[str] = []
    for f in sorted(pkg.glob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in ORDER_CALLS:
                hits.append(f"{f.name}:{n.lineno} .{n.attr}")
            elif isinstance(n, ast.Name) and n.id in ORDER_CALLS:
                hits.append(f"{f.name}:{n.lineno} {n.id}")
    return Check("no order placement", not hits,
                 "no MT5 trading call anywhere in golddesk/ — advisory only"
                 if not hits else f"ORDER CALLS FOUND: {hits}")


def _telegram_check(want: bool, secrets: Path, deliver: bool = True) -> Check:
    """Shared by every feed backend. It was previously only reachable on the MT5
    path, so an OANDA launch could pass preflight with signals going nowhere.

    AND IT ONLY CHECKED THAT CREDENTIALS EXISTED. A non-empty token file proves
    somebody typed something into a file. A revoked bot, a wrong chat id, a bot
    the user has never pressed Start on, and an egress firewall all pass that
    check and deliver nothing — which for a desk whose only product is the
    message is total failure that preflight calls PASS.

    So preflight now SENDS. `deliver=False` keeps the old credentials-only
    behaviour for tests and for anyone who does not want a message every launch.
    """
    from golddesk.notify import probe, resolve_telegram
    tok, cid, where = resolve_telegram(secrets)
    have = bool(tok and cid)
    if not want:
        return Check("telegram", True, "not requested", fatal=False)
    if not have:
        return Check("telegram", False,
                     f"missing or EMPTY {secrets}/telegram_token and/or "
                     f"telegram_chat_id (and no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                     f"in the environment) — signals will go nowhere. install.sh "
                     f"creates these files empty; fill them in.", fatal=True)
    if not deliver:
        return Check("telegram", True,
                     f"credentials found in {where} — NOT verified by delivery",
                     fatal=False)
    from golddesk.notify import build_sink
    ok, why = probe(build_sink(secrets), "preflight")
    return Check("telegram", ok, f"{why} (credentials from {where})", fatal=True)


def check_analyst_backend(provider_spec: str) -> Check:
    """The credential the CHOSEN provider actually needs.

    Checking ANTHROPIC_API_KEY unconditionally was right while there was one
    backend and is wrong now: under `claudecode:` the key is not merely
    unnecessary, it is actively removed from the child environment, so a
    preflight demanding it would fail a correctly-configured desk — and, worse,
    a preflight that PASSED on a stray key would tell an operator running on
    their subscription that everything was fine while the CLI quietly billed
    them per token. Each backend is asked for the thing it uses.
    """
    name = provider_spec.partition(":")[0]
    if name == "deterministic":
        return Check("analyst backend", True,
                     "deterministic rules — no model, no credential, no cost")
    if name == "claudecode":
        import shutil
        exe = shutil.which("claude")
        if not exe:
            return Check("analyst backend", False,
                         "provider 'claudecode' needs the Claude Code CLI on "
                         "PATH and it is not installed here")
        billed = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
        note = ("but ANTHROPIC_API_KEY is set, so reads may be BILLED per "
                "token rather than drawn from your subscription — unset it, or "
                "pass billed=False to strip it from the CLI's environment"
                if billed else
                "subscription mode — no metered bill. Verify the CLI is logged "
                "in: `claude -p hello`")
        return Check("analyst backend", True, f"claudecode at {exe}; {note}")
    key = os.environ.get("ANTHROPIC_API_KEY")
    return Check("ANTHROPIC_API_KEY", bool(key),
                 f"set ({len(key)} chars) — reads are BILLED per token"
                 if key else
                 "NOT SET — export ANTHROPIC_API_KEY=sk-ant-... "
                 "The desk cannot analyse anything without it. Or run "
                 "--provider claudecode:claude-opus-5 to use a subscription")


def preflight(symbol: str, want_telegram: bool, secrets: Path,
              feed: str = "mt5", min_stop: float = 0.0,
              declared_spread: float = 0.0,
              provider_spec: str = "anthropic:claude-opus-5") -> list[Check]:
    checks: list[Check] = [assert_no_orders()]

    # --- model -----------------------------------------------------------
    checks.append(check_analyst_backend(provider_spec))

    # --- feed ------------------------------------------------------------
    if feed == "yahoo":
        checks.append(Check("declared spread", bool(declared_spread),
                            f"${declared_spread:.2f} — the bid/ask is BUILT from "
                            f"this, because this feed publishes none"
                            if declared_spread else
                            "REQUIRED with --feed yahoo. That feed has no bid/ask, "
                            "so the quote is synthesised from your venue's spread. "
                            "Inventing one would invent the number that decides "
                            "whether marginal trades pay"))
        if declared_spread:
            from golddesk.feed_yahoo import YahooClient
            c = YahooClient(symbol=symbol, half_spread=declared_spread / 2.0)
            ok = c.initialize()
            checks.append(Check("yahoo feed", ok,
                                "reachable" if ok else f"{c.last_error()}"))
            if ok:
                t = c.symbol_info_tick(symbol)
                checks.append(Check("quote (SYNTHETIC)", t is not None,
                                    f"bid={t.bid} ask={t.ask} — built from your "
                                    f"declared spread, NOT observed. The tick path "
                                    f"cannot see a real spread widen on this feed"
                                    if t else f"no price: {c.last_error()}",
                                    fatal=False))
        checks.append(Check("broker stop limit", bool(min_stop),
                            f"{min_stop} price units, from your own terminal"
                            if min_stop else
                            "NOT SET — pass --min-stop from YOUR MT5", fatal=False))
        checks.append(_telegram_check(want_telegram, secrets))
        return checks

    if feed == "oanda":
        from golddesk.feed_oanda import OandaClient
        c = OandaClient()
        ok = c.initialize()
        checks.append(Check("OANDA feed", ok,
                            "authenticated" if ok else
                            f"{c.last_error()} — export OANDA_TOKEN and OANDA_ACCOUNT"))
        if ok:
            t = c.symbol_info_tick(c.instrument)
            checks.append(Check("live quote", t is not None,
                                f"bid={t.bid} ask={t.ask} spread=${t.ask-t.bid:.2f}"
                                if t else f"no price: {c.last_error()}"))
        checks.append(Check("broker stop limit", bool(min_stop),
                            f"{min_stop} price units, from your own terminal"
                            if min_stop else
                            "NOT SET — pass --min-stop <stops_level * point> read "
                            "from YOUR MT5. Without it stop legality is weaker "
                            "than your venue's", fatal=False))
        checks.append(_telegram_check(want_telegram, secrets))
        return checks

    # --- terminal --------------------------------------------------------
    try:
        import MetaTrader5 as mt5
        started = mt5.initialize()
        checks.append(Check("MT5 terminal", bool(started),
                            "connected" if started else
                            f"initialize() failed: {mt5.last_error()} — open the "
                            f"terminal and log in first"))
        if started:
            info = mt5.symbol_info(symbol)
            ok = info is not None and mt5.symbol_select(symbol, True)
            detail = (f"{symbol}: digits={info.digits} point={info.point} "
                      f"spread={info.spread}pts stops_level={info.trade_stops_level}"
                      if ok else
                      f"{symbol} not selectable — check the exact name in Market Watch")
            checks.append(Check("symbol", ok, detail))
            tick = mt5.symbol_info_tick(symbol) if ok else None
            checks.append(Check("live quote", tick is not None,
                                f"bid={tick.bid} ask={tick.ask} spread=${tick.ask-tick.bid:.2f}"
                                if tick else "no tick — is the market open?"))
            acct = mt5.account_info()
            checks.append(Check("account", acct is not None,
                                f"{getattr(acct,'company','?')} / {getattr(acct,'server','?')} "
                                f"(read-only use; nothing is ever sent)"
                                if acct else "no account info", fatal=False))
    except ImportError:
        checks.append(Check("MT5 terminal", False,
                            "MetaTrader5 not installed — pip install MetaTrader5 "
                            "(Windows, same machine as the terminal)"))

    checks.append(_telegram_check(want_telegram, secrets))
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--preflight", action="store_true", help="check only, start nothing")
    ap.add_argument("--shadow", action="store_true", help="run; tag every message [SHADOW]")
    ap.add_argument("--live", action="store_true", help="run; untagged advisory signals")
    ap.add_argument("--secrets", default="secrets")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--numeric-only", action="store_true",
                    help="skip charts (cheaper; changes which arm you are running)")
    ap.add_argument("--max-hours", type=float, default=None)
    ap.add_argument("--feed", default="mt5", choices=("mt5", "oanda", "yahoo"),
                    help="where PERCEPTION comes from. oanda runs on Linux with "
                         "no terminal; yahoo needs NO ACCOUNT AT ALL but has no "
                         "real bid/ask, so it synthesises one from "
                         "--declared-spread. Cost and stop legality always come "
                         "from your own broker via --min-stop")
    ap.add_argument("--min-stop", type=float, default=0.0,
                    help="your broker's trade_stops_level in PRICE units "
                         "(stops_level * point). Required with --feed oanda")
    # WHO MANAGES THE TRADE. This was an implicit default and it is the single
    # most consequential unstated choice in the desk: Claude forms the entry
    # judgement, but a HeuristicChooser has been running the lifecycle. That is
    # a defensible production stance and an indefensible accident, so it is now
    # a flag that must be chosen, is printed at boot, and is stamped on every
    # signal and outcome row.
    ap.add_argument("--management", default="heuristic",
                    choices=("heuristic", "contextual", "passive"),
                    help="which policy has AUTHORITY over the open position. "
                         "heuristic: deterministic rules (the incumbent). "
                         "contextual: Claude decides each management step — do "
                         "not grant this until it has beaten the heuristic on "
                         "paired states. passive: never intervenes; the floor.")
    ap.add_argument("--shadow-management", action="store_true", default=True,
                    help="record what the OTHER policies would have chosen at "
                         "every step, on the identical legality-filtered option "
                         "set (default on; this is how authority gets earned)")
    ap.add_argument("--no-shadow-management", dest="shadow_management",
                    action="store_false")
    ap.add_argument("--shadow-contextual", action="store_true",
                    help="include the CONTEXTUAL arm in shadowing. Off by "
                         "default because a shadow arm that calls a paid API on "
                         "every management step costs real money to observe")
    ap.add_argument("--declared-spread", type=float, default=None,
                    help="YOUR broker's typical XAUUSD spread in dollars, e.g. "
                         "0.45. Without it the desk prices every trade against "
                         "the FEED's spread — a cost you will not pay, and "
                         "usually a smaller one, so marginal trades look better "
                         "than they are")
    ap.add_argument("--provider", default="anthropic:claude-opus-5",
                    help="analyst backend. 'anthropic:<model>' bills per token. "
                         "'claudecode:<model>' runs the same model through the "
                         "Claude Code CLI, which authenticates against a Pro/Max "
                         "subscription — no metered bill, but it consumes "
                         "subscription quota and a read takes ~78s rather than "
                         "~8s, so pair it with a low wake rate. 'deterministic' "
                         "is the rule-based baseline and costs nothing at all.")
    ap.add_argument("--universe", action="store_true",
                    help="ask the analyst for every available proposition rather "
                         "than one. Changes what is asked, so it is a different "
                         "ARM — not a free improvement")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    print("=" * 78)
    print(f"AURUM SIGNAL DESK — {args.symbol} via {args.feed} — advisory only, "
          f"no order placement")
    print("=" * 78)
    checks = preflight(args.symbol, not args.no_telegram, Path(args.secrets),
                       feed=args.feed, min_stop=args.min_stop,
                       declared_spread=args.declared_spread or 0.0,
                       provider_spec=args.provider)
    for c in checks:
        print(c.render())
    fatal = [c for c in checks if c.fatal and not c.ok]
    print()
    if fatal:
        print(f"PREFLIGHT FAILED on {len(fatal)} check(s). Fix those and re-run.")
        print("Nothing was started. A half-configured 24/5 process looks alive and")
        print("produces nothing, which is worse than refusing to boot.")
        return 2
    print("PREFLIGHT PASSED")

    if args.preflight:
        print("\n--preflight given; not starting. Add --shadow or --live to run.")
        return 0
    if not (args.shadow or args.live):
        print("\nNeither --shadow nor --live given; not starting.")
        return 0

    from golddesk.live import Vision
    from golddesk.service import ServiceConfig, build_service

    shadow = not args.live
    vision = Vision.NUMERIC_ONLY if args.numeric_only else Vision.NUMERIC_PLUS_CHARTS
    print(f"\nstarting: shadow={shadow}  vision={vision.value}")
    print(f"  ENTRY judgement  : Claude ({'charts + numeric' if not args.numeric_only else 'numeric only'})")
    print(f"  ARITHMETIC/RISK  : deterministic compiler — always")
    print(f"  MANAGEMENT       : {args.management}"
          + ("  <- Claude has authority over the open position"
             if args.management == "contextual" else
             "  <- Claude does NOT manage the open position"))
    print(f"  shadow policies  : {'on' if args.shadow_management else 'OFF'}"
          + ("  (contextual included — this calls the API per step)"
             if args.shadow_contextual else "  (contextual excluded — costs money)"))
    print(f"  opportunity set  : {'universe (all propositions)' if args.universe else 'single read'}")
    if args.declared_spread:
        print(f"  COST basis       : your venue, ${args.declared_spread:.2f} declared")
    else:
        print(f"  COST basis       : THE FEED — not your execution venue.")
        print(f"                     Every expectancy figure is priced against a")
        print(f"                     spread you will not pay. Pass --declared-spread.")
    if args.management == "contextual":
        print("\n  NOTE: contextual management has not yet beaten the heuristic on")
        print("  paired states. Granting it authority before it has is the exact")
        print("  thing the evidence standard exists to prevent.")
    print("\nevery decision, refusal, management step and outcome is journalled to")
    print("state/ledger.jsonl — that file IS the forward evidence. Do not delete it.\n")

    from golddesk.management import BrokerLimits
    svc = build_service(symbol=args.symbol, shadow=shadow, vision=vision,
                        cfg=ServiceConfig(symbol=args.symbol),
                        secrets_dir=args.secrets, feed_backend=args.feed,
                        provider_spec=args.provider,
                        management=args.management,
                        declared_spread=args.declared_spread,
                        shadow_management=args.shadow_management,
                        shadow_contextual=args.shadow_contextual,
                        universe_mode=args.universe,
                        broker_limits=(BrokerLimits(min_stop_distance=args.min_stop)
                                       if args.min_stop else None))
    try:
        st = svc.run(max_seconds=(args.max_hours * 3600) if args.max_hours else None)
    except KeyboardInterrupt:
        st = svc.state
        print("\ninterrupted — state checkpointed")
    print(f"\nticks {st.ticks_seen}  bars {st.bars_processed}  "
          f"reconnects {st.reconnects}  stale suspensions {st.stale_suspensions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
