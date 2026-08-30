#!/usr/bin/env bash
# One-command install for a clean Debian/Ubuntu VPS.
#
#   sudo ./deploy/install.sh
#
# Creates the service account, the venv, the directory layout and the secret
# files, then tells you exactly which values are still missing. It does NOT
# start anything: run_desk.py --preflight is the gate, and it should be run
# deliberately once the secrets are filled in.
#
# Idempotent — safe to re-run after fixing something.

set -euo pipefail

AURUM_USER="${AURUM_USER:-aurum}"
AURUM_HOME="${AURUM_HOME:-/opt/aurum}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi

echo "==> system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync nodejs npm
npm install -g @openai/codex
echo "    Codex CLI installed for the ChatGPT analyst failover"

echo "==> service account: $AURUM_USER"
# --system: no login shell, no password, no home mail. The desk never needs an
# interactive session and an account that cannot log in cannot be logged into.
id -u "$AURUM_USER" &>/dev/null || \
    useradd --system --create-home --home-dir "$AURUM_HOME" \
            --shell /usr/sbin/nologin "$AURUM_USER"

echo "==> layout: $AURUM_HOME"
mkdir -p "$AURUM_HOME"/{state,logs,secrets/codex,external,data}
if [[ "$SRC" != "$AURUM_HOME" ]]; then
    rsync -a --delete \
        --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
        --exclude 'state' --exclude 'logs' --exclude 'secrets' \
        --exclude 'external' --exclude 'data' \
        "$SRC/" "$AURUM_HOME/"
fi

echo "==> virtualenv"
python3 -m venv "$AURUM_HOME/.venv"
"$AURUM_HOME/.venv/bin/pip" install -q --upgrade pip
"$AURUM_HOME/.venv/bin/pip" install -q -r "$AURUM_HOME/requirements.txt"
echo "    desk dependencies installed (live path only)"
# RESEARCH DEPS ARE SEPARATE AND NON-FATAL. pandas/pyarrow are a parquet
# reader/writer the live desk never touches, and pyarrow is the most likely
# thing to fail to build on a small VPS. A desk that does not use it must not be
# unable to start because of it.
if "$AURUM_HOME/.venv/bin/pip" install -q -r "$AURUM_HOME/requirements-research.txt" 2>/dev/null; then
    echo "    research dependencies installed (pandas/pyarrow)"
else
    echo "    research dependencies FAILED to install — this is NOT fatal."
    echo "    The live desk does not use them. You will not be able to run"
    echo "    fetch_dukascopy.py or run_backtest.py on this box; fetch"
    echo "    elsewhere and rsync the parquet over."
fi
if [[ "${WITH_CAPTURE:-0}" == "1" ]]; then
    "$AURUM_HOME/.venv/bin/pip" install -q -r "$AURUM_HOME/requirements-capture.txt"
    echo "    capture dependencies installed"
fi

echo "==> secrets"
# Created empty if absent so the operator has somewhere obvious to put them and
# preflight names the file rather than an abstract "missing credential".
for f in telegram_token telegram_chat_id; do
    [[ -f "$AURUM_HOME/secrets/$f" ]] || : > "$AURUM_HOME/secrets/$f"
done
[[ -f "$AURUM_HOME/.env" ]] || cp "$AURUM_HOME/deploy/env.example" "$AURUM_HOME/.env"

echo "==> permissions"
chown -R "$AURUM_USER:$AURUM_USER" "$AURUM_HOME"
# Secrets are readable only by the service account. .env holds the API key.
chmod 700 "$AURUM_HOME/secrets"
chmod 600 "$AURUM_HOME"/secrets/* "$AURUM_HOME/.env" 2>/dev/null || true

echo "==> systemd units"
install -m 644 "$AURUM_HOME/deploy/aurum-desk.service" /etc/systemd/system/
install -m 644 "$AURUM_HOME/deploy/aurum-capture.service" /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/aurum-desk.service
systemd-analyze verify /etc/systemd/system/aurum-capture.service
systemctl daemon-reload
echo "    units verified and loaded (not started)"

echo
echo "=============================================================="
echo "Installed. NOTHING IS RUNNING YET — that is deliberate."
echo
echo "1. Fill in the secrets:"
echo "     sudo -u $AURUM_USER nano $AURUM_HOME/.env"
echo "       ANTHROPIC_API_KEY, OANDA_TOKEN, OANDA_ACCOUNT"
echo
echo "   Telegram is ONE command — message your bot on your phone first"
echo "   (press Start), then let it find the chat id itself. It verifies the"
echo "   token, writes both files 0600, and sends a real message to prove the"
echo "   channel works. If no message arrives, it failed and says why."
echo "     echo '<bot token>' | sudo -u $AURUM_USER \\"
echo "       $AURUM_HOME/.venv/bin/python $AURUM_HOME/deploy/telegram_setup.py \\"
echo "       --stdin --secrets $AURUM_HOME/secrets"
echo
echo "2. Prove it is configured:"
echo "     sudo -u $AURUM_USER env HOME=$AURUM_HOME CODEX_HOME=$AURUM_HOME/secrets/codex codex login --device-auth"
echo "     sudo -u $AURUM_USER $AURUM_HOME/.venv/bin/python $AURUM_HOME/run_desk.py --preflight"
echo
echo "3. Only once preflight passes:"
echo "     sudo systemctl enable --now aurum-desk"
echo "     journalctl -u aurum-desk -f"
echo
echo "4. The control channel, so you can ask the desk things instead of"
echo "   SSH-ing in to read files. Separate unit on purpose: a bot that dies"
echo "   with the desk cannot answer 'is the desk alive?'."
echo "     sudo systemctl enable --now aurum-bot"
echo "     then message your bot /help"
echo "=============================================================="
