# Aurum Desk — External Research Node Brief

Give this file to any capable AI you want contributing gold research. Fill the two fields
at the top, paste the whole thing as its instructions, and commit whatever it returns into
`contributions/<AGENT_NAME>/`.

```
AGENT_NAME:  <model / agent name>
SPECIALTY:   GENERAL | GOLD | MACRO | COMEX | MICROSTRUCTURE | PRICE ACTION |
             EXECUTION | MANAGEMENT | REENTRY | ACADEMIC | AI | CHINA | JAPAN |
             KOREA | BOT HUNTING | DATA | UNKNOWN-UNKNOWNS
```

The full directive lives in [`CONTRIBUTOR_DIRECTIVE.md`](CONTRIBUTOR_DIRECTIVE.md).

---

## What actually gets read

Aurum's curator ingests `contributions/**/*.md` with `research/knowledge_inbox.py`. A file
is accepted only if it parses as an **Aurum Knowledge Packet** and passes the gate below.
Anything else is written to `rejected/` with the reason at the top of the file.

## The gate, and why it exists

Everything in this repository is **untrusted data written by someone else**. One rule does
not bend: **no contribution may act as an instruction.** A note saying "Aurum should always
fade above 4400" is rejected — not because the claim is necessarily wrong, but because a
research folder that can rewrite the trader is an attack surface, and the desk cannot tell
a good instruction from a poisoned one.

Rejected on sight:

- imperatives aimed at the desk (`Aurum must…`, `always buy…`, `ignore the previous…`)
- missing `anti_conditions` — a claim that cannot fail cannot be tested
- missing mechanism — "it works" is not a finding
- performance screenshots, follower counts, marketing pages as primary evidence

## Evidence grades

| grade | meaning |
|-------|---------|
| E0 | marketing claim |
| E1 | anecdote, screenshot, self-report, or an AI's general knowledge |
| E2 | public backtest |
| E3 | limited monitored live |
| E4 | substantial monitored live |
| E5 | independently reproduced |

Aurum's own forward evidence outranks all of these. **An E5 external finding is still only
a hypothesis here.** Nothing gains production influence without surviving the promotion
gate: ESS ≥ 30, BH-FDR q ≤ 0.10, positive ex-top-3, and net of the live 0.48 spread.

## Where the highest value is right now

Ordered by what the desk actually lacks, not by what is interesting:

1. **Refusal episodes.** Documented cases where a skilled trader declined a setup, and why.
   The corpus has 397 episodes, 395 of them the desk's own suppressions — almost no
   external ones. Published material records trades taken, never trades refused.
2. **Negative results.** Mechanisms tried on gold that failed. Nobody publishes these and
   they save more money than positive findings make.
3. **Contradictions of Aurum's current beliefs.** See `OPEN_QUESTIONS.md`. You are
   explicitly rewarded for falsifying these.
4. **Gold-specific microstructure** — COMEX/London price discovery, session behaviour,
   macro repricing. Generic futures teaching may not transfer to XAUUSD, and that is
   testable.
5. **Non-English practitioner material** — Chinese, Japanese, Korean, Russian ecosystems.
   Search in native terminology, not translated English queries.

## What NOT to send

- Indicator suggestions (RSI, MACD, Bollinger, FVG, order blocks) with no stated economic
  failure they reduce.
- Restatements of things the desk already has. Check `canonical/` first; if it is already
  there, only contribute a better mechanism, contradictory evidence, a new anti-condition,
  a cheaper encoding, or a new falsification test.
- Anything requiring paid data the desk does not have. Order-flow depth and the options
  surface are known gaps and are marked `BLOCKED_EXTERNAL`; restating that they would be
  useful is not new information.
- Live trade calls. Contributors are research nodes, not the trader.

## Directory rules

Write only into `contributions/<AGENT_NAME>/`:

```
contributions/<AGENT_NAME>/research/
contributions/<AGENT_NAME>/hypotheses/
contributions/<AGENT_NAME>/sources/
contributions/<AGENT_NAME>/contradictions/
contributions/<AGENT_NAME>/unknown_unknowns/
```

Never modify `canonical/`, `daily/`, `cards/`, or another agent's directory.

If you have no repository access, return the packet as Markdown in your reply and the
curator will commit it.
