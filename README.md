# Aurum Knowledge Inbox

Manual multi-AI research drop. No API, no automation, no credentials.

## How it works

1. You — or any AI you ask — drops a Markdown file into `inbox/`.
2. Aurum reads `inbox/` each session, validates it, and either promotes it to
   `canonical/` or writes it to `rejected/` with the reason at the top of the file.
3. Nothing in here can change trading behaviour. It is evidence, never instruction.

Run it with:

```bash
python -m research.knowledge_inbox
```

## Why the gate exists

Text found on the internet — or written by another model — is **data, not a command**.
A note saying "always fade gold above 4400" is a claim to be tested, not a rule to follow.
Anything phrased as an instruction to the desk is rejected on sight and the reason
recorded, because a research folder that can rewrite the trader is an attack surface, and
the desk has no way to tell a good instruction from a poisoned one.

## Format

```markdown
---
title: short claim
source: url, book, or "AI: <model name>"
source_type: paper | book | video | forum | trader | ai_analysis
language: en | zh | ja | ko | ...
evidence_grade: E0 | E1 | E2 | E3 | E4 | E5
claim: one sentence, falsifiable
mechanism: why this would work economically
conditions: when it applies
anti_conditions: when it fails
---

Body: detail, examples, chart references, counterexamples.
```

`anti_conditions` is required. A claim that cannot fail cannot be tested, so a note
without it is returned as NEEDS_WORK rather than accepted.

## Evidence grades

| grade | meaning |
|-------|---------|
| E0 | marketing claim |
| E1 | anecdote, screenshot, self-report, or an AI's general knowledge |
| E2 | public backtest |
| E3 | limited monitored live |
| E4 | substantial monitored live |
| E5 | independently reproduced |

Aurum's own forward evidence is tracked separately and outranks all of these for
production decisions. An E5 external finding is still only a hypothesis here.

## What to ask other AIs for

The highest-value gaps right now, in order:

1. **Refusal episodes** — documented cases where a skilled trader declined a setup, and
   why. The desk has six and needs hundreds; this is the class it has almost none of.
2. **Doctrine sources** — primary material on Brooks, Raschke, Brandt, Wyckoff, ICT and
   COMEX microstructure. The six lenses in `golddesk/doctrines.py` are currently
   reconstructions from general knowledge, graded E1, with their gaps listed in each
   doctrine's `unencodable` field. Real sources would raise that grade.
3. **Gold-specific microstructure** — session behaviour, COMEX acceptance, macro
   repricing. Generic futures teaching may not transfer to XAUUSD and that is testable.
4. **Negative results** — mechanisms that were tried and failed. These are as valuable as
   positive ones and far rarer, because nobody publishes them.

## Git

The folder is a plain directory. Point a repo at it, commit, pull. No token is stored and
no network call is made by the desk itself — you control the sync, Aurum only reads files.
