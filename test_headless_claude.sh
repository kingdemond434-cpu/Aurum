#!/usr/bin/env bash
# Run this on the machine where `claude` is logged into your actual Pro/Max
# subscription (NOT in a sandbox with no login) -- it cannot be run from here.
#
# WHAT THIS ANSWERS, BEFORE ANY CODE IN analyst.py CHANGES
#
#   1. Does `claude -p --output-format json --json-schema <file>` actually
#      return the analyst schema shape Aurum needs?
#   2. Does it accept an image (a rendered chart) the same way, in headless
#      mode, that call_analyst() currently sends via the messages API?
#   3. What does hitting the plan's usage ceiling actually look like --
#      an error Aurum can catch, or a silent hang? This is the "no process
#      dying" question, and nobody should guess at the answer.
#
# Nothing here touches Aurum's code. It only characterises `claude -p`
# itself, using a schema file shaped like AnalystRead so the test is honest
# about what the desk will actually ask for.
set -euo pipefail

OUT_DIR="./headless_probe_out"
mkdir -p "$OUT_DIR"

if ! command -v claude >/dev/null 2>&1; then
    echo "FATAL: 'claude' not on PATH. This must run where Claude Code is installed and logged in."
    exit 1
fi

echo "=== 0. sanity: who am I authenticated as ==="
claude --version
# If this prints an API-key-based identity rather than your subscription
# login, STOP -- that means ANTHROPIC_API_KEY is set in this shell and is
# shadowing the OAuth session (this is the precedence issue flagged
# earlier: an API key always wins over a subscription login).
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "WARNING: ANTHROPIC_API_KEY is set in this shell (len=${#ANTHROPIC_API_KEY})."
    echo "         Every call below will bill as API, not subscription. unset it first:"
    echo "         unset ANTHROPIC_API_KEY"
fi

echo
echo "=== 1. minimal structured-output probe ==="
cat > "$OUT_DIR/mini_schema.json" <<'EOF'
{
  "type": "object",
  "properties": {
    "direction": {"type": "string", "enum": ["LONG", "SHORT", "FLAT"]},
    "confidence": {"type": "integer", "minimum": 1, "maximum": 5},
    "read": {"type": "string"}
  },
  "required": ["direction", "confidence", "read"],
  "additionalProperties": false
}
EOF
claude -p "XAUUSD just swept the prior day low and reclaimed it on the H1 close. \
One sentence read and a direction call, nothing else." \
    --output-format json \
    --json-schema "$OUT_DIR/mini_schema.json" \
    --model claude-opus-5 \
    | tee "$OUT_DIR/01_structured.json"
echo "-> CHECK: does 01_structured.json have a 'structured_output' field matching the schema?"

echo
echo "=== 2. image input probe (this is the one nobody has confirmed) ==="
if [ -f "sample_chart.png" ]; then
    claude -p "Chart attached: sample_chart.png. Describe the last 5 candles in one sentence." \
        --output-format json \
        --model claude-opus-5 \
        | tee "$OUT_DIR/02_image.json"
    echo "-> CHECK: did it actually read the image, or hallucinate from the filename?"
else
    echo "SKIP: drop a real chart PNG at ./sample_chart.png and rerun to test this."
    echo "      This is a real gap: call_analyst() sends charts as base64 image blocks"
    echo "      in the messages API. Headless mode may need a file path referenced in"
    echo "      the prompt text instead, or may not support it the same way at all --"
    echo "      DO NOT ASSUME. If this doesn't work, charts stay on the API-key path."
fi

echo
echo "=== 3. burst probe: what does a rate/usage ceiling actually return ==="
echo "Firing 8 rapid --model claude-opus-5 calls. Watching for the FIRST non-zero exit"
echo "or any output that looks like a limit/quota message rather than a normal reply."
for i in $(seq 1 8); do
    ts=$(date +%H:%M:%S)
    if out=$(claude -p "Reply with exactly the word: PONG$i" --output-format json --model claude-opus-5 2>&1); then
        echo "[$ts] call $i OK: $(echo "$out" | head -c 150)"
    else
        code=$?
        echo "[$ts] call $i FAILED exit=$code"
        echo "$out" > "$OUT_DIR/03_failure_call_${i}.txt"
        echo "-> saved to $OUT_DIR/03_failure_call_${i}.txt -- this is the shape"
        echo "   call_analyst_headless() needs to catch and treat as retryable,"
        echo "   not let the desk go silent on."
    fi
    sleep 1
done

echo
echo "=== done. Read $OUT_DIR/ before touching golddesk/analyst.py. ==="
echo "Specifically: did #2 (images) work, and what did any #3 failure actually look like?"
