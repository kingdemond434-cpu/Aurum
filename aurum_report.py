#!/usr/bin/env python3
"""Every analysis the desk can run over its own ledger. One command.

    python aurum_report.py                       # state/ledger.jsonl
    python aurum_report.py --r-value 100         # net-of-inference figures
    python aurum_report.py path --mechanism X    # one section

The research modules are useless if running them requires remembering six
module names and their argument shapes. This is the front door.

Nothing here trades, decides or writes to the ledger. It reads and reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> list:
    if not path.exists():
        print(f"no ledger at {path}\n"
              f"The desk writes it on the first decision. Nothing to report yet.")
        return []
    return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="?", default="all",
                    choices=("all", "capture", "budget", "path", "competition",
                             "management", "constitution", "regime", "calendar"))
    ap.add_argument("--ledger", default="state/ledger.jsonl")
    ap.add_argument("--r-value", type=float, default=None,
                    help="what one R is worth in USD. Required for any "
                         "net-of-inference figure, and deliberately not "
                         "defaulted: an assumed value silently decides whether "
                         "every component looks profitable")
    ap.add_argument("--mechanism", default=None)
    ap.add_argument("--arm-key", default="vision",
                    help="vision | model | provider | management_policy")
    args = ap.parse_args()

    rows = load(Path(args.ledger))
    if not rows:
        return 0
    want = args.only

    from golddesk.opportunity import resolved_outcomes
    resolved = resolved_outcomes(rows)
    print(f"ledger: {args.ledger}   {len(rows)} rows, {len(resolved)} resolved trades")
    if not resolved:
        print("\nNo resolved trades yet. Everything below will say so rather than\n"
              "inventing a number from an empty sample.")

    if want in ("all", "capture"):
        section("CAPTURE — what was available and what was taken")
        try:
            from missed_money import report as missed_report
            print(missed_report(rows))
        except Exception as e:
            print(f"  unavailable: {e}")

    if want in ("all", "budget"):
        section("INFORMATION BUDGET (#9) — what the thinking cost")
        from golddesk.budget import compare_arms, report as budget_report
        print(budget_report(rows, r_value_usd=args.r_value).render())
        print()
        print(compare_arms(rows, r_value_usd=args.r_value))

    if want in ("all", "path"):
        section("PATH PREDICTION (#14) — the shape of a trade")
        from golddesk.path import report as path_report
        cond = {"mechanism_name": args.mechanism} if args.mechanism else None
        print(path_report(resolved, cond))

    if want in ("all", "regime"):
        section("REGIME NOVELTY (#11) — is the estimate interpolating?")
        from golddesk.regime import assess_novelty
        if resolved:
            latest = resolved[-1].get("context") or {}
            print(assess_novelty(latest, resolved).render())
            print("\n  Measured against the MOST RECENT resolved trade's context.")
            print("  At decision time the desk does this against the live brief.")
        else:
            print("  no resolved trades to compare against")

    if want in ("all", "competition"):
        section("MODEL COMPETITION (#7) — paired, on identical states")
        from golddesk.competition import report as comp_report
        print(comp_report(rows, arm_key=args.arm_key))

    if want in ("all", "management"):
        section("MANAGEMENT COUNTERFACTUAL (#15) — replayed on recorded paths")
        from mgmt_counterfactual import report as mgmt_report
        print(mgmt_report(rows))

    if want in ("all", "constitution"):
        section("CONSTITUTION — which restrictions have earned their keep")
        from golddesk.constitution import review
        try:
            print(review(rows, Path("golddesk")).render())
        except Exception as e:
            print(f"  unavailable: {e}")

    if want in ("all", "calendar"):
        section("SCHEDULED RELEASES (#6) — information, never a gate")
        from datetime import datetime, timezone
        from golddesk.calendar import Calendar
        print(Calendar().render(datetime.now(timezone.utc), days=21))

    print("\n" + "=" * 78)
    if args.r_value is None:
        print("Run with --r-value <usd> for net-of-inference figures.")
    print("Nothing above promotes anything. A result here is a HYPOTHESIS;")
    print("promotion requires sealing it and confirming on data it has not seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
