"""Is this desk WIRED, or merely running?

WHY A SECOND KIND OF PREFLIGHT

run_desk.py's preflight checks the world: is MT5 up, does the broker match, does
Telegram deliver. Every one of those passed all day on 2026-08-27 while the desk
was quietly broken in five separate places. They were not world problems:

  cohorts never reached the live desk, so every mechanism priced off the
  cold-start prior forever, no matter how many trades resolved

  tp1 was computed, journalled and rendered, and compared to price by nothing,
  so a +1.88R move banked +0.29R

  the observer's tick count and excursion path reset on every restart, so an
  exit reported "MFE +0.00R, 0 observations" on a trade held for hours

  the learning cycle had no Windows launcher at all -- its only trigger in the
  repo was a Linux systemd unit

  a bar the analyst never answered on left NO ledger row, so a blind session and
  a disciplined one were the same file

Each part worked in isolation and passed its own tests. What was missing was the
JOIN, and a join is invisible to any check that looks at one side of it.

WHAT THIS DOES INSTEAD

Every check below reads RUNTIME STATE and asserts a relationship between two
things that must agree. It runs at boot, costs milliseconds, and reports rather
than blocks -- a desk that refuses to start because an audit is unhappy is worse
than one that starts and says so loudly.

WHAT IT CANNOT DO, stated because the operator asked exactly this: it cannot FIX
a defect. "The compiler computes a field nothing reads" is a design fault that
needs someone to read the code and understand the intent. What a check can do is
make the fault ANNOUNCE ITSELF the same day instead of surviving weeks because
every part looks fine on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

SELF_AUDIT_VERSION = "audit-2026-08-28-a"

#: Resolved trades before "cohorts is empty" stops being ordinary and starts
#: being a wiring fault. One resolved trade with no cohort is a race; several is
#: a join that does not exist.
COHORT_EXPECTED_AFTER = 1

#: Signals that reached TP1 without a single bank before it is called unwired.
#: Two, not one -- a single trade could legitimately have been rejected by the
#: runner-fraction invariant.
TP1_UNWIRED_AFTER = 2


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str
    #: True when a bounded, allowlisted action fixes this. Most wiring faults are
    #: design faults and stay False -- they need code, and a healer that pretends
    #: otherwise loops on something it cannot mend.
    fixable: bool = False

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'BROKEN'}] {self.check:<26} {self.detail}"


def _closed(rows: Sequence[dict]) -> list[dict]:
    return [r for r in rows if r.get("kind") == "TRADE_CLOSED"]


def _signals(rows: Sequence[dict]) -> list[dict]:
    return [r for r in rows if r.get("kind") == "SIGNAL"]


def check_cohorts_are_loaded(rows: Sequence[dict], cohorts: Optional[dict]) -> Finding:
    """Resolved history exists AND the desk is holding it.

    THE DEFECT THIS CATCHES. build_cohorts() was correct and called by adapt.py
    and acceptance.py -- never by build_service. LiveDesk.cohorts stayed None
    forever, so ev_gate took its cold-start branch on every decision and a
    mechanism with eighty wins priced exactly like one never traded. Both sides
    looked healthy; only their relationship was wrong.
    """
    n = len(_closed(rows))
    if n < COHORT_EXPECTED_AFTER:
        return Finding("cohorts", True,
                       f"{n} resolved trade(s) — too few to expect cohorts yet")
    if not cohorts:
        return Finding("cohorts", False,
                       f"{n} resolved trades in the ledger and the desk holds NO "
                       f"cohorts. Every mechanism is pricing off the cold-start "
                       f"prior — the desk cannot learn from its own results.")
    return Finding("cohorts", True,
                   f"{len(cohorts)} mechanism(s) loaded from {n} resolved trades")


def check_tp1_is_acted_on(rows: Sequence[dict]) -> Finding:
    """Trades that reached TP1 must show banks.

    THE DEFECT THIS CATCHES. tp1 was computed under the comment "partial bank",
    journalled, rendered to Telegram -- and compared to price by nothing. A
    short reached +1.88R with TP1 at +1.78R and kept +0.29R.
    """
    reached, banked = 0, 0
    for c in _closed(rows):
        mfe, rr1 = c.get("mfe_r"), None
        for s in _signals(rows):
            if str(s.get("t0")) == str(c.get("entry_t0")):
                rr1 = (s.get("decision") or {}).get("rr_tp1")
                break
        if mfe is None or rr1 is None:
            continue
        if float(mfe) >= float(rr1):
            reached += 1
            if any(m.get("source") == "tp1" for m in (c.get("management") or [])):
                banked += 1
    if reached < TP1_UNWIRED_AFTER:
        return Finding("tp1 banking", True,
                       f"{reached} trade(s) reached TP1 — too few to judge")
    if banked == 0:
        return Finding("tp1 banking", False,
                       f"{reached} trades reached TP1 and NONE banked. TP1 is "
                       f"being computed and shown and acted on by nothing.")
    return Finding("tp1 banking", True, f"{banked} of {reached} TP1 touches banked")


def check_excursion_survives(rows: Sequence[dict]) -> Finding:
    """A closed trade must carry a real excursion record.

    THE DEFECT THIS CATCHES. checkpoint() wrote the observer's tick count and
    rehydrate() never read it back, and the path was never written at all, so a
    restart reset both. Exits reported "MFE +0.00R - MAE +0.00R - 0
    observations" on positions held for hours.
    """
    closed = _closed(rows)
    if not closed:
        return Finding("excursion", True, "no closed trades yet")

    def bare(c: dict) -> bool:
        return (c.get("observations") == 0
                and float(c.get("mfe_r") or 0) == 0.0
                and float(c.get("mae_r") or 0) == 0.0)

    # A FIXED DEFECT HAS TO BE ABLE TO CLEAR. This scanned every closed trade
    # ever, so the two that closed BEFORE the persistence fix landed kept it
    # BROKEN permanently -- and a check that can never go green is read as
    # furniture within a week. It was still reporting "the observer is not being
    # fed" on 2026-08-28, days after rehydrate() began restoring ticks and path
    # and with test_observer_survives_restart.py pinning that it does.
    #
    # THE BOUNDARY IS MEASURED, NOT A DATE. The first closed trade carrying real
    # excursion is the point the desk demonstrably started recording it, so
    # anything bare AFTER that is a live defect and anything bare before it is
    # history that no fix can retrieve. Nothing to go stale, and a desk that has
    # NEVER recorded excursion still fails on every trade -- which is correct,
    # and is the case this check was originally written for.
    def opened(c: dict) -> str:
        # entry_t0 is the trade's OPEN. Ordering by CLOSE was wrong: a trade
        # that opened under the broken observer and closed after the fix carries
        # zero observations through no fault of current code, and no fix can
        # retroactively give it a path. Judging it by its close date blamed the
        # running desk for a trade it inherited.
        return str(c.get("entry_t0") or c.get("ts") or "")

    good_opens = [opened(c) for c in closed if not bare(c)]
    if not good_opens:
        return Finding("excursion", False,
                       f"NONE of {len(closed)} closed trades carries excursion. "
                       f"The observer is not being fed, or its state is lost on "
                       f"restart — every stop-placement question is unanswerable "
                       f"from this data.")
    # The earliest open that DID produce excursion is the point the observer is
    # known to have been working. A bare trade that opened before it had no
    # working observer to lose; one that opened after did.
    boundary = min(good_opens)
    since = [c for c in closed if opened(c) >= boundary]
    bad = [c for c in since if bare(c)]
    historical = sum(1 for c in closed if bare(c) and opened(c) < boundary)
    tail = (f" ({historical} earlier trade(s) predate the fix and are not counted "
            f"— their excursion is gone for good)" if historical else "")
    if bad:
        return Finding("excursion", False,
                       f"{len(bad)} of {len(since)} closed trades carry ZERO "
                       f"observations and zero excursion SINCE the observer "
                       f"started recording — its state is being lost again, and "
                       f"stop placement is unanswerable for those.{tail}")
    return Finding("excursion", True,
                   f"all {len(since)} closed trades since the observer started "
                   f"recording carry excursion{tail}")


def check_running_code_is_current(disk: str, process: str) -> Finding:
    """Is the desk EXECUTING the code that is installed?

    THE GAP THIS CLOSES. `git pull` updates the working tree; it does not reload
    a long-running Python process. So every report that reads HEAD says the fix
    is deployed while the desk goes on running whatever it started with, and the
    two sentences "the fix is deployed" and "the fix is running" -- which are
    not the same claim -- were collapsed into one.

    OBSERVED 2026-08-28: the artifact reported a deployed commit that contained
    the rule-based fallback, while the desk, up since before that fallback
    existed, kept booking BLIND on every wake. The fix was present, installed,
    and not running, and nothing anywhere could see the difference.

    Mechanical, and the remedy is the one the healer already has: bounce the
    task. Deliberately NOT a comparison against "latest on the remote" -- that
    is the updater's job, and a check that fires whenever origin moves would be
    red most of the time and read as furniture within a week.
    """
    if "unknown" in (disk, process) or not disk or not process:
        return Finding("running code", True,
                       f"UNMEASURED — disk {disk or '?'}, process {process or '?'}. "
                       f"Not the same as agreement.")
    if disk[:12] != process[:12]:
        return Finding("running code", False,
                       f"the desk is RUNNING {process[:12]} while {disk[:12]} is "
                       f"installed. Every fix in between is present on disk and "
                       f"not executing — a restart is the only thing that makes "
                       f"installed code run.", fixable=True)
    return Finding("running code", True, f"process and disk agree at {disk[:12]}")


def check_blind_bars_are_journalled(rows: Sequence[dict]) -> Finding:
    """The ledger must be able to say the desk was blind.

    Not a failure when zero BLIND rows exist -- that is the healthy case. This
    reports the count so an outage is visible in the audit rather than only in a
    log, and so "quiet market" is never inferred from silence.
    """
    blind = [r for r in rows if r.get("kind") == "BLIND"]
    if not blind:
        return Finding("blind bars", True, "none recorded")
    return Finding("blind bars", True,
                   f"{len(blind)} bar(s) the analyst never answered on — these "
                   f"are NOT refusals and no gate earns credit for them")


def check_ledger_is_growing(rows: Sequence[dict],
                            now: Optional[datetime] = None,
                            quiet_hours: float = 12.0) -> Finding:
    """A ledger that has stopped growing is a desk that has stopped deciding."""
    now = now or datetime.now(timezone.utc)
    stamps = []
    for r in rows:
        raw = r.get("t0") or r.get("ts")
        if not raw:
            continue
        try:
            stamps.append(datetime.fromisoformat(str(raw)))
        except ValueError:
            continue
    if not stamps:
        return Finding("ledger growth", True, "ledger empty — nothing decided yet")
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_h = (now - newest).total_seconds() / 3600.0
    if age_h > quiet_hours:
        return Finding("ledger growth", False,
                       f"newest row is {age_h:.1f}h old. Over a weekend this is "
                       f"normal; inside a trading week it means the desk is not "
                       f"reaching decisions at all.")
    return Finding("ledger growth", True, f"newest row {age_h:.1f}h old")


# --------------------------------------------------------------------------
# System checks. These read the FILESYSTEM rather than the ledger, and each one
# corresponds to a fault observed on the live box on 2026-08-27.
# --------------------------------------------------------------------------

def check_spread_profile(base: Path) -> Finding:
    """Expectancy must be priced against the venue that fills the order.

    OBSERVED LIVE: "NO SPREAD PROFILE -- costs will be taken from the FEED,
    which is not your execution venue." The desk reads Fusion and the operator
    fills on Vantage, so every R:R figure was optimistic by whatever Vantage
    charges over Fusion. It matters least on wide stops and most on the tight
    ones, which is the wrong way round for a fault to hide.
    """
    prof = base / "config" / "spread_profile.json"
    if not prof.exists():
        return Finding("spread profile", False,
                       "absent — every expectancy figure is priced against the "
                       "FEED's spread, not the venue that actually fills you")
    try:
        d = json.loads(prof.read_text(encoding="utf-8"))
    except Exception as e:                             # noqa: BLE001
        return Finding("spread profile", False, f"unreadable ({e})")
    if not d.get("by_session"):
        return Finding("spread profile", False,
                       "present but NO session was calibrated — a profile with "
                       "no measured session prices nothing")
    return Finding("spread profile", True,
                   f"{len(d['by_session'])} session(s) measured on "
                   f"{d.get('calibrated_from', 'unknown venue')}")


def check_notifications_deliver(base: Path) -> Finding:
    """A desk whose messages do not arrive has no product.

    The message IS this desk's entire output. Silence from a broken channel and
    silence from a quiet market are indistinguishable to the operator, which is
    the same shape as the BLIND defect one level out.
    """
    st = base / "state" / "service_state.json"
    if not st.exists():
        return Finding("notifications", True, "no checkpoint yet")
    try:
        d = json.loads(st.read_text(encoding="utf-8"))
    except Exception as e:                             # noqa: BLE001
        return Finding("notifications", True, f"checkpoint unreadable ({e})")
    h = d.get("notification_health") or {}
    sent, failed = h.get("sent"), h.get("failed")
    if sent is None and failed is None:
        return Finding("notifications", True,
                       "this sink does not track delivery — health is UNKNOWN, "
                       "which is not the same as healthy")
    total = (sent or 0) + (failed or 0)
    if total and (failed or 0) / total > 0.5:
        return Finding("notifications", False,
                       f"{failed} of {total} sends FAILED — the desk is deciding "
                       f"into a void and its silence looks like a quiet market")
    return Finding("notifications", True, f"{sent or 0} delivered, {failed or 0} failed")


def check_checkpoint_is_fresh(base: Path, now: Optional[datetime] = None,
                              stale_hours: float = 6.0) -> Finding:
    """A checkpoint that has stopped moving means an open position nobody is
    persisting -- a crash from here loses the position's whole excursion."""
    now = now or datetime.now(timezone.utc)
    st = base / "state" / "service_state.json"
    if not st.exists():
        return Finding("checkpoint", True, "none yet")
    age_h = (now.timestamp() - st.stat().st_mtime) / 3600.0
    if age_h > stale_hours:
        return Finding("checkpoint", False,
                       f"last written {age_h:.1f}h ago — the desk is not "
                       f"persisting state, so a crash loses everything since")
    return Finding("checkpoint", True, f"written {age_h:.1f}h ago")


def check_ledger_integrity(base: Path) -> Finding:
    """Torn lines and duplicate decision ids, counted rather than repaired.

    NEVER auto-repaired. The ledger is the only record of what this desk
    predicted and what happened; a process that edits it unattended can destroy
    the evidence it exists to protect. Counting is the whole job here.
    """
    led = base / "state" / "ledger.jsonl"
    if not led.exists():
        return Finding("ledger integrity", True, "no ledger yet")
    torn, ids, dupes = 0, set(), 0
    for line in led.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:                              # noqa: BLE001
            torn += 1
            continue
        did = r.get("decision_id")
        if did:
            if did in ids:
                dupes += 1
            ids.add(did)
    if torn or dupes:
        return Finding("ledger integrity", False,
                       f"{torn} torn line(s), {dupes} duplicate decision_id(s). "
                       f"NOT auto-repaired: this file is the only record of what "
                       f"the desk predicted, and editing evidence unattended is "
                       f"how it gets destroyed.")
    return Finding("ledger integrity", True, f"{len(ids)} unique decision(s), no tears")


def check_disk_headroom(base: Path, min_free_mb: float = 500.0) -> Finding:
    """A full disk stops the ledger silently -- writes fail, the desk continues."""
    try:
        import shutil
        free_mb = shutil.disk_usage(base).free / (1024 * 1024)
    except Exception as e:                             # noqa: BLE001
        return Finding("disk", True, f"UNMEASURED ({e})")
    if free_mb < min_free_mb:
        return Finding("disk", False,
                       f"{free_mb:.0f}MB free — below {min_free_mb:.0f}MB. Ledger "
                       f"writes fail silently on a full disk and the desk keeps "
                       f"deciding as if they succeeded.")
    return Finding("disk", True, f"{free_mb:.0f}MB free")


def check_macro_is_measured(rows: Sequence[dict]) -> Finding:
    """Gold's entire bid is macro. Briefs reading UNMEASURED are briefs the
    analyst took blind to DXY, real yields and risk.

    OBSERVED LIVE: yfinance returned "possibly delisted" for DX-Y.NYB, ^GSPC and
    ^VIX simultaneously -- the Yahoo API, not three delistings -- so every brief
    carried no macro at all.
    """
    recent = [r for r in rows if r.get("brief_render")][-20:]
    if not recent:
        return Finding("macro", True, "no briefs yet")
    blind = sum(1 for r in recent if "UNMEASURED" in str(r.get("brief_render")))
    if blind == len(recent):
        return Finding("macro", False,
                       f"all {len(recent)} recent briefs carried MACRO UNMEASURED "
                       f"— the analyst is reading gold with no DXY, no real "
                       f"yield and no risk proxy")
    return Finding("macro", True, f"{len(recent) - blind}/{len(recent)} briefs carried macro")


def check_desk_started_after_boot(base: Path,
                                  boot_time: Optional[datetime] = None,
                                  now: Optional[datetime] = None,
                                  grace_min: float = 15.0) -> Finding:
    """Did the desk actually come back up after the machine last booted?

    THE GAP THIS PARTLY CLOSES. Every task runs at LogonType Interactive, so a
    reboot that never reaches a desktop takes the desk AND every watchdog
    together, and nothing is left running to notice. That case cannot be caught
    from inside the box at all.

    What CAN be caught is the near miss: the machine rebooted, something is
    running again, and the desk is NOT among it -- a logon that happened late, a
    task that failed to start, an autologon that worked once and stopped. The
    checkpoint's mtime says when the desk last wrote; the boot time says when
    the machine last started. A desk whose newest write predates the boot did
    not come back.

    UNMEASURED when boot time is unavailable, which it is on any box without
    psutil -- and stated as such rather than passed.
    """
    now = now or datetime.now(timezone.utc)
    if boot_time is None:
        try:
            import psutil
            boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
        except Exception:                              # noqa: BLE001
            return Finding("came back after boot", True,
                           "UNMEASURED — boot time unavailable (no psutil). Not "
                           "the same as healthy.")
    st = base / "state" / "service_state.json"
    if not st.exists():
        return Finding("came back after boot", True, "no checkpoint yet")
    wrote = datetime.fromtimestamp(st.stat().st_mtime, tz=timezone.utc)
    if (now - boot_time).total_seconds() / 60.0 < grace_min:
        return Finding("came back after boot", True,
                       f"booted {(now - boot_time).total_seconds() / 60:.0f} min "
                       f"ago — inside the {grace_min:.0f} min grace")
    if wrote < boot_time:
        return Finding("came back after boot", False,
                       f"the machine booted {boot_time.isoformat()} and the desk "
                       f"has not written since {wrote.isoformat()}. It did not "
                       f"come back — a late logon, a task that failed to start, "
                       f"or autologon that stopped working.")
    return Finding("came back after boot", True,
                   f"wrote {(now - wrote).total_seconds() / 60:.0f} min ago, after "
                   f"a boot {(now - boot_time).total_seconds() / 3600:.1f}h ago")


def audit(rows: Sequence[dict], cohorts: Optional[dict] = None,
          now: Optional[datetime] = None,
          base: Optional[Path] = None,
          disk_commit: Optional[str] = None,
          process_commit: Optional[str] = None) -> list[Finding]:
    """Ledger checks always; filesystem checks when a base path is supplied.

    `base` is optional so the ledger half stays trivially testable without a
    filesystem, and so a caller with no checkout (a backtest, a notebook) gets
    the checks it can actually answer rather than a spray of UNMEASURED.
    """
    out = [
        check_running_code_is_current(disk_commit or "unknown",
                                      process_commit or "unknown"),
        check_cohorts_are_loaded(rows, cohorts),
        check_tp1_is_acted_on(rows),
        check_excursion_survives(rows),
        check_blind_bars_are_journalled(rows),
        check_ledger_is_growing(rows, now),
        check_macro_is_measured(rows),
    ]
    if base is not None:
        out += [
            check_spread_profile(base),
            check_notifications_deliver(base),
            check_checkpoint_is_fresh(base, now),
            check_ledger_integrity(base),
            check_disk_headroom(base),
            check_desk_started_after_boot(base),
        ]
    return out


def render(findings: Sequence[Finding]) -> str:
    broken = [f for f in findings if not f.ok]
    head = (f"SELF-AUDIT ({SELF_AUDIT_VERSION}) — "
            + ("all wiring checks pass" if not broken
               else f"{len(broken)} WIRING FAULT(S)"))
    out = [head] + [f.line for f in findings]
    if broken:
        out += ["",
                "  These are JOINS, not components. Every part named above works",
                "  in isolation and passes its own tests; what is broken is the",
                "  relationship between two of them, which no single-sided check",
                "  can see. Fixing one needs someone to read the code — this can",
                "  only make it announce itself the same day."]
    return "\n".join(out)
