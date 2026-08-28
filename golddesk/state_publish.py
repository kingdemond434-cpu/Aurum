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

WHY IT PUBLISHES TO ITS OWN REF, AND WHY THE OBVIOUS DESIGN WAS WRONG

The first version of this committed the artifact onto the desk's CODE branch.
That is broken, and the thing it breaks is the auto-updater:

  Update-AurumDesk.ps1 advances the box with `git merge --ff-only`, and skips
  entirely when `git status --porcelain` is non-empty. So an artifact committed
  on the code branch whose push loses a race to a code push leaves the box
  DIVERGED -- "ABORT: not a fast-forward" -- and auto-update stops until a human
  intervenes. Writing the file into a tracked directory dirties the tree and
  makes the updater skip on its own. A visibility fix that disables the update
  path is worse than no visibility fix.

So state is published as a COMMIT BUILT BY PLUMBING on a dedicated ref
(STATE_BRANCH). hash-object, mktree, commit-tree and update-ref never read or
write the index, HEAD, the working tree or the current branch, so this cannot
dirty the tree, cannot diverge the code branch, and cannot race the updater --
not by policy, but because it never touches any of them. The local file is a
buffer only, and is gitignored so the code tree stays permanently clean.

Read it with:

    git fetch origin aurum-state
    git show origin/aurum-state:desk_state.json

WHY IT IS SAFE TO RUN UNATTENDED

  - it never runs `git add`, `git commit`, `git checkout`, `git pull` or
    `git reset`, so R0423's failure -- a broad commit sweeping a sibling
    session's staged files -- is not merely avoided but unreachable;
  - it never stashes (same law; a stash restores to the index and a sibling can
    check the tree out from under you);
  - it never force-pushes. A rejected push is left for the next cycle, fifteen
    minutes away, and the ref is rebuilt from scratch each time anyway;
  - an unchanged state does no git work at all.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

#: LOCAL BUFFER ONLY, and gitignored. It exists so an unchanged state can be
#: detected without touching git at all; the published copy lives on
#: STATE_BRANCH, built by plumbing, and never enters this tree's index.
ARTIFACT = Path("reports") / "desk_state.json"

#: The ref the artifact is published on. A branch of its own, carrying ONE file
#: and no code, so it can never fast-forward-block the desk's code branch or
#: dirty the tree the auto-updater checks.
STATE_BRANCH = "aurum-state"

#: The artifact's name inside that branch. Flat: the state branch has no
#: directory structure to mirror, and a reader should not have to know where the
#: buffer happened to sit on the box.
FILENAME = "desk_state.json"

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


def _git(base: Path, *args: str, stdin: Optional[str] = None,
         timeout: int = 120):
    return subprocess.run(["git", "-C", str(base), *args], input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def publish(base: Path, state: dict, *, push: bool = True,
            runner=None) -> tuple[bool, str]:
    """Build a one-file commit on STATE_BRANCH by plumbing and push it.

    Returns (changed, message). `changed` is False when the state is
    byte-identical to the buffer already on disk -- the common case, fifteen
    minutes at a time -- and that check happens BEFORE any git call, so a quiet
    cycle costs nothing.

    NOTHING HERE READS OR WRITES THE INDEX, HEAD, THE WORKING TREE OR THE
    CURRENT BRANCH. hash-object, mktree, commit-tree and update-ref all operate
    on the object database directly. That is the property that makes it safe to
    run every fifteen minutes underneath an auto-updater that advances with
    `merge --ff-only` and skips on a dirty tree: this cannot dirty the tree and
    cannot diverge the branch, because it never touches either.
    """
    git = runner or (lambda *a, **kw: _git(base, *a, **kw))
    path = base / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, indent=2, sort_keys=True, default=str) + "\n"

    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False, "unchanged"
    path.write_text(text, encoding="utf-8")

    # 1. The file becomes a blob. -w writes it to the object database; it does
    #    NOT stage it.
    blob = git("hash-object", "-w", "--", str(path))
    if blob.returncode != 0:
        return False, f"hash-object failed: {(blob.stderr or blob.stdout)[:200]}"
    sha = (blob.stdout or "").strip()
    if not sha:
        return False, "hash-object produced no sha"

    # 2. A tree containing exactly that one blob. Built from stdin rather than
    #    from the index, which is why the index is never involved.
    tree = git("mktree", stdin=f"100644 blob {sha}\t{FILENAME}\n")
    if tree.returncode != 0:
        return False, f"mktree failed: {(tree.stderr or tree.stdout)[:200]}"
    tree_sha = (tree.stdout or "").strip()

    # 3. Parent is the previous state commit, if the ref exists. Missing on the
    #    first run, and an absent ref is a normal state rather than an error --
    #    the first publish is a root commit.
    head = git("rev-parse", "--verify", "--quiet", f"refs/heads/{STATE_BRANCH}")
    parent = (head.stdout or "").strip() if head.returncode == 0 else ""

    # 4. commit-tree needs an identity, and a deployment box may have none
    #    configured. Supplied per-invocation with -c rather than by writing to
    #    the repo config, because changing a box's git identity is a side effect
    #    nobody asked for.
    ident = ["-c", "user.name=aurum-desk",
             "-c", "user.email=aurum-desk@localhost"]
    args = [*ident, "commit-tree", tree_sha]
    if parent:
        args += ["-p", parent]
    msg = f"aurum desk state {state.get('generated_utc', '')}"
    com = git(*args, stdin=msg + "\n")
    if com.returncode != 0:
        return False, f"commit-tree failed: {(com.stderr or com.stdout)[:200]}"
    commit = (com.stdout or "").strip()

    upd = git("update-ref", f"refs/heads/{STATE_BRANCH}", commit)
    if upd.returncode != 0:
        return False, f"update-ref failed: {(upd.stderr or upd.stdout)[:200]}"
    if not push:
        return True, f"{STATE_BRANCH} updated (push disabled)"

    # 5. NEVER --force. The ref is rebuilt from scratch every cycle, so a
    #    rejected push costs one cycle and nothing else; forcing would trade a
    #    fifteen-minute delay for the chance of discarding somebody's work.
    p = git("push", "origin",
            f"refs/heads/{STATE_BRANCH}:refs/heads/{STATE_BRANCH}")
    if p.returncode == 0:
        return True, "pushed"
    return True, (f"{STATE_BRANCH} updated locally; push failed, will retry "
                  f"next cycle: {(p.stderr or p.stdout)[:200]}")
