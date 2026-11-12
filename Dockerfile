# syntax=docker/dockerfile:1
# AgentCore Runtime requires linux/arm64.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
FROM --platform=linux/arm64 ${BASE_IMAGE}

# Optional mirrors for builds inside mainland China, e.g.
#   --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
#   --build-arg APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=
ARG APT_MIRROR=

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git curl jq ripgrep bubblewrap procps; \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /mnt/workspace && chown app:app /mnt/workspace

WORKDIR /app
COPY requirements.txt .
RUN set -eux; \
    if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi; \
    pip install --no-cache-dir -r requirements.txt

COPY config/ /app/config/
COPY app/ /app/app/

ENV PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    DSH_HOME=/home/app/.dsh-home \
    WORKSPACE_MOUNT=/mnt/workspace \
    DSH_PROFILE=sdk \
    DSH_PATCHES=/app/config/runtime.patch.yml \
    DSH_PERMISSION_MODE=danger-full-access \
    DSH_TELEMETRY_DISABLED=1

USER app
# Initialise the profile at build time so cold starts skip it.
RUN mkdir -p "$DSH_HOME" && dsh --profile "$DSH_PROFILE" --dump-default-config > /dev/null

EXPOSE 8080
CMD ["python", "-m", "app.main"]
