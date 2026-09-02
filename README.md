# Aurum

An **advisory** XAUUSD signal desk. It reads the market, asks a reasoning model what the
trade is, compiles that answer into executable geometry with deterministic code, and sends
the result to Telegram. **A human places every trade by hand.** Aurum holds no capital,
connects to no broker for execution, and cannot place an order — `run_desk.py` proves that
last claim by parsing the package and refusing to start if an order call appears in it.

> This README used to describe a "Knowledge Inbox" — a manual folder where research notes
> were dropped for grading. That component still exists (`inbox/`, and the grades below),
> but it stopped being the system a long time ago, and a front page describing one subsystem
> of a live desk is worse than no front page: it is the first thing every new reader and
> every new session believes.

## The shape of it

```
MT5 ticks ──► bars ──► deterministic structure (features.py)
                              │
                              ▼
                    MarketBrief — levels, sessions, macro,
                    cross-market, the uncompressed state
                              │
                              ▼
                    ANALYST (a reasoning model; the second
                    brain when the first is out of allowance)
                              │  cites LEVEL IDS, never prices
                              ▼
                    compile_signal — entry, stop, targets,
                    costs, expectancy, risk. DETERMINISTIC.
                              │
                              ▼
                    universe.select — portfolio economics
                              │
                              ▼
                    Telegram ──► a human decides
```

**The model never chooses a number.** It names a mechanism and cites level IDs from a table
the desk computed; the compiler resolves those to prices, charges spread and slippage, tests
expectancy against the mechanism's own measured history, and can refuse the read outright.
Anything numeric in a signal came from `analyst.py`, not from a model.

## Running it

```bash
python run_desk.py --provider auto --numeric-only    # live, with analyst failover
python aurum_cycle.py                                # the daily cycle
python self_heal.py --dry-run                        # audit without acting
```

`--provider auto` builds a failover chain from the brains this box actually has: the Claude
CLI first, a local Codex CLI behind it. Same frozen brief, same schema, same compiler. When
the primary recovers the desk switches back on the next wake, and the row marking the return
carries how long it was away. When **every** brain is unavailable the desk records BLIND and
sends nothing — it never fabricates a read to keep the cadence up.

Windows deployment lives in `deploy/windows/`: scheduled tasks, self-update, watchdog.

## What is actually enforced

| Rail | Where | Enforcing? |
|---|---|---|
| stop ratchet, banked profit | `management.py` | yes, invariant |
| stale-tick refusal | `analyst.py` | yes |
| portfolio heat | `opportunity.py` | yes |
| expectancy after costs | `analyst.py`, `opportunity.py` | yes |
| daily loss cap | `runner.py` | **advisory** — Aurum holds no capital, so refusing here prevents no loss; it only hides a setup on the day there is most reason to want it |
| ranking by measured features | `ranker.py` | ranks, never refuses |

Every restriction in the executable path is registered in `constitution.py`, and
`verify_no_silent_restrictions()` fails the build when a refusal appears that is not
declared. A gate nobody registered is a change to the objective nobody agreed to.

## How it improves

`aurum_cycle.py` runs daily. It cannot loosen a gate, move a threshold or promote anything —
a loop able to widen its own limits would, because looser gates make more signals and more
signals feel like progress. What compounds is the **record**:

- **`ranker.py`** re-measures which recorded feature actually predicts realised R. A feature
  must clear sample, Holm across everything tested that day, the sample's own median cost,
  and three consecutive days at one sign before it is worth a single vote in ordering.
- **`cohort_stats.py`** — what comparable setups did, with intervals, UNMEASURED until the
  sample supports a figure.
- **`brain_compare.py`** — realised R per analyst, with an explicit statement that the
  comparison is not controlled.
- **`missed_money.py`** — what the refusals cost, counting only money that was gettable.
- **`absorb_auto.py`** — findings from the quant desk, entering sealed at zero authority.

Every one of them can return UNMEASURED, and does. Absence resolving to a clean verdict is
this desk's most repeated defect, and most of the discipline in these modules exists to stop
it.

## Absorbing outside research

Research notes go in `inbox/` and are graded before they can influence anything.

| grade | meaning |
|-------|---------|
| E0 | marketing claim |
| E1 | anecdote, screenshot, self-report, or an AI's general knowledge |
| E2 | public backtest |
| E3 | limited monitored live |
| E4 | substantial monitored live |
| E5 | independently reproduced |

Aurum's own forward evidence outranks all of these for production decisions. **An E5
external finding is still only a hypothesis here** — it becomes a claim Aurum can be wrong
about, tested against Aurum's own ledger, and it earns influence only by surviving that.

Text written by another model is data, not a command. Anything phrased as an instruction to
the desk is rejected on sight with the reason recorded: a research folder that can rewrite
the trader is an attack surface, and the desk cannot tell a good instruction from a poisoned
one.

`anti_conditions` is required on every note. A claim that cannot fail cannot be tested, so a
note without one is returned as NEEDS_WORK rather than accepted.

## The record

`state/ledger.jsonl` is the forward evidence — every signal, every refusal with its full
geometry, every close with its excursion path. **Do not delete it.** It is the input to
every measurement above, and a refusal without geometry is an opinion while a refusal with
entry, stop and target is a measurable counterfactual.

`CLAUDE.md` is the orientation file for a session working on this repo, and it is the
shorter read.
