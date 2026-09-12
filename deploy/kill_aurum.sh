#!/usr/bin/env bash
# Full teardown of the Aurum desk on a systemd host.
#
# Timers and Restart=always are the equivalent of the Windows watchdog: `systemctl
# stop` on a unit with Restart=always brings it back. Every unit is DISABLED before
# it is stopped, and the timers go before the services they trigger.
#
# Stops processes only. Nothing is uninstalled and no ledger, state or repository
# file is touched.
#
#   sudo bash deploy/kill_aurum.sh          # tear down
#   sudo bash deploy/kill_aurum.sh --dry-run
set -uo pipefail

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1
run() { if [[ $DRY == 1 ]]; then echo "    would: $*"; else "$@" >/dev/null 2>&1; fi; }

# Timers first — a timer can start a service you just stopped.
TIMERS=(aurum-cycle.timer)
SERVICES=(aurum-desk.service aurum-bot.service aurum-capture.service aurum-cycle.service)

echo
echo "[1/3] Masking timers"
for u in "${TIMERS[@]}"; do
    if systemctl list-unit-files "$u" >/dev/null 2>&1 && systemctl cat "$u" >/dev/null 2>&1; then
        run systemctl disable --now "$u"
        run systemctl mask "$u"      # mask, not just disable: nothing can pull it back in
        echo "    $u  disabled + masked"
    else
        echo "    $u  not installed"
    fi
done

echo
echo "[2/3] Masking services"
for u in "${SERVICES[@]}"; do
    if systemctl cat "$u" >/dev/null 2>&1; then
        run systemctl disable --now "$u"
        run systemctl mask "$u"      # defeats Restart=always
        echo "    $u  disabled + masked"
    else
        echo "    $u  not installed"
    fi
done

echo
echo "[3/3] Killing stragglers"
# Matched on command line so we do not kill unrelated python on the box.
PAT='run_desk\.py|aurum_cycle\.py|signals_capture\.py|golddesk'
PIDS=$(pgrep -f "$PAT" 2>/dev/null || true)
if [[ -z "$PIDS" ]]; then
    echo "    none"
else
    for pid in $PIDS; do
        echo "    killing $pid: $(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | cut -c1-90)"
        run kill -TERM "$pid"
    done
    [[ $DRY == 0 ]] && sleep 5
    for pid in $(pgrep -f "$PAT" 2>/dev/null || true); do
        echo "    SIGKILL $pid (did not exit on TERM)"
        run kill -KILL "$pid"
    done
fi

echo
if [[ -z "$(pgrep -f "$PAT" 2>/dev/null || true)" ]]; then
    echo "AURUM IS DOWN."
else
    echo "NOT FULLY DOWN — still running:"; pgrep -af "$PAT"
fi
echo
echo "To bring it back:  systemctl unmask <unit> && systemctl enable --now <unit>"
echo
