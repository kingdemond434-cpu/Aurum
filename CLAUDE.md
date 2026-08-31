# Aurum — orientation for a fresh context

**Nothing loads this for you except this file.** A session after compaction starts blind to every
standing fact below. This file exists because that gap kept costing real things: facts the operator
had already stated were re-derived WRONG hours later, and they had to say them twice.

Keep it SHORT. It sits in every context window. It is an INDEX and a list of things already settled,
never a copy of anything.

## What Aurum IS — get this right before anything else

**An ADVISORY desk.** It reads XAUUSD and sends SIGNALS to Telegram. The operator places the trades
by hand in MT5. Aurum holds **no capital, has no gateway, arms nothing, and has no ruin rail.**

The armed `$500` gateway, the deadman switch and "arming is the principal's act" all belong to the
**quant** desk (`C:\opt\quant`), a DIFFERENT system that happens to share the box. Never attach
quant's capital vocabulary to Aurum — it was done once and it reads as not knowing what this desk is.

## The box

| Thing | Where |
|---|---|
| Contabo Windows VPS, user | `Administrator` (autologon already configured — do not ask again) |
| Aurum | `C:\Aurum` |
| quant | `C:\opt\quant` — **NOT `C:\quant`**, which does not exist and has been sent twice |
| branch | `claude/aurum-check-kqwpy6` |

## Settled. Do not re-litigate, do not re-ask.

- **The CLI runs on a MAX SUBSCRIPTION.** Never propose an API key, never suggest
  `ANTHROPIC_API_KEY`, never recommend the metered `anthropic:` provider on cost grounds. A CLI
  banner reading "API Usage Billing" was misread as org metering on 2026-08-28; the operator
  corrected it. `billing_basis()` already returns `assumed_subscription` -> `billed False` ->
  `cost_usd 0.0`, which is correct.
- **MAXIMUM FREQUENCY. ANTI-TIMIDITY. STANDING ORDER.** Maximum net-return capture, maximum
  exploitation, maximum quant absorption. Never restricted to certain setup types. **New information
  — a loss, a bad signal, a fresh feedback — must RAISE frequency and net size, never add a gate,
  never add conservatism, never "reconsider risk".** Smarter signals at the same or higher frequency
  is the only acceptable direction. A demotion that fires in shadow is a bug, not caution.
- **Partials are DYNAMIC** (`golddesk/partial_policy.py`): banked against live maturity, volatility,
  HTF alignment and TP2 headroom. Never hardcode a fraction.
- **The operator should not be running diagnostic commands.** If a question about the live desk
  cannot be answered from the repo, that is a defect in the publishing, not a reason to ask them.

## Reading the live desk WITHOUT asking anybody

`self_heal.py` runs every 15 min and publishes a bounded state artifact to its own ref:

```
git fetch origin aurum-state
git show origin/aurum-state:desk_state.json
```

Audit verdicts with their detail sentences, per-kind decision counts, timestamps, and the CLI's own
words when a read failed. Built by plumbing on a dedicated branch — it never touches the index, HEAD,
the working tree or the code branch, because `Update-AurumDesk.ps1` advances with `merge --ff-only`
and skips on a dirty tree. Putting state on the code branch disables auto-update; that was tried.

## The ONE thing only a human can do

The Claude CLI's OAuth login. When it expires the desk books `BLIND` on every bar and the CLI says
`Failed to authenticate: OAuth session expired and could not be refreshed` — inside an envelope that
reports `subtype: "success"`, `api_error_status: null` and `stop_reason: "stop_sequence"`. The desk
detects this on the FIRST failure, skips the flag ladder, and alarms with the fix: run `claude` once
interactively on the box as the task's user. No retry, restart or watchdog can clear it.

Everything else self-corrects. 24 checks across five axes, allowlisted remedies in `remediate.py`.

## Traps this desk has actually fallen into

- **Windows scheduler status codes are NOT exit codes.** `267009` (running), `267011` (not yet run),
  `267012`, `267014` are benign. Reading one as a failure produced a false "watchdog down".
- **Never `git stash`.** It swallowed three source files here on 2026-08-28. Same law as quant's
  R0423. Stage explicit paths; never `git commit -a`.
- **UNMEASURED is a real answer.** Rendering "no data" as healthy has shipped twice — `task_health`
  reported "every watchdog is running" with all six unreadable.
- **Truncating an error at 300–500 chars cuts off exactly the field that names the cause.** Both
  `providers.py` and `live.py` carry 2000 for this reason; one of them had to learn it twice.
- **An inferred cause that fits the arithmetic is not a diagnosis.** A 9,098 > 8,191 argv overflow
  explained the symptom perfectly and was not the failure; `prompt_chars=3418` settled it. Wait for
  the measurement.

## Before any push

```
python3 -m pytest --co -q     # 8s. Collection is a SEPARATE gate — run it first
python3 -m pytest -q          # ~4 min, 1235 tests
ruff check golddesk/ *.py
```

`live.py` carries 5 pre-existing F401s; they are not yours.

## Current state

**Not written here on purpose** — it would be wrong the day after it is typed. Read
`origin/aurum-state:desk_state.json`, which is at most 15 minutes old and says so in its own
`generated_utc`.
