#!/usr/bin/env bash
# Create or update the ECR repository and execution role.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

aws cloudformation deploy \
  --stack-name "$BASE_STACK" \
  --template-file "$ROOT_DIR/infra/base.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    AgentName="$AGENT_NAME" \
    RepositoryName="$REPOSITORY_NAME"

echo "RepositoryUri:    $(stack_output "$BASE_STACK" RepositoryUri)"
echo "ExecutionRoleArn: $(stack_output "$BASE_STACK" ExecutionRoleArn)"
