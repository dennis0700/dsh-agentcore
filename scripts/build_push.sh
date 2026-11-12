#!/usr/bin/env bash
# Build the linux/arm64 image and push it to ECR with an immutable tag.
# Usage: scripts/build_push.sh [tag]   (default: UTC timestamp)
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

TAG="${1:-$(date -u +%Y%m%d%H%M%S)}"
REGISTRY="$(ecr_registry)"
IMAGE="${REGISTRY}/${REPOSITORY_NAME}:${TAG}"

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"

# On x86_64 hosts, cross-building needs QEMU first:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx build --platform linux/arm64 \
  --build-arg BASE_IMAGE="${BASE_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim-bookworm}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL:-}" \
  --build-arg APT_MIRROR="${APT_MIRROR:-}" \
  -t "$IMAGE" --push "$ROOT_DIR"

echo "$IMAGE" > "$ROOT_DIR/.last-image"
echo "pushed $IMAGE"
