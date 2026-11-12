#!/usr/bin/env bash
# Local end-to-end test of the agent container. No AWS account needed.
#
# A container stands in for an AgentCore microVM and a Docker volume stands in
# for sessionStorage. The script exercises the HTTP contract, the event
# stream, OpenAI-compatible request shape, session restore on a fresh
# container, and secret handling.
#
#   tests/local_e2e.sh            # scripted mock model, no credentials needed
#   tests/local_e2e.sh --real     # a real endpoint from tests/e2e.env
#
# Options:
#   --real        call the endpoint in tests/e2e.env instead of the mock
#   --no-build    reuse an existing image instead of building one
#   --keep        leave containers and volumes in place for inspection
#   --image NAME  image tag to build or reuse (default dsh-agent:e2e)
#
# Environment (all optional): DOCKER="sudo docker", E2E_ENV_FILE=path,
# PIP_INDEX_URL, APT_MIRROR, BASE_IMAGE (build mirrors, as in deploy.env).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$ROOT/tests/e2e_lib.py"
MODE=mock BUILD=1 KEEP=0 IMAGE=dsh-agent:e2e
while [[ $# -gt 0 ]]; do
  case "$1" in
    --real) MODE=real ;;
    --no-build) BUILD=0 ;;
    --keep) KEEP=1 ;;
    --image) IMAGE="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

read -r -a DOCKER_CMD <<<"${DOCKER:-docker}"
d() { "${DOCKER_CMD[@]}" "$@"; }
command -v python3 >/dev/null || { echo "python3 is required on the host" >&2; exit 1; }
d info >/dev/null 2>&1 || { echo "cannot reach the Docker daemon; set DOCKER=\"sudo docker\" if needed" >&2; exit 1; }

RUN_ID="$(date +%s)$$"
NET="dsh-e2e-net-$RUN_ID"
VOL="dsh-e2e-ws-$RUN_ID"
MOCK="dsh-e2e-mock-$RUN_ID"
AGENT="dsh-e2e-agent-$RUN_ID"
PROBE="dsh-e2e-probe-$RUN_ID"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/dsh-e2e.XXXXXX")"
LLM_ENV="$OUT/llm.env"   # agent model settings; may hold the API key, so 0600
: >"$LLM_ENV"; chmod 600 "$LLM_ENV"
MOCK_LOG="$OUT/mock-requests.jsonl"

cleanup() {
  rm -f "$LLM_ENV"   # holds the API key in --real mode; never kept
  if [[ "$KEEP" == 1 ]]; then
    echo "kept: containers $AGENT $MOCK, volume $VOL, network $NET, output $OUT"
    echo "note: 'docker inspect $AGENT' shows the model settings, including the API key"
    return
  fi
  d rm -f "$AGENT" "$MOCK" "$PROBE" >/dev/null 2>&1 || true
  d volume rm "$VOL" >/dev/null 2>&1 || true
  d network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

PASS=0 FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
lib() { python3 "$LIB" "$@"; }

# Model settings ------------------------------------------------------------
if [[ "$MODE" == real ]]; then
  ENV_FILE="${E2E_ENV_FILE:-$ROOT/tests/e2e.env}"
  [[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE; copy tests/e2e.env.example and fill it in" >&2; exit 1; }
  python3 - "$ENV_FILE" "$LLM_ENV" <<'PY'
import sys
allowed = {"LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_CONTEXT_WINDOW",
           "LLM_MAX_OUTPUT_TOKENS", "LLM_REASONING_EFFORTS", "LLM_COMPAT", "LLM_HEADERS",
           "LLM_SUPPORTS_IMAGES", "DEEPSEEK_API_KEY", "DSH_MODEL"}
values = {}
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if key in allowed:
        values[key] = value
if values.get("LLM_PROVIDER", "openai-compatible") == "openai-compatible":
    missing = [k for k in ("LLM_BASE_URL", "LLM_MODEL") if not values.get(k)]
    if missing:
        sys.exit("set " + ", ".join(missing) + " in " + sys.argv[1])
with open(sys.argv[2], "w", encoding="utf-8") as out:
    for key, value in values.items():
        out.write(f"{key}={value}\n")
PY
  MODEL_LABEL="$(sed -n 's/^LLM_MODEL=//p' "$LLM_ENV") at $(sed -n 's/^LLM_BASE_URL=//p' "$LLM_ENV")"
  if grep -Eq '^LLM_BASE_URL=https?://(127\.0\.0\.1|localhost)' "$LLM_ENV"; then
    echo "LLM_BASE_URL points at localhost, which inside a container is the container itself." >&2
    echo "Use http://host.docker.internal:<port>/v1 for an endpoint on this machine." >&2
    exit 1
  fi
else
  cat >"$LLM_ENV" <<EOF
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=http://$MOCK:9000/v1
LLM_MODEL=mock-model
LLM_API_KEY=e2e-dummy-key-$RUN_ID
LLM_CONTEXT_WINDOW=65536
LLM_HEADERS={"X-E2E-Run":"$RUN_ID"}
EOF
  MODEL_LABEL="scripted mock"
fi

# Build ---------------------------------------------------------------------
echo "mode:   $MODE ($MODEL_LABEL)"
echo "image:  $IMAGE"
echo "output: $OUT"
if [[ "$BUILD" == 1 ]]; then
  echo; echo "[build] docker build (linux/arm64)"
  BUILD_ARGS=()
  [[ -n "${PIP_INDEX_URL:-}" ]] && BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
  [[ -n "${APT_MIRROR:-}" ]] && BUILD_ARGS+=(--build-arg "APT_MIRROR=$APT_MIRROR")
  [[ -n "${BASE_IMAGE:-}" ]] && BUILD_ARGS+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
  if ! d build --platform linux/arm64 "${BUILD_ARGS[@]}" -t "$IMAGE" "$ROOT" >"$OUT/build.log" 2>&1; then
    tail -30 "$OUT/build.log"; echo "build failed; full log: $OUT/build.log" >&2; exit 1
  fi
fi

# Infrastructure ------------------------------------------------------------
d network create "$NET" >/dev/null
d volume create "$VOL" >/dev/null
if [[ "$MODE" == mock ]]; then
  d run -d --name "$MOCK" --network "$NET" -e MOCK_DELAY_SECONDS=3 \
    -v "$ROOT/tests/mock_openai.py:/opt/mock_openai.py:ro" --entrypoint python "$IMAGE" /opt/mock_openai.py >/dev/null
fi
# Probe container on the same network; reaches the agent by name, needs no host port.
d run -d --name "$PROBE" --network "$NET" --entrypoint sleep "$IMAGE" infinity >/dev/null

start_agent() {
  d rm -f "$AGENT" >/dev/null 2>&1 || true
  d run -d --name "$AGENT" --network "$NET" --add-host host.docker.internal:host-gateway \
    -v "$VOL:/mnt/workspace" --env-file "$LLM_ENV" "$IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    d exec "$PROBE" curl -sf "http://$AGENT:8080/ping" >/dev/null 2>&1 && return 0
    sleep 1
  done
  d logs "$AGENT" | tail -30; echo "agent did not become healthy" >&2; return 1
}
invoke() {  # invoke <out-file> <session-id> <prompt> [stream true|false]
  local body; body="$(lib payload "$3" "${4:-false}")"
  d exec "$PROBE" curl -sS -N --max-time 600 -X POST "http://$AGENT:8080/invocations" \
    -H 'Content-Type: application/json' -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $2" \
    -d "$body" >"$1" 2>>"$OUT/curl.err" || true
}
workspace() { d exec "$AGENT" sh -c "$1"; }
mock_log() { [[ "$MODE" == mock ]] && d exec "$MOCK" cat /tmp/mock-requests.jsonl >"$MOCK_LOG" 2>/dev/null; }
ping_healthy() { d exec "$PROBE" curl -sf "http://$AGENT:8080/ping" | grep -q Healthy; }
file_has() { d exec "$AGENT" cat "/mnt/workspace/project/$1" | grep -q "$2"; }
no_local_session() { ! d exec "$AGENT" sh -c "ls /home/app/.dsh-home/sessions/*/$1" >/dev/null 2>&1; }
image_has_no_llm_env() { ! d image inspect "$IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^LLM_'; }
recalls_code_word() { lib contains "$1" final_response pelican || lib contains "$1" final_response Pelican; }

S1="e2e-session-$RUN_ID-0000000000001"
S2="e2e-session-$RUN_ID-0000000000002"
MARKER="m$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"

# 1. Contract ----------------------------------------------------------------
echo; echo "[1/6] HTTP contract"
start_agent
check "GET /ping reports Healthy" ping_healthy
invoke "$OUT/bad.json" "$S1" " "
check "blank prompt is rejected with BAD_REQUEST" lib expect "$OUT/bad.json" type=error code=BAD_REQUEST

# 2. First run ---------------------------------------------------------------
echo; echo "[2/6] first run: tool call, file write, event stream"
if [[ "$MODE" == mock ]]; then
  PROMPT_1="write mock.txt"
  EXPECTED_FILE=mock-ok
else
  PROMPT_1="Run this exact shell command: echo $MARKER > verify.txt
Then remember this code word for later: pelican. Reply with the single word: done."
  EXPECTED_FILE=$MARKER
fi
invoke "$OUT/run1.sse" "$S1" "$PROMPT_1" true
check "stream ends with finish_reason=completed" lib expect "$OUT/run1.sse" type=final finish_reason=completed
check "stream reports a bash tool call" lib has "$OUT/run1.sse" type=tool state=start name=bash
check "tool call succeeded" lib has "$OUT/run1.sse" type=tool state=end is_error=false
check "status events: running then idle" lib has "$OUT/run1.sse" type=status status=idle
FILE_NAME=$([[ "$MODE" == mock ]] && echo mock.txt || echo verify.txt)
check "file written to /mnt/workspace/project" file_has "$FILE_NAME" "$EXPECTED_FILE"
check "dsh state mirrored to sessionStorage" workspace 'ls /mnt/workspace/.dsh-state/sessions/*/'"$S1"

# 3. Request shape (mock only) -------------------------------------------------
echo; echo "[3/6] request sent to the OpenAI-compatible endpoint"
if [[ "$MODE" == mock ]]; then
  mock_log
  check "Chat Completions shape: streaming, max_tokens, no store/developer, bearer auth" lib request-shape "$MOCK_LOG"
  check "LLM_HEADERS reached the request" lib header "$MOCK_LOG" "$RUN_ID"
else
  echo "  SKIP  only checked against the mock"
fi

# 4. Concurrency ---------------------------------------------------------------
echo; echo "[4/6] concurrent invocation on a busy session"
if [[ "$MODE" == mock ]]; then
  invoke "$OUT/slow.sse" "$S2" "write mock.txt" true &
  SLOW=$!
  sleep 2
  invoke "$OUT/busy.json" "$S2" "second request"
  wait "$SLOW"
  check "second request gets SESSION_BUSY" lib expect "$OUT/busy.json" type=error code=SESSION_BUSY
  check "first request still completes" lib expect "$OUT/slow.sse" type=final finish_reason=completed
else
  echo "  SKIP  only checked against the mock (needs a predictable delay)"
fi

# 5. Restore on a fresh container ---------------------------------------------
echo; echo "[5/6] new container, same volume (simulated new microVM)"
start_agent
check "fresh container has no local session before the request" no_local_session "$S1"
if [[ "$MODE" == mock ]]; then
  invoke "$OUT/run2.json" "$S1" "resume-check-$MARKER"
  check "resumed run completes" lib expect "$OUT/run2.json" type=final finish_reason=completed
  mock_log
  check "resumed request carries the earlier history" lib last-request-contains "$MOCK_LOG" "write mock.txt"
else
  invoke "$OUT/run2.json" "$S1" "Without calling any tools, reply with exactly one line: code=<the code word I asked you to remember>"
  check "resumed run completes" lib expect "$OUT/run2.json" type=final finish_reason=completed
  check "conversation history survived (code word recalled)" recalls_code_word "$OUT/run2.json"
  invoke "$OUT/run3.json" "$S1" "Run this exact shell command and reply with only its output: cat verify.txt"
  check "workspace survived (file contents returned)" lib contains "$OUT/run3.json" final_response "$MARKER"
fi
d logs "$AGENT" >"$OUT/agent.log" 2>&1
check "log: state restored from sessionStorage" grep -q "restored .* state files" "$OUT/agent.log"
check "log: dsh session resumed" grep -q "resumed dsh session $S1" "$OUT/agent.log"

# 6. Secrets -------------------------------------------------------------------
echo; echo "[6/6] secret handling"
d cp "$ROOT/tests/scan_secret.py" "$AGENT:/tmp/scan_secret.py" >/dev/null
check "API key not written to any file in the container or volume" d exec "$AGENT" python /tmp/scan_secret.py
check "image does not bake in model settings" image_has_no_llm_env

echo
echo "passed $PASS, failed $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  d logs "$AGENT" >"$OUT/agent.log" 2>&1 || true
  echo "LOCAL E2E FAIL - see $OUT (agent.log, *.sse, *.json)"
  exit 1
fi
echo "LOCAL E2E PASS"
