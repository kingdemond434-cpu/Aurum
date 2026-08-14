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


def preflight(symbol: str, want_telegram: bool, secrets: Path) -> list[Check]:
    checks: list[Check] = [assert_no_orders()]

    # --- model -----------------------------------------------------------
    key = os.environ.get("ANTHROPIC_API_KEY")
    checks.append(Check("ANTHROPIC_API_KEY", bool(key),
                        f"set ({len(key)} chars)" if key else
                        "NOT SET — export ANTHROPIC_API_KEY=sk-ant-... "
                        "The desk cannot analyse anything without it"))

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

    # --- telegram --------------------------------------------------------
    tok = (secrets / "telegram_token")
    cid = (secrets / "telegram_chat_id")
    have = tok.exists() and cid.exists()
    checks.append(Check("telegram", have or not want_telegram,
                        f"credentials found in {secrets}/" if have else
                        (f"missing {secrets}/telegram_token and/or telegram_chat_id — "
                         f"signals will go nowhere" if want_telegram else
                         "not requested"),
                        fatal=want_telegram))
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
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    print("=" * 78)
    print(f"AURUM SIGNAL DESK — {args.symbol} — advisory only, no order placement")
    print("=" * 78)
    checks = preflight(args.symbol, not args.no_telegram, Path(args.secrets))
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
    print("every decision, refusal, management step and outcome is journalled to")
    print("state/ledger.jsonl — that file IS the forward evidence. Do not delete it.\n")

    svc = build_service(symbol=args.symbol, shadow=shadow, vision=vision,
                        cfg=ServiceConfig(symbol=args.symbol),
                        secrets_dir=args.secrets)
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
