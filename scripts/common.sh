#!/usr/bin/env bash
# Shared helpers for deployment scripts.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE (copy deploy.env.example)" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${AWS_REGION:?}" "${STACK_PREFIX:?}" "${AGENT_NAME:?}" "${REPOSITORY_NAME:?}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

BASE_STACK="${STACK_PREFIX}-base"
RUNTIME_STACK="${STACK_PREFIX}-runtime"

stack_output() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

ecr_registry() {
  local account
  account="$(aws sts get-caller-identity --query Account --output text)"
  local suffix="amazonaws.com"
  [[ "$AWS_REGION" == cn-* ]] && suffix="amazonaws.com.cn"
  echo "${account}.dkr.ecr.${AWS_REGION}.${suffix}"
}
