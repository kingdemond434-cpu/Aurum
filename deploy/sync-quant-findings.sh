#!/usr/bin/env bash
# Move the quant desk's findings into Aurum's absorption inbox.
#
# WHY THIS IS A SCRIPT AND NOT A PYTHON IMPORT
#
# The two desks are separate repositories with separate lifecycles. A python
# step on either side that reached into the other's checkout would fail in a
# way neither desk owns -- Aurum's step_absorb says exactly this from its side,
# and the quant exporter says it from the other. The transport is therefore an
# operator-owned step, and this file is that step written down so it is not
# re-invented differently every time.
#
# WHAT IT MOVES
#
#   <quant>/desks/mt5/reports/aurum_findings.jsonl   (produced daily by
#                                                     research/daily_cycle.py)
#        -> <aurum>/inbox/quant_findings.jsonl       (read daily by
#                                                     aurum_cycle.py step_absorb)
#
# APPEND, NEVER OVERWRITE, AND DEDUPED ON THE WAY IN
#
# Overwriting would silently drop any finding that was exported, absorbed, and
# then rotated out of the source file. Appending alone would grow the inbox
# without bound. So this appends only rows whose (statement, measured_on) pair
# is not already present -- the SAME pair the quant exporter dedups on and the
# same one Aurum's Absorber content-hashes on, so all three agree about what
# counts as the same claim. Running this twice is a no-op.
#
# USAGE
#
#   deploy/sync-quant-findings.sh /path/to/quant [/path/to/aurum]
#
# Cron it next to the daily cycle if both repos live on the same box:
#   15 22 * * *  /opt/aurum/deploy/sync-quant-findings.sh /opt/quant /opt/aurum
set -euo pipefail

QUANT_ROOT="${1:-}"
AURUM_ROOT="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [ -z "$QUANT_ROOT" ]; then
    echo "usage: $0 /path/to/quant [/path/to/aurum]" >&2
    exit 2
fi

SRC="$QUANT_ROOT/desks/mt5/reports/aurum_findings.jsonl"
DST_DIR="$AURUM_ROOT/inbox"
DST="$DST_DIR/quant_findings.jsonl"

if [ ! -f "$SRC" ]; then
    # NOT an error, and deliberately not silent. reports/ is gitignored and
    # lives on whichever host ran the hunts, so its absence on this box is a
    # real and expected state -- but it must be SAID, because "no findings
    # arrived" and "the source file was never here" are different facts and
    # only one of them means the quant desk learned nothing.
    echo "$(date -u +%FT%TZ) no source at $SRC -- nothing to sync." \
         "That is UNMEASURED, not 'no new findings'."
    exit 0
fi

mkdir -p "$DST_DIR"
touch "$DST"

python3 - "$SRC" "$DST" <<'PY'
import json, sys, pathlib

src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

def key(row):
    return (row.get("statement", "").strip().lower(),
            row.get("measured_on", "").strip().lower())

def rows(p):
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # A malformed row is reported, never dropped silently: a transport
            # that quietly discards what it cannot parse is how a finding goes
            # missing with every step reporting success.
            print(f"  WARNING malformed row skipped in {p.name}: {line[:90]}")

seen = {key(r) for r in rows(dst)}
new = [r for r in rows(src) if key(r) not in seen]

if not new:
    print(f"  0 new finding(s); inbox already holds {len(seen)}. "
          f"Steady state for a daily run.")
else:
    with dst.open("a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r) + "\n")
    print(f"  {len(new)} new finding(s) appended to {dst} "
          f"({len(seen)} already present)")
    for r in new:
        print(f"    [{r.get('grade','?')}] {r.get('statement','')[:88]}...")
PY

echo "$(date -u +%FT%TZ) sync complete. Aurum's next cycle will queue anything new"
echo "as a SEALED HYPOTHESIS at zero authority -- absorption is not adoption."
