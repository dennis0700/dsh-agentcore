#!/usr/bin/env bash
# Delete the runtime stack (and its session storage), then optionally the base stack.
# Usage: scripts/destroy.sh [--all]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

read -r -p "Delete stack $RUNTIME_STACK in $AWS_REGION? This erases all session storage. [y/N] " ok
[[ "$ok" == "y" ]] || exit 1
aws cloudformation delete-stack --stack-name "$RUNTIME_STACK"
aws cloudformation wait stack-delete-complete --stack-name "$RUNTIME_STACK"

if [[ "${1:-}" == "--all" ]]; then
  read -r -p "Also delete $BASE_STACK (ECR repo must be empty)? [y/N] " ok
  [[ "$ok" == "y" ]] || exit 0
  aws cloudformation delete-stack --stack-name "$BASE_STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$BASE_STACK"
fi
