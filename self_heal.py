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


def _sample_spread() -> bool:
    """Run the venue spread sampler. It refuses if the attached terminal is not
    the execution venue, and that refusal is CORRECT -- recorded as not-taken."""
    script = BASE / "sample_vantage_spread.py"
    if not script.exists():
        return False
    try:
        r = subprocess.run([sys.executable, str(script), "--seconds", "90",
                            "--statistic", "conservative"],
                           capture_output=True, timeout=300, cwd=str(BASE))
        return r.returncode == 0
    except Exception as e:                             # noqa: BLE001
        log.warning("spread sampler failed: %s", e)
        return False


def _refresh_macro() -> bool:
    """Re-pull the free driver set. A blocked or rate-limited public feed often
    clears on retry; if it does not, the attempt cap escalates it."""
    try:
        import os
        from golddesk.drivers_free import build_drivers
        d = build_drivers(os.environ.get("FRED_API_KEY"))
        return any(p.observed for p in d.values())
    except Exception as e:                             # noqa: BLE001
        log.warning("macro refresh failed: %s", e)
        return False


#: Log files the disk remedy may delete. ROTATED LOGS ONLY.
#:
#: The ledger, every checkpoint and every tick archive are absent from this list
#: DELIBERATELY and must stay absent. A disk remedy that can reach evidence is
#: one that eventually destroys the only record of what the desk predicted --
#: worse than the full disk it was fixing.
ROTATABLE = ("*.log.1", "*.log.2", "*.log.old", "_desk_stdout.tmp.*",
             "_desk_stderr.tmp.*")


def _rotate_logs() -> bool:
    logs = BASE / "logs"
    if not logs.exists():
        return False
    freed = 0
    for pat in ROTATABLE:
        for f in logs.glob(pat):
            try:
                freed += f.stat().st_size
                f.unlink()
            except Exception:                          # noqa: BLE001
                continue
    if freed:
        log.info("freed %.1fMB of rotated logs", freed / (1024 * 1024))
    return freed > 0


def _sync_quant() -> bool:
    """Run the quant->Aurum findings transport out of band.

    Idempotent on (statement, measured_on): running it early or twice appends
    nothing. It CANNOT fix quant having produced no export -- that is the other
    repository's schedule, and the attempt cap turns a persistent failure here
    into an escalation naming exactly that.
    """
    script = BASE / "deploy" / "windows" / "Sync-QuantFindings.ps1"
    if not script.exists():
        return False
    quant = BASE.parent / "quant"
    if not quant.exists():
        log.warning("no quant checkout at %s — transport cannot run", quant)
        return False
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy",
                            "Bypass", "-File", str(script),
                            "-QuantRoot", str(quant), "-AurumRoot", str(BASE)],
                           capture_output=True, timeout=300)
        return r.returncode == 0
    except Exception as e:                             # noqa: BLE001
        log.warning("quant sync failed: %s", e)
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
    from golddesk import capture as cap
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
    findings = audit(rows, cohorts, base=BASE)
    print(audit_render(findings))

    # THE SECOND AXIS. self_audit asks "is the desk WIRED"; this asks "is it
    # still TAKING WHAT IS THERE". A desk can pass every wiring check and be
    # worth nothing because it refuses everything, banks 15% of the moves it
    # calls right, or stopped receiving the quant desk's survivors -- and none
    # of those raises an error or looks like anything but a quiet week.
    cap_findings = cap.audit(rows, base=BASE)
    print(cap.render(cap_findings))
    findings = list(findings) + list(cap_findings)

    remedies, escalations = plan(findings, restart_desk=_restart_desk,
                                 refresh_flows=_refresh_flows,
                                 sample_spread=_sample_spread,
                                 refresh_macro=_refresh_macro,
                                 rotate_logs=_rotate_logs,
                                 sync_quant=_sync_quant)
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
