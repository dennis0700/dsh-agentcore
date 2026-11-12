#!/usr/bin/env bash
# End-to-end check of the deployed runtime using the AgentCore CLI.
#
#   1. invoke: write a file and remember a code word
#   2. exec:   inspect sessionStorage inside the session's microVM
#   3. stop the session (microVM is reclaimed)
#   4. invoke the same session again: the file and the code word must survive
#   5. logs:   confirm the state was restored and the dsh session resumed
#
# Requires: npm i -g @aws/agentcore, deploy.env, a deployed runtime stack.
# Usage: scripts/verify_cli.sh [session-id]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

command -v agentcore >/dev/null || { echo "agentcore CLI not found: npm i -g @aws/agentcore" >&2; exit 1; }
agentcore config telemetry.enabled false >/dev/null 2>&1 || true

RUNTIME_ID="$(stack_output "$RUNTIME_STACK" AgentRuntimeId)"
RUNTIME_ARN="$(stack_output "$RUNTIME_STACK" AgentRuntimeArn)"
SESSION="${1:-verify-$(date +%s)-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}"
MARKER="m$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
OUT="$(mktemp -d /tmp/dsh-verify.XXXXXX)"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "runtime: $RUNTIME_ID"
echo "session: $SESSION"
echo "output:  $OUT"

FAILED=0
check() {  # check <name> <0|1>
  if [[ "$2" == 0 ]]; then echo "  PASS  $1"; else echo "  FAIL  $1"; FAILED=1; fi
}
payload() {
  python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1], "stream": False}))' "$1"
}
invoke() {  # invoke <file> <prompt>
  agentcore runtime invoke --id "$RUNTIME_ID" --session-id "$SESSION" \
    --content-type application/json --accept application/json \
    --payload "$(payload "$2")" --output-file "$1" >"$1.cli" 2>&1
}
field() {  # field <file> <key>: print a key from the agent's final JSON message
  python3 - "$1" "$2" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read().strip()
for chunk in reversed([c for c in text.replace("data: ", "\n").splitlines() if c.strip()]):
    try:
        obj = json.loads(chunk)
    except ValueError:
        continue
    if isinstance(obj, dict) and obj.get("type") in ("final", "error"):
        value = obj.get(sys.argv[2], "")
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        break
PY
}

echo
echo "[1/5] invoke: create a file and remember the code word"
invoke "$OUT/1.json" "Run this exact shell command: echo $MARKER > verify.txt
Then remember this code word for later: pelican. Reply with the single word: done."
check "first invoke returned finish_reason=completed" "$([[ "$(field "$OUT/1.json" finish_reason)" == completed ]]; echo $?)"

echo
echo "[2/5] exec: inspect sessionStorage in the microVM"
if agentcore runtime exec --id "$RUNTIME_ID" --session-id "$SESSION" --timeout 60 --command \
  'set -x; cat /mnt/workspace/project/verify.txt; ls -la /mnt/workspace/.dsh-state; du -sh /mnt/workspace; df -h /mnt/workspace;
   cd /mnt/workspace && echo x > .lt && (ln .lt .lt2 && echo HARDLINK=supported || echo HARDLINK=unsupported); rm -f .lt .lt2' \
  >"$OUT/2.exec" 2>&1; then
  check "exec: verify.txt contains the marker" "$(grep -q "$MARKER" "$OUT/2.exec"; echo $?)"
  check "exec: dsh state mirrored to sessionStorage" "$(grep -q sessions "$OUT/2.exec"; echo $?)"
  grep -o 'HARDLINK=[a-z]*' "$OUT/2.exec" | tail -1 | sed 's/^/  INFO  /'
else
  echo "  SKIP  runtime exec failed (InvokeAgentRuntimeCommand may be unavailable here); see $OUT/2.exec"
fi

echo
echo "[3/5] stop the session"
aws bedrock-agentcore stop-runtime-session --agent-runtime-arn "$RUNTIME_ARN" --runtime-session-id "$SESSION" >"$OUT/3.stop" 2>&1
check "stop-runtime-session accepted" "$?"
echo "  waiting 30s for the microVM to flush and terminate"
sleep 30

echo
echo "[4/5] invoke the same session on a new microVM"
# One step, one message: final_response only carries the last assistant message.
invoke "$OUT/4.json" "Without calling any tools, reply with exactly one line in this form:
code=<the code word I asked you to remember>"
ANSWER="$(field "$OUT/4.json" final_response)"
check "resumed invoke returned finish_reason=completed" "$([[ "$(field "$OUT/4.json" finish_reason)" == completed ]]; echo $?)"
check "conversation history survived (code word recalled)" "$(grep -qi pelican <<<"$ANSWER"; echo $?)"
invoke "$OUT/4b.json" "Run this exact shell command and reply with only its output: cat verify.txt"
FILE_ANSWER="$(field "$OUT/4b.json" final_response)"
check "workspace survived (file contents returned)" "$(grep -q "$MARKER" <<<"$FILE_ANSWER"; echo $?)"

echo
echo "[5/5] logs: confirm restore and resume"
sleep 10
agentcore runtime logs --id "$RUNTIME_ID" --since "$STARTED" --limit 500 >"$OUT/5.logs" 2>&1 || true
check "logs: state restored from sessionStorage" "$(grep -q "restored .* state files" "$OUT/5.logs"; echo $?)"
check "logs: dsh session resumed" "$(grep -q "resumed dsh session $SESSION" "$OUT/5.logs"; echo $?)"

echo
if [[ "$FAILED" == 0 ]]; then
  echo "VERIFY PASS"
else
  echo "VERIFY FAIL - inspect $OUT (1.json, 2.exec, 4.json, 5.logs, *.cli)"
  echo "last answer: ${ANSWER:0:300}"
fi
exit "$FAILED"
