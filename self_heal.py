r"""The 24/7 fixer. Audit, fix what is mechanical, escalate what is not.

    python3 self_heal.py            # audit, fix, report
    python3 self_heal.py --dry-run  # audit and report, change nothing

Runs from a scheduled task every 15 minutes. Almost every run finds nothing and
exits silently; a run that finds something either fixes it or tells the operator
why it cannot, and never both quietly.

WHY THIS IS NOT "AN AI THAT FIXES THE DESK"

Because that is the dangerous version. Every action available here is on an
ALLOWLIST in remediate.py, each one deterministic, bounded to operational state,
reversible, and rate-limited. Nothing here writes code, moves a threshold,
touches a position, or reaches the ruin rail. The faults that need those things
are reported with their diagnosis attached, immediately, so the human loop is
FAST rather than absent -- which is the honest improvement available.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BASE = Path(__file__).parent
STATE = BASE / "state" / "self_heal.json"
log = logging.getLogger("self_heal")


def _restart_desk() -> bool:
    """Bounce the desk task. THE ONLY process control this file has.

    Deliberately `schtasks` and not a kill: the task carries the supervisor, the
    working directory and the argument string, and reproducing those by hand is
    how a restart silently changes what the desk is running.
    """
    try:
        subprocess.run(["schtasks", "/End", "/TN", "AurumSignalDesk"],
                       capture_output=True, timeout=60)
        subprocess.run(["schtasks", "/Run", "/TN", "AurumSignalDesk"],
                       capture_output=True, timeout=60, check=True)
        return True
    except Exception as e:                             # noqa: BLE001
        log.warning("restart failed: %s", e)
        return False


def _refresh_flows() -> bool:
    """Re-collect the public flow feeds and write the cache the desk reads.

    `collect` fetches and `save` writes; there is no combined `refresh` and this
    file must not invent one. An earlier draft here called `flows.refresh(...)`,
    which does not exist -- a remedy that raises on its first real invocation is
    worse than no remedy, because the allowlist claims it works.
    """
    try:
        from golddesk import flows
        cache = BASE / "state" / "flows.json"
        flows.save(flows.collect(cache), cache)
        return True
    except Exception as e:                             # noqa: BLE001
        log.warning("flows refresh failed: %s", e)
        return False


def _load_attempts() -> dict:
    """Attempt history OUTLIVES the process, or the cooldown is a fiction: a
    task that starts fresh every 15 minutes would have no memory of the restart
    it ordered 15 minutes ago and would loop forever."""
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
        return {k: [datetime.fromisoformat(t) for t in v] for k, v in raw.items()}
    except Exception:                                  # noqa: BLE001
        return {}


def _save_attempts(attempts: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {k: [t.isoformat() for t in v] for k, v in attempts.items()},
        indent=2), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="audit and report; take no action")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from golddesk.ledger import Ledger
    from golddesk.opportunity import build_cohorts
    from golddesk.remediate import Remediator, plan, render
    from golddesk.self_audit import audit, render as audit_render

    rows = []
    try:
        rows = Ledger(BASE / "state" / "ledger.jsonl").read_all()
    except Exception as e:                             # noqa: BLE001
        log.warning("no ledger (%s) — auditing what little there is", e)

    # Cohorts are computed here the same way build_service computes them, so the
    # audit sees what a FRESH BOOT would see. Reading the running desk's memory
    # is not possible from another process, and guessing would make the check
    # about this script rather than about the desk.
    cohorts = build_cohorts(rows) or None
    findings = audit(rows, cohorts)
    print(audit_render(findings))

    remedies, escalations = plan(findings, restart_desk=_restart_desk,
                                 refresh_flows=_refresh_flows)
    if args.dry_run:
        for r in remedies:
            print(f"  WOULD FIX  {r.fault}: {r.action} — {r.why}")
        for f in escalations:
            print(f"  WOULD ESCALATE  {f.check}: {f.detail}")
        return 0

    rem = Remediator(attempts=_load_attempts())
    outcomes = rem.run(remedies)
    _save_attempts(rem.attempts)

    report = render(outcomes, escalations)
    if report:
        print(report)
        # The operator hears about it on the channel the desk already uses. A
        # self-healer whose only output is a log file nobody greps has moved the
        # silence rather than removed it.
        try:
            from golddesk.notify import build_sink
            build_sink(None).send("*SELF-HEAL*\n" + report)
        except Exception as e:                         # noqa: BLE001
            log.warning("could not notify: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
