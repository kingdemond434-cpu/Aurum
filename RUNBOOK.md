# Aurum operator runbook

Everything below needs **you** — an account, a credential, a machine, or a
decision. Nothing here can be done from inside a sandbox with no network and no
keys, which is why it is a document rather than more code.

Work top to bottom. Each step says what it is for, what "done" looks like, and
what to do when it fails.

**Time:** about 90 minutes end to end, most of it waiting for downloads.

---

## Before you start: what you are deploying

| | |
|---|---|
| **What it does** | Watches XAUUSD 24/5, sends you Telegram signals |
| **What it does NOT do** | Place orders. There is no broker order code anywhere, and preflight AST-scans for it and refuses to boot if any appears |
| **You execute** | Manually, from the Telegram message |
| **Entry judgement** | Claude |
| **Arithmetic, risk, cost** | Deterministic code — always, never the model |
| **Trade management** | A heuristic, **not** Claude (see step 8 — this is a decision you are making) |
| **Evidence so far** | −7.8R over 20 trades, one arm, backtest only. **Zero live-forward trades.** Deploy in `--shadow` |

---

## 1. The price feed — pick one

### Option A: zero setup (recommended to START)

Nothing to sign up for. Skip straight to step 2.

```
--feed yahoo --declared-spread 0.45
```

**What it costs you:** the endpoint publishes OHLC, not a two-sided quote, so
the bid/ask is SYNTHESISED as `mid ± half your declared spread`. Honest
arithmetic on a number you supply — but constant by construction, so it cannot
widen into a release or gap at the rollover. Every such tick is stamped
`synthetic`. It is also an unofficial endpoint that can rate-limit or change
without notice.

**Good enough to start collecting forward evidence, which is the point.**
Switch to B when you start caring about the tick path — profit-lock, trailing,
giveback, intrabar exits — because all of that is measured against a synthetic
spread here.

### Option B: OANDA practice (~10 min, real bid/ask)

The price feed. Practice account is correct — you are not trading through it.

1. Sign up: <https://www.oanda.com/> → **demo/practice** account
2. Log in → **Manage API Access**
3. Generate a token
4. Note your account id, format `101-004-XXXXXXX-001`

**Done when:** you have a token string and an account id.

> **Do not create a token with trade permissions.** Aurum has no code that
> places an order; a token that *could* is authority with nothing to use it and
> everything to lose from.

Verify (replace both values):

```bash
curl -s -H "Authorization: Bearer YOUR_TOKEN" \
  "https://api-fxpractice.oanda.com/v3/accounts/YOUR_ACCOUNT/pricing?instruments=XAU_USD" \
  | head -c 400
```

Expect JSON with `bids` and `asks`. `401` = bad token. `403` = wrong environment
(practice token against the live host or vice versa).

**Switching between A and B later is one flag.** Both implement the same
Mt5Client Protocol; nothing else in the desk changes.

---

## 2. Telegram bot and chat id

Where signals arrive.

1. Telegram → message **@BotFather** → `/newbot` → follow prompts → copy the token
2. **Send your new bot any message** (it cannot message you first)
3. Get your chat id:

```bash
curl -s "https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates" | head -c 600
```

Find `"chat":{"id":123456789`. That number is your chat id (negative for groups).

**Done when:** you have a bot token and a chat id.

**Empty `getUpdates`?** You skipped step 2 — send the bot a message first.

---

## 3. The VPS

Your existing Contabo Linux box is fine. So is the crypto-quant VPS if it has
~2GB free RAM and Python 3.11+.

```bash
ssh you@your-vps
python3 --version          # need 3.11 or newer
df -h /                    # need ~2GB free
```

**You do not need Windows and you do not need MetaTrader5.** The OANDA feed is
Linux-native and speaks the same interface. MT5 is only relevant for step 9,
which is optional.

---

## 4. Install

```bash
# on the VPS
git clone -b claude/aurum-check-kqwpy6 https://github.com/kingdemond434-cpu/Aurum.git
cd Aurum
sudo ./deploy/install.sh
```

This creates the `aurum` service account, `/opt/aurum`, a venv with pinned
dependencies, empty secret files, and installs + verifies both systemd units. It
**starts nothing** — deliberately.

**Done when** it prints the numbered next-steps block.

Also want the external-signal collector? `sudo WITH_CAPTURE=1 ./deploy/install.sh`

---

## 5. Fill in the secrets

```bash
sudo -u aurum nano /opt/aurum/.env
```

Set `ANTHROPIC_API_KEY`. If you chose OANDA, also `OANDA_TOKEN` and
`OANDA_ACCOUNT`; on `--feed yahoo` neither is needed. No quotes, no `export`, no
trailing comments — systemd reads the line literally.

Then the Telegram credentials, as **files** (preferred over env vars: env is
visible in `systemctl show`, in crash logs, and to anything that can read
`/proc`):

```bash
printf '%s' 'YOUR_BOT_TOKEN' | sudo -u aurum tee /opt/aurum/secrets/telegram_token >/dev/null
printf '%s' 'YOUR_CHAT_ID'   | sudo -u aurum tee /opt/aurum/secrets/telegram_chat_id >/dev/null
sudo chmod 600 /opt/aurum/secrets/*
```

`printf` not `echo` — `echo` appends a newline into the token.

### Your Anthropic API key

<https://console.anthropic.com/> → API Keys. Set a **monthly spend limit** while
you are there. Rough cost at default settings: **$3–8/day** with charts,
**$1–3/day** numeric-only.

> A key was pasted into a chat earlier in this project. **Treat it as burned —
> revoke it and issue a new one.** Anything pasted into a chat window should be
> assumed public.

---

## 6. Preflight

```bash
sudo -u aurum /opt/aurum/.venv/bin/python /opt/aurum/run_desk.py --preflight
```

Every line must say `[PASS]`. It will not start on a failure, on purpose — a
half-configured 24/5 process looks alive and produces nothing, which is worse
than refusing to boot.

| Failure | Fix |
|---|---|
| `ANTHROPIC_API_KEY NOT SET` | step 5; check for quotes in `.env` |
| `telegram missing or EMPTY` | step 5; `install.sh` creates those files empty |
| `OANDA` | re-run the curl from step 1 |
| `ORDER CALLS FOUND` | **stop and tell me.** Nothing should ever trip this |

---

## 7. Historical data (do this before the first launch)

The single biggest constraint on this project is data, not architecture.

**Do these three in order. The first two cost nothing and take seconds.**

```bash
cd /opt/aurum

# 1. Prove the FORMAT handling is right. No network needed.
sudo -u aurum .venv/bin/python fetch_dukascopy.py --offline-test

# 2. Prove you can reach the feed. One hour, ~2 seconds.
sudo -u aurum .venv/bin/python fetch_dukascopy.py --selftest

# 3. See what you are committing to BEFORE committing to it.
sudo -u aurum .venv/bin/python fetch_dukascopy.py \
    --from 2023-01-01 --to 2026-01-01 --dry-run
```

Then the real pull, **under `tmux`** so an SSH drop does not kill it:

```bash
tmux new -s fetch
sudo -u aurum .venv/bin/python fetch_dukascopy.py \
    --from 2023-01-01 --to 2026-01-01 --pause 0.05
# Ctrl-B then D to detach.  tmux attach -t fetch  to come back.
```

**Corrected timing — I under-estimated this earlier.** The dry-run gives the
real number: five years is ~31,000 hourly files, which is **10–26 hours**, not
1–3. Three years is roughly 6–15 hours and ~5GB. Start with three years; you can
always extend later because the fetch is resumable per month.

`--pause 0.05` adds ~15 minutes over three years and is good manners to a free
feed. Use it.

**It is genuinely resumable now.** It writes one parquet per month and skips
completed months without issuing a single request. If it dies you lose at most
the month in progress — just re-run the same command.

**Why it matters:** the arms comparison needs paired states in volume. Twenty
trades cannot separate anything, and no amount of architecture substitutes for
sample.

---

## 8. Tell the desk what YOUR broker charges

**Do not skip this. It changes every trade decision.**

The OANDA feed is where Aurum *looks*. Your broker is where you *execute*. They
charge different spreads, and retail gold brokers are usually **wider** than the
feed. Without this the desk prices every trade against a cost you will not pay —
in the direction that makes marginal trades look positive.

On a $6 stop: a $0.30 feed spread is 0.05R; a $0.60 broker spread is 0.10R. That
0.05R gap decides marginal trades, and marginal trades are most of them.

Find your broker's typical XAUUSD spread (MT5 Market Watch, or ask support for
the average — not the "from" number in their marketing), then:

```
--declared-spread 0.45
```

Add it to `ExecStart` in `/etc/systemd/system/aurum-desk.service`.

If you skip it the desk still runs, prints `COST basis: THE FEED — not your
execution venue` at boot, and stamps every signal with where the cost came from.
It will not pretend.

Later, once the tick archive has a few weeks in it, you can measure the real
per-session profile instead of declaring one number.

---

## 9. Decide who manages the trade

**This is a real decision and it is yours.** Claude forms the entry judgement.
Who runs the position after fill is a separate question:

| `--management` | Meaning |
|---|---|
| `heuristic` | Deterministic rules manage. **Recommended.** |
| `contextual` | Claude decides each management step |
| `passive` | Never intervenes; the floor every arm must beat |

**Start with `heuristic` and shadow the contextual arm.** Contextual has not
beaten the heuristic on paired states, and granting authority before it has is
exactly what the evidence standard exists to prevent. Shadowing records what
Claude *would* have done at every step, on the identical legal option set, so
the comparison accumulates without staking anything on it.

The shipped unit already does this. To include the contextual arm in shadowing —
it calls the API on every management step, so it costs real money — add
`--shadow-contextual` to `ExecStart`.

---

## 10. Optional: your broker's minimum stop distance

The shipped unit assumes `--min-stop 0.50`. If you have MT5 on any Windows
machine, get the real number:

```python
import MetaTrader5 as mt5
mt5.initialize()
i = mt5.symbol_info("XAUUSD")
print("min stop:", i.trade_stops_level * i.point)
print("freeze  :", i.trade_freeze_level * i.point)
```

Put the result in `ExecStart` in `/etc/systemd/system/aurum-desk.service`.

If you skip this, stop legality falls back to the through-the-market test only —
it may propose a stop your broker rejects. Not dangerous (nothing is placed
automatically), just occasionally annoying.

---

## 11. Launch, in shadow

Markets open **Sunday ~22:00 UTC**. Start before then; the desk detects the
venue is shut, backs off to a 60-second health poll, and resumes by itself.

```bash
sudo systemctl enable --now aurum-desk
journalctl -u aurum-desk -f
```

Expect at boot: the mode banner (who manages, which arm), `broker limits`,
`management authority: heuristic`, `notification sink: TelegramSink`.

Then send yourself a test:

```bash
curl -s -X POST "https://api.telegram.org/botYOUR_TOKEN/sendMessage" \
  -d "chat_id=YOUR_CHAT_ID" -d "text=aurum test"
```

**Every message will be tagged `[SHADOW]`.** That is correct. Do not remove
`--shadow` until you have weeks of forward evidence.

### The control channel

```bash
sudo systemctl enable --now aurum-bot
```

Then message your bot `/help`. It answers:

| command | what it tells you |
|---|---|
| `/status` | alive, halted, bars, ticks, reconnects, stale suspensions |
| `/positions` | the open position in the checkpoint |
| `/recent` | last 10 decisions |
| `/refusals` | last 10 refusals **and what each one cost** |
| `/pnl` | resolved R and hit rate — R, never currency |
| `/growth` | risk per trade and heat budget, solved from the ledger |
| `/why` | the reasoning behind the last decision |
| `/halt` / `/resume` | stand the desk down, and bring it back |

Three things worth knowing about it:

**It is a separate unit from the desk, deliberately.** A bot running inside
`aurum-desk` dies with it, so the one question you most want answered — *is the
desk alive?* — is exactly the one it could not answer.

**Only your chat id can command it.** Checked on every message, not just the
first. Anyone else who finds the bot gets silence; an error reply would confirm
there is something there worth attacking. If you ever change chat (new group,
new account), update `secrets/telegram_chat_id` or the bot will ignore you too.

**`/halt` stops the desk deciding; it closes nothing.** Aurum has never held a
position — whatever is open is open in your terminal, and stays open. The halt
is a file (`state/HALTED`), so it survives a restart and you can set or clear it
by hand during an incident:

```bash
sudo -u aurum touch /opt/aurum/state/HALTED     # same as /halt
sudo -u aurum rm    /opt/aurum/state/HALTED     # same as /resume
```

### Tick integrity

Every quote is checked before anything acts on it — crossed quotes, decimal
slips, absurd spreads, impossible jumps. A bad print that reached the tick path
would trip a stop that never happened and write a fabricated loss into the
ledger, which is worse than a trading error: nothing downstream can tell it
apart from a real one.

Rejections are logged and archived to `data/ticks/XAUUSD_rejects_*.csv.gz`. A
handful a day is normal. **A steadily rising reject rate means the feed has
developed a problem** — check it in the journal:

```bash
journalctl -u aurum-desk | grep "REJECTED TICK" | tail
```

Weekend gaps and news moves are explicitly NOT rejected — the jump test only
applies when the previous tick was seconds old, so a Sunday reopen or an NFP
spike passes through untouched.

### What normal looks like

- `alive: N ticks, M bars` every 15 minutes
- Long quiet stretches. **Expect roughly 0–3 signals a day.** Zero on a
  featureless day is a correct answer, not a fault
- `VENUE APPEARS CLOSED` on weekends — normal, not an error

### Useful commands

```bash
systemctl status aurum-desk
journalctl -u aurum-desk -f                    # follow
journalctl -u aurum-desk --since "1 hour ago" | grep -E "ENTRY|EXIT|ERROR"
sudo systemctl restart aurum-desk              # safe: state is checkpointed
wc -l /opt/aurum/state/ledger.jsonl            # decisions recorded
```

> **`state/ledger.jsonl` IS the forward evidence.** Never delete it.
>
> **`data/ticks/` is the other irreplaceable thing.** From launch, every
> accepted tick is archived as `XAUUSD_ticks_YYYYMMDD.csv.gz`. Your own venue's
> tick history is the one dataset you cannot buy or backfill — Dukascopy has
> *their* feed, not yours, and the difference is exactly the spread and slippage
> behaviour that decides whether a strategy pays. It starts accumulating the day
> the desk starts and not one day sooner.
>
> Back up both, weekly:
> `rsync -a you@vps:/opt/aurum/state/ you@vps:/opt/aurum/data/ticks/ ./aurum-backup/`
>
> Budget roughly 50-150 MB/month gzipped for ticks.

---

## 12. Read what it did

```bash
cd /opt/aurum
sudo -u aurum .venv/bin/python aurum_report.py --r-value 100
```

`--r-value` is what one R is worth in your account (1% of a $10k account = $100).
It is required for any net figure and deliberately not defaulted — an assumed R
value silently decides whether every component looks profitable.

Run it weekly. Sections: capture, information budget, path prediction, regime
novelty, model competition, management counterfactual, constitutional review.

---

## 13. The arms comparison — read the cost first

**This is where I was naive earlier and the numbers corrected me.**

Always price it before spending:

```bash
cd /opt/aurum
sudo -u aurum .venv/bin/python run_backtest.py \
    --parquet data/XAUUSD_M15.parquet --estimate-only
```

Real figures from the estimator:

| Sample | Full ladder A–H | **A vs B only** |
|---|---:|---:|
| 2 weeks M15 | $2,092 | **$92** |
| 3 months M15 | $6,974 | **$308** |
| 6 months M15 | $20,923 | **$924** |
| 1 year M15 | $122,049 | $5,390 |

**Do not run the full ladder.** I said "run arms B–H" earlier without pricing
it; that was wrong and I should have checked before recommending it.

One comparison gates all the others:

> **arm A (deterministic) vs arm B (Claude, numeric)** — does the model beat the
> baseline at all?

Every other rung sits *above* B on the ladder. If Claude does not beat the
baseline, C through H answer a question that no longer matters. If it does, you
have bought the fact that decides everything else for a few hundred dollars.

```bash
sudo -u aurum .venv/bin/python run_backtest.py \
    --parquet data/XAUUSD_M15.parquet \
    --arms AB --max-usd 350 --out backtest_out/ab
```

`--max-usd` refuses to start if the estimate exceeds it. It also prompts for
confirmation above $25. Set a spend limit in the Anthropic console as well —
that is the one that actually stops anything.

Spend the minimum that can change your mind. That is what an ablation ladder is
*for*: it is ordered so you can stop.

---

## 14. External signal collection (optional, later)

Only after the desk has been running for weeks.

```bash
# api_id / api_hash from https://my.telegram.org
sudo -u aurum nano /opt/aurum/.env      # TELEGRAM_API_ID, TELEGRAM_API_HASH
sudo systemctl enable --now aurum-capture
```

Constraints that are not negotiable:

- **Only channels you are legitimately a member of**, whose terms permit it
- Nothing bypasses authentication, defeats access controls, or evades rate limits
- The Telethon `*.session` file **is a live credential for your Telegram
  account.** It stays in `secrets/`, is hard-ignored by git, and must never be
  committed, zipped or pasted
- External signals are **information, never authority.** No source count,
  consensus threshold or provider reputation may ever become an automatic
  LONG/SHORT gate

---

## What I still cannot do from here

| Blocked on | Why |
|---|---|
| **Run the arms** | Needs `ANTHROPIC_API_KEY`. See step 13 — and read the cost note first, it changed my advice |
| **Verify `feed_oanda.py` live** | `403 CONNECT` to every external host from this sandbox |
| **Verify `fetch_dukascopy.py`** | Same |
| **#12 source discovery** | Needs your Telegram account and membership |
| **#13 cross-market data** | Module and the honest UNAVAILABLE path are built; the feed needs your credentials |

Send me a traceback from any of these and I can fix it without the network.

---

## Order of operations, condensed

```
1. Price feed: yahoo (nothing to do) or OANDA        0-10 min
2. Telegram bot + chat id                            ~5 min
3. VPS with python3.11+                              ~5 min
4. sudo ./deploy/install.sh                          ~10 min
5. Fill .env + secrets/                              ~5 min
6. run_desk.py --preflight   -> all PASS             ~2 min
7. fetch_dukascopy --offline-test, --selftest        2 min
   fetch_dukascopy --dry-run, then the pull (tmux)   6-15 hrs (3 years)
8. --declared-spread <your broker's spread>          IMPORTANT
9. Choose --management (heuristic)                   decision
9. Optional: MT5 stops_level                         ~5 min
10. systemctl enable --now aurum-desk, Sunday        ~5 min
11. run_backtest.py --estimate-only, then --arms AB  ~$300
12. aurum_report.py weekly                           ongoing
```

Then stop building and let it run. At this point more architecture is worth less
than four weeks of forward evidence on real gold.
