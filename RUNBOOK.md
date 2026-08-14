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

## 1. OANDA account and a READ-ONLY token

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

Set `ANTHROPIC_API_KEY`, `OANDA_TOKEN`, `OANDA_ACCOUNT`. No quotes, no `export`,
no trailing comments — systemd reads the line literally.

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

```bash
cd /opt/aurum
sudo -u aurum .venv/bin/python fetch_dukascopy.py --symbol XAUUSD \
    --start 2015-01-01 --out data/xauusd_ticks.parquet
```

Free tick data back to ~2003. Expect **1–3 hours** and several GB; run it under
`tmux` or `screen`. Untested against the live source from here — if it errors,
send me the traceback and the failing URL.

**Why it matters:** the arms comparison needs paired states in volume. Twenty
trades cannot separate anything, and no amount of architecture substitutes for
sample.

---

## 8. Decide who manages the trade

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

## 9. Optional: your broker's minimum stop distance

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

## 10. Launch, in shadow

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

> **`state/ledger.jsonl` IS the forward evidence.** Never delete it. Back it up:
> `rsync -a you@vps:/opt/aurum/state/ ./aurum-state-backup/`

---

## 11. Read what it did

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

## 12. External signal collection (optional, later)

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
| **Run arms B–H** | Needs `ANTHROPIC_API_KEY`. This is the real evidence gap — one arm, 20 trades, −7.8R |
| **Verify `feed_oanda.py` live** | `403 CONNECT` to every external host from this sandbox |
| **Verify `fetch_dukascopy.py`** | Same |
| **#12 source discovery** | Needs your Telegram account and membership |
| **#13 cross-market data** | Module and the honest UNAVAILABLE path are built; the feed needs your credentials |

Send me a traceback from any of these and I can fix it without the network.

---

## Order of operations, condensed

```
1. OANDA practice account + read-only token          ~10 min
2. Telegram bot + chat id                            ~5 min
3. VPS with python3.11+                              ~5 min
4. sudo ./deploy/install.sh                          ~10 min
5. Fill .env + secrets/                              ~5 min
6. run_desk.py --preflight   -> all PASS             ~2 min
7. fetch_dukascopy.py (tmux)                         1-3 hrs
8. Choose --management (heuristic)                   decision
9. Optional: MT5 stops_level                         ~5 min
10. systemctl enable --now aurum-desk, Sunday        ~5 min
11. aurum_report.py weekly                           ongoing
```

Then stop building and let it run. At this point more architecture is worth less
than four weeks of forward evidence on real gold.
