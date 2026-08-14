# Aurum V2 — audit of the packaged artifact against the executable live path

Audited: the shipped `golddesk/` package, by AST over executable code and by
running the real `LiveDesk`. Not by reading module docstrings, and not by
grepping — a grep matches a comment explaining that something was removed.

The rule applied throughout: **a module that nothing imports is a plan, not a
capability.**

---

## Gap matrix

| # | Constitutional article | Gap found in the executable path | Evidence |
|---|---|---|---|
| G1 | §1 AI is the trading brain | `LiveDesk._choose()` was a handcrafted hierarchy and the **only** management decision-maker in production. `policies.py` — containing `PassiveChooser`, `HeuristicChooser`, `ContextualChooser` — was imported by **nobody**. | import graph: `policies imported by: NOBODY` |
| G2 | §4 no proxy drift | `live.py` imported `reentry.may_reenter`, which hardcoded a 20-minute cool-off and refused any re-entry whose prior attempt stopped below +0.5R MFE. Neither was declared or justified. The versioned `ReentryPolicy` (cool-off 0) was dead code. | AST: `reentry.py:34 timedelta(minutes=20)` |
| G3 | §3 continuous observation | `TradeObserver` was never instantiated anywhere in `live.py`. Management ran only at bar close. | no `TradeObserver` reference in `live.py` |
| G4 | §5/§13 intrabar resolution | `_manage()` checked `stop_hit` before `tp_hit` — the pessimistic assumption — and labelled the result identically to an observed fill. `resolve_intrabar()` was never called on the live path. | `live.py:245` (pre-change) |
| G5 | §14 anti-drift auditor | `constitution.py` existed, was imported by nobody, had zero call sites, and was **not in the packaged zip**. | import graph + zip manifest |
| G6 | §10/§11 evidence standards | Router promotion to ENFORCING (a permanent veto) was decided by splitting the *same mined sample* into halves and checking they agreed. Promotions mutated an in-memory list and evaporated on restart. | `adapt._evidence_for()`, `_apply_rule()` |
| G7 | §11 adaptation must change behaviour | `Adapter.run()` appended `Change("policy", "active", …)` to an audit trail and **never changed any policy**. Nothing read those rows. | `adapt.py:149` (pre-change) |
| G8 | §4 cold-start prior | `fallback_min_rr=1.5` blocked unknown-mechanism trades with no measurement of what it forwent. | `opportunity.ev_gate` |
| G9 | §2 multimodal | `charts: bool = False` meant a run intending numeric+visual Claude could silently execute numeric-only and be filed under the wrong arm. | `live.py:84` (pre-change) |

### Two defects found that were not in the brief, and are worse than the ones that were

| # | Defect | Consequence |
|---|---|---|
| **D1** | `compile_signal()` bound `verdict` to the router verdict, then **rebound the same name** to the expectancy verdict. The final `router_advisories=verdict.advisories` raised `AttributeError` on every path that reached it. | **The compiler could never emit a signal.** Every entry that got as far as the end of the compiler crashed. The production entry path was non-functional in the packaged artifact. |
| **D2** | `build_cohorts`, hypothesis discovery and hypothesis confirmation all read `kind=="SIGNAL"` rows carrying `decision["realised_r"]`. **No such row is ever written** — entries are journalled before the outcome exists, and the outcome lands later on a separate close row. | **The entire learning loop aggregated an empty set.** Cohorts were permanently empty, the expectancy gate fell back to its cold-start prior forever, and no hypothesis could ever accrue a single observation. Self-improvement was structurally impossible, not merely slow. |

Both are now fixed, with the mechanism that hid them noted in the code.

---

## What changed

**`live.py`** — `TradeObserver` instantiated per position and driven by a new
`on_tick()`; exits checked at tick ordering before anything else; wakes routed
into a shared `_management_step()` used by both the tick and bar paths;
`resolve_intrabar()` called whenever a finer series is supplied; `Resolution`
enum stamped on every close so observed and assumed fills can never aggregate
silently; `Vision` enum replacing `charts: bool`, **verified** at the call site
(declared multimodal with no rendered chart is now a refusal, not a downgrade);
management resolved from `PolicyState`; every non-active arm evaluated on the
identical option set and recorded, so later comparison is paired.

**`policy_state.py`** (new) — durable, atomically written, versioned binding of
decision slots to policy versions, each carrying the evidence that justified it
and an expiry. Reads check expiry, so lapsed authority stops applying even if no
adaptation cycle has run. One-step revert per slot.

**`hypothesis.py`** (new) — `DISCOVERED → SEALED → CONFIRMING → ENFORCING →
LAPSED/REJECTED`. A sealed hypothesis records the instant beyond which it had
seen nothing; only outcomes after that instant may confirm it. Content-hashed,
so re-tuning a rule forfeits the confirmation the old version earned. Rejected
hypotheses are kept forever because they are the denominator that keeps the
multiple-testing correction honest.

**`adapt.py`** — the half-split promotion is gone; router authority now flows
through the sealed book. Policy selection writes through `PolicyState`, so the
cycle changes what runs rather than what is logged.

**`providers.py`** — `choose_option()` on the provider contract, with a schema
built per call from the live option list, so the model physically cannot return
an illegal id and has no field in which to express a price. The default
implementation raises rather than guessing, so "no contextual management" is
distinguishable from "contextual management chose HOLD".

**`reentry.py`** — `may_reenter()` **deleted**. It stayed importable long after
`LiveDesk` stopped calling it, and an importable gate with a hardcoded cool-off
is one careless import away from being live again.

**`opportunity.py`** — `resolved_outcomes()`, one reader for the whole desk;
the cold-start prior renamed, documented as a response to ignorance rather than
a quality standard, and routed through `constitution.is_enforcing()` so it stops
blocking when measurement says it costs more than it saves.

---

## Evidence, separated by kind

### 1. Engineering correctness — demonstrated

`acceptance.py` drives the real `LiveDesk` and prints **30 numbered arrows**,
each an assertion that fails the run if the seam is not connected. Highlights:

```
[ 7] MULTIMODAL VERIFIED       vision=NUMERIC_PLUS_CHARTS and 1 image block(s)
                               actually in the request payload
[11] on_tick -> observer       34 ticks consumed, MFE=+3.00R path_points=34
[14] wake -> mgmt              4/4 management steps originated from an observer
                               wake, not a bar close
[16] ContextualChooser         4 step(s) decided by the model-in-the-loop arm
[17] legality guaranteed       every chosen id came from the enumerated set: True
[18] intrabar-resolved EXIT    exits=1 tick_resolved=1 assumed=0
[24] learning loop sees        resolved_outcomes=1 cohorts=1
                               (was structurally 0 before this revision)
[27] POLICY ACTUALLY BOUND     heuristic-v1 -> passive
[28] DURABLE ACROSS RESTART    fresh PolicyState from disk reports 'passive'
[29] REVERSIBLE                1 change undone
```

Constitutional build check: **PASS**, 0 undeclared refusal sites, 18 registered
restrictions (7 hard-risk exempt, 11 that must earn their keep).

### 2. Historical out-of-sample evidence — **none exists**

No walk-forward result is reported, because none can be honestly produced here.
What *was* measured on real broker XAUUSD D1 (2,169 bars, 2018-03-18 →
2026-08-11), via `ambiguity.py`:

- 113 signals compiled and resolved through the real compiler
- exit ordering **observed on 100%** of them; 0% ambiguous
- error bar from ordering alone: **0.00R** — wide targets on a daily series
  never span both stop and target in one bar

That result contradicted the conclusion originally drafted for that script,
which asserted a large ambiguity band. The script now derives its conclusion
from its own numbers.

It does **not** make D1 adequate. Ordering is the smaller intrabar problem. The
larger one is visible in the acceptance trace: a position reached **+3.00R MFE
and realised +0.00R**, entirely inside what a daily bar shows as one price. Any
management result computed on D1 is uninformative.

### 3. Live-forward evidence — **none exists**

No shadow run has occurred.

---

## Remaining blockers, verified rather than assumed

| Blocker | Probe | Result |
|---|---|---|
| Real Claude analyst | `POST api.anthropic.com/v1/messages` | `401 — x-api-key header is required`; `ANTHROPIC_API_KEY` not set. Host is reachable; only the credential is missing. |
| MT5 tick/M1 history | `import MetaTrader5` | `ModuleNotFoundError` |
| Any third-party market data | CONNECT to dukascopy / stooq / yahoo / mt5tickdata | `403 CONNECT — policy denial` on all four |
| Telegram delivery | CONNECT `api.telegram.org` | `403 CONNECT — policy denial` |

Consequently the analyst arrow is exercised through `AnthropicAnalyst.read()`
with the HTTP boundary replaced by a capturing double, and the tick path is
driven by a synthetic sequence. Both are labelled as such in the script header
and in every claim above. **Neither produces evidence about Claude's trading
ability or about edge.**

---

## Walk-forward harness (added after the seam work)

`golddesk/backtest.py` + `run_backtest.py` replay the **real** `LiveDesk` — same
compiler, router, risk gate, observer, management engine and ledger. There is no
simplified Aurum anywhere in it.

- **Splits** are chronological and never shuffled: train `2019-03-20 → 2022-11-27`,
  calibration `→ 2024-05-21`, OOS `→ 2026-08-11`.
- **Preregistration** frozen with a content hash before the run; tamper check **PASS**.
- **Leakage** is proved, not asserted: `leakage_report()` re-derives each decision
  from a truncated history (`bars[:i+1]`) and demands it match the decision taken
  with the full array resident. **PASS**, 41 states checked.
- **Ablation ladder** A→H. Arms whose capability is unavailable are **omitted, not
  downgraded** — an arm labelled `+charts` that ran without charts would corrupt
  every comparison above it.
- **Fold agreement** is reported alongside the aggregate, because a total driven
  by one fold is a different claim from one that holds across all of them.

### Result actually obtained

Only **arm A** could run — no API key, so B–H were omitted. Arm A is the
deterministic baseline and contains no model.

| arm | acted | select | net R | R/trade | win | ESS | maxDD | capture |
|-----|-------|--------|-------|---------|-----|-----|-------|---------|
| A | 20 | 1.3% | **−7.8** | −0.389 | 10% | 11 | −11.0 | −1% |

Fold agreement: **1/3 folds positive** (`−6.00R/6`, `+5.21R/7`, `−7.00R/7`).

The floor is negative and unstable. That is a useful thing to know and it is the
only performance number in this repository. It says nothing about Claude.

Resolution provenance: `M1_OBSERVED=19 (95%)`, `M15_PESSIMISTIC_UNCERTAIN=1 (5%)`
— 5% of fills are a modelling choice rather than a measurement.

## Anti-drift auditor (§14)

`golddesk/drift_audit.py` runs two checks on every meaningful change:

1. **Behavioural diff on frozen states** — a corpus of 367 real decision states,
   frozen once and never regenerated (regenerating it alongside a change would
   compare two different questions). Reports newly-refused vs newly-tradeable
   separately, because a change that only ever removes opportunity is drifting
   toward conservatism no matter how principled each gate sounded.
2. **Undeclared threshold detection** — any numeric literal in a comparison
   inside a function that can refuse is a trading threshold whether or not it
   was meant as one.

On first run it found two: an HTTP status code in `notify.py` (protocol constant,
now narrowly exempted) and `0.6` in `review._same_claim` (a genuine magic number,
now hoisted to a named `CLAIM_SIMILARITY`). Current state: **0 undeclared
thresholds, 0 silent restrictions, 18 registered restrictions.**

## What must happen next, in order

1. Supply `ANTHROPIC_API_KEY` → the analyst arm becomes real.
2. Export MT5 M1/tick XAUUSD from the broker terminal → the observer,
   management engine and intrabar resolution become measurable.
3. Only then: walk-forward with sealed hypotheses and the ablation ladder.
4. Only then: live shadow.

Nothing in steps 3–4 is worth running before 1–2, because the questions they ask
cannot be answered by daily bars.
