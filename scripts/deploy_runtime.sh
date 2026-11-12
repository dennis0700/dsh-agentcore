#!/usr/bin/env bash
# Create or update the AgentCore runtime.
# Usage: scripts/deploy_runtime.sh [image-uri]   (default: last image from build_push.sh)
# Note: every update creates a new runtime version, which resets sessionStorage
# for all sessions.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

IMAGE="${1:-$(cat "$ROOT_DIR/.last-image" 2>/dev/null || true)}"
: "${IMAGE:?pass an image URI or run build_push.sh first}"

ROLE_ARN="$(stack_output "$BASE_STACK" ExecutionRoleArn)"
export IMAGE ROLE_ARN

# Parameters go through a 0600 temp file so the API key is not on the command line.
PARAMS_FILE="$(mktemp)"
trap 'rm -f "$PARAMS_FILE"' EXIT
chmod 600 "$PARAMS_FILE"
python3 - "$PARAMS_FILE" <<'PY'
import json, os, sys
env = os.environ
provider = env.get("LLM_PROVIDER", "openai-compatible")
if provider == "openai-compatible":
    missing = [k for k in ("LLM_BASE_URL", "LLM_MODEL") if not env.get(k)]
    if missing:
        sys.exit(f"set {', '.join(missing)} in deploy.env for LLM_PROVIDER=openai-compatible")
elif provider == "deepseek" and not env.get("DEEPSEEK_API_KEY"):
    sys.exit("set DEEPSEEK_API_KEY in deploy.env for LLM_PROVIDER=deepseek")
params = {
    "AgentName": env["AGENT_NAME"],
    "ContainerUri": env["IMAGE"],
    "ExecutionRoleArn": env["ROLE_ARN"],
    "LlmProvider": provider,
    "LlmBaseUrl": env.get("LLM_BASE_URL", ""),
    "LlmModel": env.get("LLM_MODEL", ""),
    "LlmApiKey": env.get("LLM_API_KEY", ""),
    "LlmContextWindow": env.get("LLM_CONTEXT_WINDOW", "128000"),
    "LlmMaxOutputTokens": env.get("LLM_MAX_OUTPUT_TOKENS", ""),
    "LlmReasoningEfforts": env.get("LLM_REASONING_EFFORTS", ""),
    "LlmCompat": env.get("LLM_COMPAT", ""),
    "LlmHeaders": env.get("LLM_HEADERS", ""),
    "LlmSupportsImages": env.get("LLM_SUPPORTS_IMAGES", "false"),
    "DeepSeekApiKey": env.get("DEEPSEEK_API_KEY", ""),
    "DeepSeekBaseUrl": env.get("DEEPSEEK_BASE_URL", ""),
    "Model": env.get("DSH_MODEL", "deepseek-v4-flash"),
    "ReasoningEffort": env.get("DSH_REASONING_EFFORT", ""),
    "MaxTokens": env.get("DSH_MAX_TOKENS", ""),
    "Profile": env.get("DSH_PROFILE", "sdk"),
    "PermissionMode": env.get("DSH_PERMISSION_MODE", "danger-full-access"),
    "UploadSessionLog": env.get("DSH_UPLOAD_SESSION_LOG", "false"),
    "IdleRuntimeSessionTimeout": env.get("IDLE_RUNTIME_SESSION_TIMEOUT", "1800"),
    "MaxLifetime": env.get("MAX_LIFETIME", "28800"),
}
with open(sys.argv[1], "w") as f:
    json.dump([f"{k}={v}" for k, v in params.items()], f)
PY

aws cloudformation deploy \
  --stack-name "$RUNTIME_STACK" \
  --template-file "$ROOT_DIR/infra/runtime.yaml" \
  --no-fail-on-empty-changeset \
  --parameter-overrides "file://$PARAMS_FILE"

echo "AgentRuntimeArn:     $(stack_output "$RUNTIME_STACK" AgentRuntimeArn)"
echo "AgentRuntimeVersion: $(stack_output "$RUNTIME_STACK" AgentRuntimeVersion)"
