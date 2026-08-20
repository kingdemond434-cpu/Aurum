# Go live — everything left on your side

Ordered. Every command is copy-paste. Nothing here places an order: Aurum is a
signal desk and the deliverable is a Telegram message.

Read **§0** first — it decides which half you can actually run today.

---

## 0. What runs where, and why that matters

| | Aurum | quant |
|---|---|---|
| what it is | XAUUSD signal desk, advisory only | 22-instrument research + MT5 execution gateway |
| OS | **Linux** — your Contabo box | **Windows**, because `MetaTrader5` the Python package needs the MT5 terminal |
| live entry point | `run_desk.py --shadow` | `run_gateway_loop.py`, one pass per minute via Task Scheduler |
| shadow entry point | same, `--shadow` tags every message | `research/shadow_forward.py`, no capital, replays real bars |
| needs a broker login | no (read-only price feed) | yes, for the gateway; no, for shadow_forward |

**If your VPS is Linux only, you can run all of Aurum and quant's *research* and
*shadow*, but not quant's MT5 gateway.** That is not a blocker for going live in
shadow, which is the correct next step anyway — there is no forward evidence yet
and nothing should size real money against a backtest.

---

## Part A — Aurum, shadow, on the Linux VPS

### A1. Get the code

```bash
ssh you@your-vps
sudo apt-get update -qq && sudo apt-get install -y -qq git
git clone https://github.com/kingdemond434-cpu/Aurum.git ~/aurum-src
cd ~/aurum-src && git checkout claude/aurum-check-kqwpy6
```

### A2. Install

```bash
sudo ./deploy/install.sh
```

Creates the `aurum` service account, `/opt/aurum`, the venv, the directory
layout, and the systemd units. **It starts nothing** — that is deliberate.

### A3. Install Claude Code, so the analyst costs nothing

This is what makes the desk free to run. Skip it and every analyst read is
billed per token (~$0.63 a read, $640–1,280/month at the default cadence).

```bash
curl -fsSL https://claude.ai/install.sh | sudo bash     # or: npm i -g @anthropic-ai/claude-code
sudo -u aurum claude          # log in with your Pro/Max account, then quit
```

**Leave `ANTHROPIC_API_KEY` unset** in `/opt/aurum/.env`. If it is set, Claude
Code may bill it per token instead of using your subscription. Preflight warns
you, and the provider strips it from the CLI's environment, but the clean
configuration is simply not to set it.

### A4. Telegram — one command

First, on your phone: message your bot (press **Start**, or send `hello`). A bot
cannot open a conversation with you, so until you do this there is no chat to
send to.

```bash
echo 'YOUR_BOT_TOKEN' | sudo -u aurum /opt/aurum/.venv/bin/python \
  /opt/aurum/deploy/telegram_setup.py --stdin --secrets /opt/aurum/secrets
```

Verifies the token, finds your chat id, writes both files `0600`, and **sends a
real message through the desk's own sink**. Exit 0 means a message arrived. If
nothing arrives it failed and printed why.

### A5. The price feed

Cheapest path with no account:

```bash
sudo -u aurum nano /opt/aurum/.env      # leave ANTHROPIC_API_KEY unset
```

Then run with `--feed yahoo --declared-spread 0.35`. Yahoo publishes OHLC only,
so the desk builds bid/ask from the spread **you** declare — it refuses to
invent one, because that single number decides whether marginal trades are worth
taking. Check your broker's typical XAUUSD spread and use that.

If you have an OANDA practice account, set `OANDA_TOKEN` / `OANDA_ACCOUNT`
(**read-only token** — Aurum has no code to place orders and a trade-enabled
token grants authority it cannot use) and use `--feed oanda` instead.

### A6. Preflight — the gate

```bash
sudo -u aurum /opt/aurum/.venv/bin/python /opt/aurum/run_desk.py \
  --preflight --provider claudecode:claude-opus-5 \
  --feed yahoo --declared-spread 0.35
```

Every line must be `[PASS]`. It refuses to boot half-configured on purpose: a
24/5 process that looks alive and produces nothing is worse than one that will
not start.

### A7. Start it in shadow

```bash
sudo systemctl enable --now aurum-desk
sudo systemctl enable --now aurum-bot
journalctl -u aurum-desk -f
```

Then message your bot `/help`.

**Shadow tags every message `[SHADOW]`.** Leave it there. There is no forward
evidence yet — the ledger is empty — and every number in this repo is a
backtest. Shadow is how that stops being true.

---

## Part B — quant

### B1. Shadow (Linux is fine)

```bash
git clone https://github.com/kingdemond434-cpu/quant.git ~/quant
cd ~/quant && git checkout integration/full-quant
cd desks/mt5 && python3 -m pytest tests/ -q          # expect 382 passing
python3 research/shadow_forward.py
```

No capital, no broker. Replays real H1 bars through the same engine the
backtest used and journals to `reports/shadow/`.

### B2. The gateway (Windows + MT5 only)

Only if you have a Windows box with the MT5 terminal and `pip install MetaTrader5`.
Task Scheduler, every minute:

```
python C:\quant\desks\mt5\run_gateway_loop.py
```

A file lock prevents overlapping passes. **Do not run this against a live
account until shadow has produced forward evidence.**

### B3. Two jobs worth running unattended (free, hours)

```bash
cd ~/quant/desks/mt5
nohup python3 -u research/rank_ic.py --dates 500 --model-dates 500 \
  > logs/rank_ic_500.txt 2>&1 &
```

This is the one that decides whether the LLM ranker is worth anything. Baseline
is IC 0.0248 (t 1.90) over 496 dates. If the model beats it, the growth ceiling
roughly doubles; if not, we drop it and save the quota.

---

## Part C — how to know it is actually working

Three checks, in increasing order of what they prove.

```bash
systemctl is-active aurum-desk aurum-bot      # 1. the process exists
journalctl -u aurum-desk --since "1 hour ago" | tail -40
```

2. Message the bot `/status`. It reads the notification sink's health counters
   and is a **separate process** from the desk, so "bot answers, desk silent"
   and "both dead" are distinguishable — which is the point of splitting them.

3. **A message arrives in Telegram.** Aurum places no orders; the message is the
   entire product. A running process that has delivered nothing is total product
   failure wearing the appearance of health, which is why `HealthTrackingSink`
   counts consecutive failures and escalates to `ERROR` after five.

---

## Part D — what is wired, and what is not

Asked directly, so answered directly.

### Wired and tested

- **Signal → compile → risk gate → ledger → Telegram**, with every decision,
  refusal and outcome journalled to `state/ledger.jsonl`. That file *is* the
  forward evidence.
- **Growth-optimal sizing** derived from your stated drawdown tolerance, not a
  fixed lot.
- **Effective-trials deflation** and **k_eff independence** — the desk cannot
  credit itself with breadth it does not have.
- **Partial exits**: `bank_frac` / `bank_protect_k` take a fraction off at
  target and ratchet the survivor's stop.
- **The runner exit** that was measured this week: breathe at 4 ATR, tighten to
  1 ATR after 3 bars with no new extreme. Worth +3.7 points of CAGR on the book;
  measured across 22 instruments at +0.39 ATR/trade against a flat trail at the
  same final width.
- **Two-way bot control**, including `/halt` — which writes a *file*, so it
  survives a restart and works when the bot is dead.

### Built, deliberately NOT wired

- **`dying` (trend-death detection).** Banking the whole position on it loses on
  **0 of 22 instruments**, mean t −21.96. The trail already exits a dying trend
  and does it better. Wiring it because it was requested would be the expensive
  kind of agreeable.
- **The macro blackout rail.** `calendar_us.FIXED` covers 2026 only, so
  FOMC/CPI/PPI cannot be dated across the sample. Tested on the rule-derived
  events alone (NFP, claims) it made drawdown **worse** — 48.3% → 62.0%.

### Honest gaps

- **The quant → Aurum absorption channel has never carried anything.**
  `external/channels.txt` and `external/signals.jsonl` are both **0 bytes**. The
  module exists and enforces the right discipline (an external finding enters as
  a sealed hypothesis at zero authority), but nothing has passed through it. So:
  *no*, Aurum is not currently using quant's findings. That is a real gap.
- **The ledger is empty.** Nothing is live. Every CAGR in this repo is a
  backtest at half-edge.
- **The 77 candidates from the other session: zero clear the effective bar.**
  All 77 have `clears_effective_bar: false` and `dsr_deflated: null` against a
  3,168-trial search. The top one (NZDJPY monday-gap fade, in-sample Sharpe
  2.99) is *below* what the max of 3,168 trials produces by chance. Nothing
  there to promote.
- **`REAL_SURVIVORS.json` lists 9**, but those are hunt12's under the old
  multiplicity correction. Re-swept at the corrected bar (hunt14): **4**.
- **Max frequency is not maxed.** The book is 5 sleeves on 4 symbols. Expanding
  to all 44 was measured and it is catastrophic (−4.0% CAGR), and causal
  walk-forward selection does not rescue it (33.8%). More frequency requires
  more *edge*, not more instruments — breadth multiplies IC, it does not create
  it.

---

## The one number to hold onto

**100% CAGR needs 48.3% drawdown today, or 38.8% with a −2R daily loss limit.**
Half-edge, matched CAGR, validated at trade level with the 136 recovery days the
stop cost you charged against it.

On €1,500 that is a real, expected **−€580** at some point in the first year.
If that number makes you want to skip shadow mode, that is the number doing its
job.
