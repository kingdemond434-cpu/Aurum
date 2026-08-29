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
import csv
import io
import json
import logging
import subprocess
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BASE = Path(__file__).parent
STATE = BASE / "state" / "self_heal.json"
log = logging.getLogger("self_heal")

# ONE list of where quant might be, shared with the nightly cycle. See
# _QUANT_CANDIDATES below for why two lists was a defect rather than a detail.
from golddesk.absorb_auto import QUANT_ROOT_CANDIDATES as _AA_CANDIDATES  # noqa: E402


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


def _read_task(name: str):
    """Read one scheduled task's real state via schtasks CSV.

    schtasks and not PowerShell's Get-ScheduledTaskInfo: schtasks exists on
    every Windows since XP, needs no module import, and is already the only
    process-control verb this file uses. On a non-Windows box it raises, and
    task_health.audit turns that into UNMEASURED rather than a pass.
    """
    from golddesk.task_health import TaskInfo
    r = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/V"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return TaskInfo(name, False, False, None, None)
    rows = list(csv.DictReader(io.StringIO(r.stdout)))
    if not rows:
        return TaskInfo(name, False, False, None, None)
    d = rows[0]
    status = (d.get("Scheduled Task State") or d.get("Status") or "").strip()
    last_run, last_res = None, None
    raw = (d.get("Last Run Time") or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            last_run = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    try:
        last_res = int(str(d.get("Last Result") or "").strip())
    except ValueError:
        last_res = None
    return TaskInfo(name, True, status.lower() != "disabled", last_run, last_res)


def _run_update() -> bool:
    """Invoke the deployer directly, because its scheduled task is failing.

    THE LOOP THIS BREAKS. AurumSignalDesk-Update exits 1 every thirty minutes,
    so nothing deploys -- including the fixes that would tell anyone why it is
    failing. The only way out has been a human running `git pull` by hand, three
    times in one day. Meanwhile self_heal's own task, same box, same account,
    same interpreter, runs cleanly every fifteen minutes: whatever is wrong lives
    in that task's environment, not in the script.

    SAME SCRIPT, SAME GUARDS. Update-AurumDesk.ps1 refuses a dirty tree, advances
    only by fast-forward, runs the suite against the new code while the old desk
    is still live, rolls back on red, refuses to restart on an open position, and
    never re-registers a task. None of that is bypassed here -- only the trigger
    changes. UTF-8 is forced for the same reason the script does it: cp1252 and
    a codebase full of em-dashes is how the suite goes red for no reason.
    """
    script = BASE / "deploy" / "windows" / "Update-AurumDesk.ps1"
    if not script.exists():
        return False
    try:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-DeskRoot", str(BASE)],
            capture_output=True, timeout=1800, cwd=str(BASE), env=env)
        return r.returncode == 0
    except Exception as e:                             # noqa: BLE001
        log.warning("update run failed: %s", e)
        return False


def _enable_task(name: str) -> bool:
    """Re-enable a registered task. The ONE task-control action taken
    automatically: flipping a flag on an existing registration is deterministic
    and reversible. REGISTERING one is not, and stays the operator's act."""
    try:
        r = subprocess.run(["schtasks", "/Change", "/TN", name, "/ENABLE"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception as e:                             # noqa: BLE001
        log.warning("could not enable %s: %s", name, e)
        return False


#: Where the quant checkout might live. ORDERED, and searched rather than
#: assumed, because this was hardcoded to BASE.parent/"quant" -- C:\quant on the
#: box -- and the real checkout is at C:\opt\quant. The remedy pointed at a
#: directory that does not exist and reported "transport cannot run" forever,
#: which is a fixer that cannot fix and says so in a log nobody was reading.
#:
#: An explicit env var wins, because a search is a guess and an operator with a
#: fifth location should not have to edit this list.
#:
#: DERIVED FROM THE SHARED LIST, plus this box's own sibling directory. There
#: used to be two lists — this one and the nightly cycle's — with different
#: entries and different markers, so the watchdog and the thing it watches could
#: disagree about whether the checkout exists at all.
_QUANT_CANDIDATES = tuple(
    [Path(c) for c in _AA_CANDIDATES] + [BASE.parent / "quant"])


def _quant_root():
    """Delegates to absorb_auto so there is ONE answer to "where is quant".

    There used to be two implementations — this one and the nightly cycle's —
    with different candidate lists and different markers, which meant the
    watchdog and the thing it watches could disagree about whether the checkout
    exists at all. A fixer that believes the pipe is fine while the cycle
    believes it is absent is worse than either belief on its own.

    `_QUANT_CANDIDATES` above is kept only for the log line that names where the
    search looked; the search itself is no longer here.
    """
    from golddesk.absorb_auto import discover_quant_root
    root, _basis = discover_quant_root(candidates=_QUANT_CANDIDATES)
    return root


def _absorb_now() -> bool:
    """Pull quant's findings into the inbox right now, in-process.

    Deliberately NOT the PowerShell transport: this runs identically on the
    Windows box and on any clone, needs no script on disk, and dedupes by
    content hash downstream, so an out-of-band run appends only what is new and
    is a no-op otherwise.

    Returns False when no checkout is reachable — the one case a fixer genuinely
    cannot fix. The attempt cap then escalates it as exactly that, instead of
    retrying a missing directory forever.
    """
    from golddesk.absorb_auto import discover_quant_root, to_inbox
    root, basis = discover_quant_root()
    if root is None:
        log.warning("absorption cannot run: no quant checkout (%s); looked in %s",
                    basis, [str(c) for c in _QUANT_CANDIDATES])
        return False
    try:
        res = to_inbox(root, BASE / "inbox" / "quant_findings.jsonl")
        log.info("absorbed %s finding(s) from %s (%s), dropped %s as not gold",
                 res["written"], root, basis, res["dropped_not_relevant"])
        return True
    except Exception as e:                             # noqa: BLE001
        log.warning("absorption failed: %s", e)
        return False


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
    quant = _quant_root()
    if quant is None:
        log.warning("no quant checkout found in %s — transport cannot run. Set "
                    "AURUM_QUANT_ROOT to point at it.", [str(c) for c in _QUANT_CANDIDATES])
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


#: A fault that is still there is not news. Re-announce an UNCHANGED fault set
#: at most this often, so a standing problem is not forgotten entirely while a
#: 15-minute cadence does not turn the channel into noise. A CHANGED set is
#: always announced immediately -- a new fault, or one clearing, is the event.
RENOTIFY_AFTER = timedelta(hours=12)


def _fault_key(findings) -> str:
    """A stable fingerprint of WHICH checks are unhappy, not of their wording.

    Deliberately the check NAMES only. Detail text carries live numbers -- "15%
    of MFE kept across 6 winners" becomes "14% ... across 7" on the next closed
    trade -- and fingerprinting that would make every fault look new every time
    and defeat the whole mechanism.
    """
    return ",".join(sorted(f.check for f in findings if not f.ok))


def _should_notify(key: str, now: datetime) -> bool:
    """True when the fault SET changed, or when it has stood long enough to be
    worth repeating. Persisted, because the task starts fresh every 15 minutes
    and an in-memory memory would remember nothing."""
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        last_key = d.get("last_fault_key")
        last_at = d.get("last_notified_at")
        last_at = datetime.fromisoformat(last_at) if last_at else None
    except Exception:                                  # noqa: BLE001
        return True
    if key != last_key:
        return True
    if last_at is None:
        return True
    return (now - last_at) >= RENOTIFY_AFTER


def _record_notify(key: str, now: datetime) -> None:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        d = {}
    d["last_fault_key"] = key
    d["last_notified_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _load_attempts() -> dict:
    """Attempt history OUTLIVES the process, or the cooldown is a fiction: a
    task that starts fresh every 15 minutes would have no memory of the restart
    it ordered 15 minutes ago and would loop forever."""
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
        return {k: [datetime.fromisoformat(t) for t in v]
                for k, v in raw.items() if isinstance(v, list)}
    except Exception:                                  # noqa: BLE001
        return {}


def _save_attempts(attempts: dict) -> None:
    """Merge, never overwrite: the notification bookkeeping lives in this same
    file and a blind write would silently reset the de-duplication every pass."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        d = {}
    d = {k: v for k, v in d.items() if not isinstance(v, list)}
    d.update({k: [t.isoformat() for t in v] for k, v in attempts.items()})
    STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")


#: Where a failing scheduled task writes its OWN reason. An explicit map, not a
#: guess: each path is the one the installer wires that task's stdout to.
TASK_LOGS = {
    "AurumSignalDesk-Update": BASE / "logs" / "update.log",
    "AurumSignalDesk-SelfHeal": BASE / "logs" / "self_heal.log",
    "AurumSignalDesk-VantageSpread": BASE / "logs" / "vantage_spread.log",
    "AurumSignalDesk-Cycle": BASE / "logs" / "cycle.log",
}

#: Lines from the tail of a task's log that are worth quoting. Matched
#: case-insensitively. Deliberately short: the point is to name the cause, not
#: to reprint the log into a Telegram message.
_FAILURE_MARKERS = ("abort", "failed", "error", "traceback", "not a fast-forward",
                    "skip:", "exception", "refus")


def _why_the_task_failed(task: str, keep: int = 4, scan: int = 200) -> str:
    """The failing task's own explanation, or an honest UNREADABLE.

    WHY THIS EXISTS. task_health can say `AurumSignalDesk-Update last run exited
    1 -- pulls and deploys fixes is firing and FAILING`, which is true and
    useless: it names the task, not the cause. Meanwhile the cause is one line
    in a log ON THE BOX, and Update-AurumDesk.ps1 has exactly three ways to exit
    1 (git not on PATH, not a fast-forward, suite red) that are trivially told
    apart by reading it.

    OBSERVED 2026-08-28: the updater had been failing long enough that the box
    sat on a commit from before a day of fixes -- login detection, the flag
    ladder, state publishing, all pushed and none deployed -- while every report
    said only "exited 1". The desk was blind on 59 of 59 wakes and the thing
    that would have said why had never been installed, because the thing that
    installs it was the thing that was broken.

    Read-only, bounded to the tail, and quoted rather than interpreted.
    """
    path = TASK_LOGS.get(task)
    if path is None:
        return ""
    try:
        if not path.exists():
            # ABSENT IS A FINDING. A task that exits 1 without ever writing its
            # log died before its first line -- which for a scheduled task means
            # the interpreter or the working directory, not the script's logic.
            return (f"      (no {path.name} — the task failed BEFORE writing a "
                    f"single line, so it is the interpreter, the working "
                    f"directory or the PATH, not the script's own logic)")
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-scan:]
    except Exception as e:                             # noqa: BLE001
        return f"      (could not read {path.name}: {e})"
    hits = [ln.strip() for ln in tail
            if any(m in ln.lower() for m in _FAILURE_MARKERS)]
    if not hits:
        return (f"      (nothing in the last {scan} lines of {path.name} names a "
                f"failure — UNMEASURED, not healthy)")
    return "\n".join(f"      > {ln[:300]}" for ln in hits[-keep:])


#: Publish outcomes that mean the artifact reached the remote.
_PUBLISH_OK = ("pushed", "unchanged")

#: How long a publish may keep failing before the operator is told, in cycles of
#: 15 minutes. TWO, not one: a single rejected push is ordinary -- it races the
#: code branch and the next cycle rebuilds the ref from scratch. Two in a row is
#: not a race, it is a broken channel.
_PUBLISH_ALARM_AFTER = 2

PUBLISH_STATE = BASE / "state" / "publish_health.json"


def _report_publish_health(how: str, dry_run: bool) -> None:
    """Say out loud when the state channel itself is down.

    THE CIRCULARITY THIS CLOSES, and it is a defect I shipped. The whole point of
    state_publish is that the desk's condition reaches someone without them
    logging into the box. But the delivery is a git push, so when the push fails
    -- no credentials on the clone, a rejected ref, no network -- the failure is
    written to a log ON THE BOX. The channel that exists to end "go and look"
    was, in exactly the case that matters, only visible by going and looking.

    Observed 2026-08-28: the operator asked whether the desk was working, the
    artifact had never appeared on the remote, and nothing anywhere could say
    which link had broken.

    So a publish that does not reach the remote escalates to Telegram, which is
    the channel already known to work. Rate-limited to two consecutive failures
    so a lost push race stays quiet, and it announces recovery, because the last
    thing the operator heard must never be that something was broken.
    """
    if dry_run:
        return
    ok = any(how.startswith(p) for p in _PUBLISH_OK)
    try:
        prev = json.loads(PUBLISH_STATE.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        prev = {}
    fails = 0 if ok else int(prev.get("consecutive_failures") or 0) + 1
    alarmed = bool(prev.get("alarmed"))

    msg = None
    if not ok and fails >= _PUBLISH_ALARM_AFTER and not alarmed:
        alarmed = True
        msg = ("*DESK STATE NOT PUBLISHING* — the 15-minute state artifact has "
               f"failed to reach the remote {fails} times running.\n\n"
               f"`{how[:300]}`\n\n"
               "The desk itself may be fine; what is broken is the channel that "
               "reports on it. Until this clears, the desk's condition is only "
               "visible ON THE BOX — which is the situation this artifact exists "
               "to end. Most likely: the clone has no push credentials.")
    elif ok and alarmed:
        alarmed = False
        msg = "*DESK STATE PUBLISHING AGAIN* — the state artifact reached the remote."

    if msg:
        try:
            from golddesk.notify import build_sink
            build_sink(None).send(msg)
        except Exception as e:                         # noqa: BLE001
            log.warning("could not notify about publish health: %s", e)
    try:
        PUBLISH_STATE.parent.mkdir(parents=True, exist_ok=True)
        PUBLISH_STATE.write_text(json.dumps(
            {"consecutive_failures": fails, "alarmed": alarmed,
             "last": how[:300],
             "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    except Exception as e:                             # noqa: BLE001
        log.warning("could not record publish health: %s", e)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="audit and report; take no action")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from golddesk.ledger import Ledger
    from golddesk.opportunity import build_cohorts
    from golddesk import analyst_health as ah
    from golddesk import read_quality as rq
    from golddesk import capture as cap
    from golddesk import task_health as th
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
    from golddesk.state_publish import deployed_commit, running_commit
    _disk, _proc = deployed_commit(BASE), running_commit(BASE)
    findings = audit(rows, cohorts, base=BASE,
                     disk_commit=_disk, process_commit=_proc)
    print(audit_render(findings))

    # THE SECOND AXIS. self_audit asks "is the desk WIRED"; this asks "is it
    # still TAKING WHAT IS THERE". A desk can pass every wiring check and be
    # worth nothing because it refuses everything, banks 15% of the moves it
    # calls right, or stopped receiving the quant desk's survivors -- and none
    # of those raises an error or looks like anything but a quiet week.
    cap_findings = cap.audit(rows, base=BASE)
    print(cap.render(cap_findings))

    # IS THE ANALYST STILL ANSWERING, and as what. A wedged session, a login
    # that expired, or latency drifting into the timeout all look identical to a
    # careful desk from outside -- fewer decisions, no error anywhere.
    ah_findings = ah.audit(rows, expected_model=os.environ.get("AURUM_MODEL"))
    print(ah.render(ah_findings))

    # IS THE ANALYST ANY GOOD, not merely responding. Answerable only against
    # resolved outcomes, so it reports UNMEASURED for as long as that is the
    # honest answer -- which, with two resolved trades, is weeks yet.
    rq_findings = rq.audit(rows)
    print(rq.render(rq_findings))

    # WHO WATCHES THE WATCHDOGS. Every check above runs inside a scheduled task,
    # and a stopped check looks exactly like a passing one.
    th_findings = th.audit(_read_task)
    print(th.render(th_findings))
    # AND WHY. "exited 1" names the task, not the cause, and the cause is one
    # line in that task's own log sitting on this very box. Quoting it turns a
    # report that prompts another round of asking into one that can be acted on.
    for f in th_findings:
        if f.ok:
            continue
        why = _why_the_task_failed(f.check)
        if why:
            print(f"    {f.check} — its own log says:")
            print(why)

    # PUBLISH BEFORE REMEDIATING. The artifact must describe what was FOUND,
    # not what was left after fixing -- a state file that only ever shows the
    # post-fix world cannot answer "what has been going wrong", which is the
    # question it exists for. It also means a crash in remediation still leaves
    # a current artifact behind.
    #
    # THIS IS WHY IT EXISTS AT ALL: every check above prints to a log ON THE
    # BOX, so the only way anyone elsewhere learns the desk is blind is to log
    # in and run something. That cost four hours on 2026-08-28, and it is the
    # difference between a desk that is watched and one that is asked about.
    try:
        from golddesk.state_publish import (build_state, deployed_commit,
                                            publish, running_commit)
        # THE REASON TRAVELS WITH THE FAULT. _why_the_task_failed already reads
        # the failing task's own log, but only into stdout -- so the single most
        # useful line on the box stayed on the box, and the artifact carried
        # "Update exited 1" with no cause, which is the exact uselessness that
        # published artifact exists to end. Enriched here rather than inside
        # task_health so the audit stays pure and filesystem-free.
        import dataclasses
        th_published = []
        for f in th_findings:
            why = "" if f.ok else _why_the_task_failed(f.check)
            th_published.append(
                dataclasses.replace(f, detail=f.detail + (
                    "  ITS OWN LOG SAYS: " + " / ".join(
                        ln.strip().lstrip("> ") for ln in why.splitlines()
                        if ln.strip())
                    if why else ""))
                if why else f)
        state = build_state(rows, {"wiring": findings, "capture": cap_findings,
                                   "analyst": ah_findings,
                                   "read_quality": rq_findings,
                                   "tasks": th_published},
                            commit=deployed_commit(BASE),
                            process_commit=running_commit(BASE))
        _, how = publish(BASE, state, push=not args.dry_run)
        log.info("desk state: %s", how)
        _report_publish_health(how, args.dry_run)
    except Exception as e:                             # noqa: BLE001
        # NEVER the reason a self-heal run fails. Publishing is visibility, and
        # visibility failing must not take down the thing doing the watching.
        log.warning("could not publish desk state: %s", e)
        _report_publish_health(f"crashed: {e}", args.dry_run)

    findings = (list(findings) + list(cap_findings) + list(ah_findings)
                + list(rq_findings) + list(th_findings))

    remedies, escalations = plan(findings, restart_desk=_restart_desk,
                                 refresh_flows=_refresh_flows,
                                 sample_spread=_sample_spread,
                                 refresh_macro=_refresh_macro,
                                 rotate_logs=_rotate_logs,
                                 sync_quant=_sync_quant,
                                 absorb_now=_absorb_now,
                                 run_update=_run_update,
                                 enable_task=_enable_task)
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
        print(report)                                  # the log gets it EVERY run
        # THE CHANNEL DOES NOT. A fault that is still there is not news, and at
        # a 15-minute cadence a standing problem -- capture at 15%, an absent
        # spread profile -- would be 96 messages a day. An alert channel that
        # fires every quarter hour is one nobody reads, which costs more than
        # the alert was ever worth. Announced when the fault SET changes, and
        # otherwise at most twice a day so a standing problem is not forgotten.
        now = datetime.now(timezone.utc)
        key = _fault_key([f for f in findings if not f.ok])
        if _should_notify(key, now):
            try:
                from golddesk.notify import build_sink
                build_sink(None).send("*SELF-HEAL*\n" + report)
                _record_notify(key, now)
            except Exception as e:                     # noqa: BLE001
                log.warning("could not notify: %s", e)
        else:
            log.info("fault set unchanged — logged, not re-sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
