r"""Push the desk's own state to git, so nobody has to be asked what it is.

WHY THIS EXISTS

Every audit in this repo runs ON THE BOX and prints to a log ON THE BOX. That
means the only way anyone off the box learns whether the desk is reading the
market is to log in and run something -- and on 2026-08-28 that cost four hours
of blind bars, because the fact that the CLI's login had expired existed in a
file nobody was looking at, on a machine nobody was logged into.

The MT5 desk already solved exactly this and the fix is copied from it:
`desks/mt5/scripts/sync_shadow_to_git.ps1` commits `shadow_health.json` every
fifteen minutes, so cross-brain visibility travels through git instead of
through a person. Aurum never got that, so Aurum's state was private to the
Administrator session.

WHAT IT PUBLISHES, AND WHAT IT REFUSES TO

A BOUNDED SUMMARY: audit verdicts, counts, timestamps, the last decision of each
kind, and the CLI's own explanation when a read failed. That is enough to answer
"is it working, and if not, why" without anybody typing a command.

It publishes NO secrets, NO credentials and NO environment: `data/secrets/**`
never leaves the box, and the allowlist below is positive -- fields are copied in
by name, never filtered out by pattern. A denylist is one forgotten key away
from leaking; an allowlist fails closed.

WHY IT IS SAFE TO RUN UNATTENDED

It is deterministic, bounded to a single file, and it REFUSES rather than forces:
  - it stages exactly one path, never `git commit -a` (R0423 -- three recorded
    instances of a broad commit sweeping a sibling session's staged files);
  - it never stashes (same law; a stash restores to the index and a sibling can
    check the tree out from under you);
  - if the working tree carries changes to anything else, it does NOT commit --
    a deployment clone should be clean, and a dirty one means something is going
    on that an automated commit would bury;
  - a rejected push is retried exactly once, after a rebase, and then left for
    the next cycle. It never force-pushes.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

#: Where the published artifact lands. Tracked in git ON PURPOSE -- the whole
#: point is that it travels.
ARTIFACT = Path("reports") / "desk_state.json"

#: Decision kinds summarised in the artifact. Explicit rather than "whatever is
#: in the ledger", so a new kind is a deliberate addition here rather than an
#: unreviewed field appearing in a published file.
KINDS = ("SIGNAL", "BLIND", "REFUSAL_MODEL", "REFUSAL_COMPILER",
         "REFUSAL_ROUTER", "REFUSAL_RISK")

#: Fields copied out of a BLIND row's CLI detail. POSITIVE allowlist: a denylist
#: is one forgotten key away from publishing something it should not.
CLI_FIELDS = ("subtype", "result", "stop_reason", "is_error", "duration_api_ms",
              "input_tokens", "output_tokens", "reading", "needs_login")


def _ts(row: dict) -> Optional[str]:
    v = row.get("t0") or row.get("ts")
    return str(v) if v else None


def _summarise_findings(name: str, findings: Sequence[Any]) -> dict:
    """Verdicts only -- check name, pass/fail, and the detail sentence.

    The detail is what makes the artifact worth reading: "BROKEN analyst login"
    is a status, "THE LOGIN HAS EXPIRED -- run `claude` once interactively" is
    an instruction, and only one of them saves the trip.
    """
    items = []
    for f in findings:
        items.append({"check": getattr(f, "check", "?"),
                      "ok": bool(getattr(f, "ok", False)),
                      "detail": str(getattr(f, "detail", ""))[:600]})
    return {"axis": name,
            "faults": sum(1 for i in items if not i["ok"]),
            "checks": items}


def build_state(rows: Sequence[dict], audits: dict[str, Sequence[Any]],
                now: Optional[datetime] = None) -> dict:
    """The artifact. Pure -- no git, no filesystem, so it is trivially testable."""
    now = now or datetime.now(timezone.utc)
    by_kind: dict[str, dict] = {}
    for k in KINDS:
        matching = [r for r in rows if r.get("kind") == k]
        last = matching[-1] if matching else None
        entry: dict[str, Any] = {"count": len(matching),
                                 "last": _ts(last) if last else None}
        if k == "BLIND" and last is not None:
            dec = last.get("decision") or {}
            cli = dec.get("cli") or {}
            entry["why"] = {f: cli[f] for f in CLI_FIELDS if f in cli}
            if dec.get("needs_login"):
                entry["why"]["needs_login"] = True
            entry["stage"] = dec.get("stage")
            entry["error"] = str(dec.get("error") or "")[:400]
        by_kind[k] = entry

    faults = sum(a["faults"] for a in
                 (_summarise_findings(n, f) for n, f in audits.items()))
    return {
        "generated_utc": now.isoformat(),
        # THE STALENESS CLOCK. The MT5 desk's artifact went 35 hours stale while
        # every number in it kept being read as current; a reader has to be able
        # to date what they are looking at without trusting that it was fresh.
        "ledger_rows": len(rows),
        "last_row_utc": _ts(rows[-1]) if rows else None,
        "total_faults": faults,
        "decisions": by_kind,
        "audits": {n: _summarise_findings(n, f) for n, f in audits.items()},
    }


def _git(base: Path, *args: str, timeout: int = 120):
    return subprocess.run(["git", "-C", str(base), *args],
                          capture_output=True, text=True, timeout=timeout)


def publish(base: Path, state: dict, *, push: bool = True,
            runner=None) -> tuple[bool, str]:
    """Write, stage, commit and push the artifact. Returns (changed, message).

    `changed` is False when the state is byte-identical to what is already
    committed -- the common case, fifteen minutes at a time. An empty commit
    every cycle would bury the ones that mean something.
    """
    git = runner or (lambda *a: _git(base, *a))
    path = base / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, indent=2, sort_keys=True, default=str) + "\n"

    # NOTHING TO SAY. Compared against the file rather than against git, so a
    # cycle that finds no change does no git work at all.
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False, "unchanged"

    # REFUSE ON A DIRTY TREE. A deployment clone should carry nothing but this
    # artifact; anything else means work is in progress that an automated commit
    # would sweep up. R0423 records three instances of exactly that.
    st = git("status", "--porcelain")
    if st.returncode != 0:
        return False, f"git status failed: {(st.stderr or st.stdout)[:200]}"
    dirty = [ln[3:].strip() for ln in (st.stdout or "").splitlines() if ln.strip()]
    unexpected = [p for p in dirty if p.replace("\\", "/") != ARTIFACT.as_posix()]
    if unexpected:
        return False, (f"working tree carries {len(unexpected)} other change(s) "
                       f"({unexpected[:4]}) — NOT committing over them")

    path.write_text(text, encoding="utf-8")
    add = git("add", "--", ARTIFACT.as_posix())        # explicit path, never -a
    if add.returncode != 0:
        return False, f"git add failed: {(add.stderr or add.stdout)[:200]}"
    msg = f"aurum desk state {state.get('generated_utc', '')}"
    com = git("commit", "-m", msg, "--", ARTIFACT.as_posix())
    if com.returncode != 0:
        out = (com.stdout or "") + (com.stderr or "")
        if "nothing to commit" in out:
            return False, "unchanged"
        return False, f"git commit failed: {out[:200]}"
    if not push:
        return True, "committed (push disabled)"

    p = git("push")
    if p.returncode == 0:
        return True, "pushed"
    # ONE rebase and ONE retry. A push races the code branch; losing that race
    # is ordinary and the next cycle is fifteen minutes away, so this never
    # loops and never forces.
    rb = git("pull", "--rebase")
    if rb.returncode != 0:
        return True, (f"committed; rebase failed, will retry next cycle: "
                      f"{(rb.stderr or rb.stdout)[:200]}")
    p2 = git("push")
    if p2.returncode == 0:
        return True, "pushed after rebase"
    return True, (f"committed; push rejected twice, will retry next cycle: "
                  f"{(p2.stderr or p2.stdout)[:200]}")
