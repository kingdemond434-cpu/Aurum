# Two Claude sessions are working on this repo. Read this before touching anything.

Two accounts, two Claude Code sessions, no way for either to message the other
directly — no shared agent registry connects them. This file is the only
channel that persists between sessions. If you are a session picking up work
here, **read this file's current state before editing `run_desk.py`,
`golddesk/providers.py`, or `golddesk/service.py`** — those three have collided
three times in one day already, each time invisibly to `git merge` (no
conflict markers, clean merge, broken code) because both sessions added the
same thing on adjacent, non-overlapping lines.

## What already collided, so it does not happen a fourth time

1. `run_desk.py`: both sessions added `provider_spec=args.provider,` to the
   same `build_service(...)` call → `SyntaxError: keyword argument repeated`.
   `run_desk.py` did not parse. Fixed in `32329b6`.
2. `golddesk/providers.py`: both sessions built a class named
   `ClaudeCodeAnalyst` with genuinely incompatible interfaces — one an
   `AnalystProvider.read()` implementation, one a `client.messages.create()`
   shim for injection into `AnthropicAnalyst`. Python silently kept the later
   definition; the `PROVIDERS` dict that survived pointed at the broken
   combination — `AnthropicAnalyst(client=<wrong object>)`, which would have
   crashed with `AttributeError` on the first real analyst call, live, on a
   real account. Fixed in `32329b6` — the untested shim was deleted, not kept
   alongside.
3. `run_desk.py` again: both sessions independently added
   `ap.add_argument("--provider", ...)` → `argparse.ArgumentError`. The desk
   could not parse its own arguments. Fixed in `efbc691`.

None of these were bad luck. Two sessions editing the same file with no
coordination will keep producing this exact failure shape: locally sensible,
individually tested, invisible to git, broken the moment it actually runs.

## The rule, until something better replaces it

**Before pushing a change to `run_desk.py`, `golddesk/providers.py`, or
`golddesk/service.py`: `git pull` first, read the diff of what came in, and
update this file's "Recent claims" section below with what you're about to
touch.** If another session's entry says it's mid-edit on the same file,
either wait, or pick a different file to work in and come back.

If you resolve a conflict with `git checkout --theirs` or `--ours`: **read
what you are about to discard first.** That command threw away a working
`--effort` flag and its `provider_effort` plumbing once already — not broken
code, a finished feature, gone because nobody read the diff before choosing a
side. If a real feature is on the losing side, hand-merge it in rather than
letting the flag decide for you.

## Recent claims (most recent first — add yours, do not delete others' unless resolved)

- **2026-08-20, this session** (branch `claude/aurum-check-kqwpy6`): touched
  `run_desk.py`, `golddesk/providers.py`, `golddesk/service.py` to add the
  `--effort` reasoning-effort flag (threaded through both `anthropic:` and
  `claudecode:` providers, confirmed against a real `claude --help`), the
  `provider/vision match` preflight check, and three merge-collision fixes.
  Also added `golddesk/gold_trend.py` (ported trend detector, wired into
  `MarketBrief`/`build_brief`) and `golddesk/quant_findings.py` (the
  absorption channel's first real entries). Pushed as of this note.

- **Unknown session, branch `claude/aurum-tier2-brain`**: was building a
  `--effort` flag and `provider_effort` plumbing independently (visible in the
  operator's local `my-local-changes.patch`, discarded during a conflict
  resolution before it was pushed). If this branch still exists and gets
  merged, **its `--effort` work will collide with this session's** — same
  flag name, likely same shape. Whoever merges second: check `git log` for
  which one actually reached `origin/claude/aurum-check-kqwpy6` first (this
  session's effort flag is live there as of this commit) and do not
  re-add a duplicate.

## If you are the operator, not a session

Tell whichever session you're talking to which branch the *other* one is
using, if you know it. Neither session can see the other's branch name
without being told — `ListAgents` returns nothing, there is no cross-session
visibility in this environment. A one-line "the other one is on
`claude/aurum-tier2-brain`" is the cheapest thing you can do to stop the next
collision before it happens.
