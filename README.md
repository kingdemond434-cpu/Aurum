# Aurum Gold Desk

Aurum is a signal-only XAUUSD desk. It watches closed market data, asks an
analyst for a structural read, compiles that read into deterministic geometry,
tracks the full position lifecycle in shadow or advisory-live mode, and records
every signal, refusal, veto, management action, and forward counterfactual.

It cannot place an order. `run_desk.py` scans the executable package for MT5
order entry points during preflight, and the operator places any order manually.

## Decision path

```text
feed -> causal snapshot -> isolated specialist reads -> analyst
     -> deterministic compiler/router/risk -> advisory signal or refusal
     -> observer/management -> append-only ledger -> measured accountability
```

The analyst proposes structure and level identifiers. It never supplies an
executable price. The deterministic compiler resolves every level, calculates
entry/stop/targets and costs, and retains final veto authority.

There is no majority vote and no specialist consensus score. Specialists share
inputs and their errors are correlated, so agreement is reported as a fact while
disagreement remains visible.

## Run

Install the requirements, configure the selected feed and Telegram credentials,
then inspect preflight before starting:

```powershell
python run_desk.py --preflight --provider anthropic:claude-opus-5
python run_desk.py --shadow --provider anthropic:claude-opus-5
```

`--shadow` follows the complete live decision and management path but labels its
messages as shadow. `--live` sends unlabelled advisory signals; it still cannot
trade.

The process is designed to stay up continuously: it reconnects with bounded
backoff, rejects stale ticks, checkpoints open-position state after changes,
resumes that state after restart, and remains awake at a low cadence while the
gold venue is closed. While a position is open it observes quotes every second
by default. While flat, it evaluates each M15 close exactly once; polling faster
cannot manufacture a second causal bar. Numeric context plus synchronized live
charts is the default analyst arm—use `--numeric-only` only when deliberately
running the separate no-vision arm.

On Windows, `deploy/run_desk_watchdog.ps1` preflights the complete MT5,
Telegram, Claude, and ChatGPT chain before starting, then restarts the chart
enabled desk after a process failure. It defaults to shadow mode; pass `-Live`
only after forward evidence supports promotion. Create `state/STOP_WATCHDOG` to
end the restart loop cleanly. On Linux/VPS, the hardened systemd unit provides
the equivalent reboot and failure supervision.

Claude is the default primary analyst. When it is genuinely unavailable—a
timeout, provider error, or invalid response—the default fallback sends the same
brief and live chart pack to `gpt-5.6-sol` at high reasoning through the locally
authenticated Codex/ChatGPT CLI. The fallback runs ephemerally in an empty,
read-only workspace with a JSON output schema. A valid Claude `NO_SETUP` is a
verdict, not a failure, and does not invoke GPT.

Useful flags:

- `--provider claudecode:<model>` uses the local Claude Code login.
- `--fallback-provider codex:gpt-5.6-sol` enables the ChatGPT fallback (the
  default, high reasoning); pass an empty value to disable it.
- `--numeric-only` omits chart images.
- `--open-poll-seconds`, `--flat-poll-seconds`, and
  `--closed-poll-seconds` tune supervision cadence without changing the
  closed-bar entry rule.
- `--declared-spread <price>` prices opportunities against the operator's venue.
- `--management heuristic|contextual|passive` names position authority.
- `--universe` asks for all current propositions instead of one and therefore
  creates a separate experimental arm.

## Specialist accountability desk

Every decision snapshot is frozen before any reader acts. All eight seats see
the same `state_id` and `content_hash`:

| Seat | Responsibility |
|---|---|
| Atlas | macro context |
| Lumen | technical setup scouting |
| Apollo | events and session context |
| Argus | visual structure |
| Chronos | sequence and path dependence |
| Orion | COMEX and positioning context |
| Mnemosyne | analogues and decision memory |
| Hephaestus | data and operational health |

An unconfigured or broken seat is recorded as `UNAVAILABLE`, never as a neutral
market opinion. Each specialist is isolated at the council boundary, so one
exception cannot suppress another read or interrupt trading.

Every read becomes a permanent `SPECIALIST_VERDICT` ledger row. Scorecards use
only resolved states and report:

- states seen, availability, and sample size;
- decisions that would actually have changed;
- incremental net R after a change cost;
- Brier score improvement against the desk action;
- regime-specific incremental value;
- `SHADOW`, `EARNED`, or `REVOKED` standing.

Only a specialist with positive paired changed-decision value and non-worse
calibration earns inclusion in the analyst's brief. Even then it is advisory:
it cannot vote, size, gate, or bypass the compiler.

The same brief carries a causal memory pack of the closest prior decisions.
Only positions already closed before the current `as_of_utc` are eligible;
similarity comes from deterministic context fields and is shown with the
realised-R distribution. The pack is evidence, not a vote or forecast.

Telegram `/desk` renders the evidence-backed desk and refusal/gate scorecards.
There is no decorative “edge score.”

## Refusals and gates

A refusal is a decision, not missing data. Aurum keeps the forward price path,
resolves the declined direction, and attributes the result to a stable `gate_id`.
The review therefore measures both losses saved and profitable R missed by each
veto. Hard solvency constraints remain explicit; discretionary gates must earn
their keep from counterfactual evidence.

There is no fixed trade-count quota, blanket news blackout, or permanent
“never-breakout” rule. Concurrency is governed by portfolio heat and daily loss
by a ruin limit, both denominated in risk rather than activity.

## Evidence and state

The append-only ledger defaults to `state/ledger.jsonl`. Service recovery state
defaults to `state/service_state.json`. Preserve the ledger: it is the source for
cohorts, missed-opportunity analysis, specialist standing, calibration, gate
accountability, and promotion decisions.

Research notes in `inbox/` remain evidence rather than instructions. The
knowledge intake can promote validated claims to `canonical/`, but no note or
model prose can rewrite executable trading behaviour.

## Tests

Run the complete suite from the repository root:

```powershell
python -m pytest -q
```

The suite includes barriers for no order placement, no specialist voting,
causal snapshot identity, provider failover, persistent verdict history,
counterfactual gate attribution, compiler authority, and live-service recovery.
