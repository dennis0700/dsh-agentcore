from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import llm

def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _optional_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    # sessionStorage mount; only available while an invocation is being handled.
    mount_path: Path
    # Local (container disk) Harness home. dsh session logs need hard links,
    # which sessionStorage does not support, so the home stays local and is
    # synced to the mount after every run.
    dsh_home: Path
    provider: str
    model: str
    reasoning_effort: str | None
    max_tokens: int | None
    profile: str
    patches: tuple[str, ...]
    max_prompt_chars: int
    heartbeat_seconds: int
    initialize_timeout_seconds: int
    # Used only when no AgentCore session header is present (local testing).
    local_session_id: str

    @property
    def project_dir(self) -> Path:
        return self.mount_path / "project"

    @property
    def state_dir(self) -> Path:
        return self.mount_path / ".dsh-state"

    @classmethod
    def from_env(cls) -> "Settings":
              # CloudFormation cannot omit a single env var, so empty means "unset".
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DSH_REASONING_EFFORT", "DSH_MAX_TOKENS",
                     "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_CONTEXT_WINDOW", "LLM_MAX_OUTPUT_TOKENS",
                     "LLM_REASONING_EFFORTS", "LLM_COMPAT", "LLM_HEADERS"):
            if os.environ.get(name, None) == "":
                del os.environ[name]
        patches = tuple(
            p.strip()
            for p in os.environ.get("DSH_PATCHES", "/app/config/runtime.patch.yml").split(",")
            if p.strip()
        )
        llm_provider = os.environ.get("LLM_PROVIDER", "openai-compatible").strip()
        if llm_provider == "openai-compatible":
            patch = llm.write_patch(Path(os.environ.get("LLM_PATCH_PATH", "/home/app/.dsh-llm.patch.yml")))
            patches = (*patches, str(patch))
            provider, model = llm.ROUTE, os.environ["LLM_MODEL"].strip()
        elif llm_provider == "deepseek":
            provider = os.environ.get("DSH_PROVIDER", "deepseek-official")
            model = os.environ.get("DSH_MODEL", "deepseek-v4-flash")
        else:
            raise ValueError(f"LLM_PROVIDER must be 'deepseek' or 'openai-compatible', got {llm_provider!r}")
        return cls(
            mount_path=Path(os.environ.get("WORKSPACE_MOUNT", "/mnt/workspace")),
            dsh_home=Path(os.environ.get("DSH_HOME", "/home/app/.dsh-home")),
            provider=provider,
            model=model,
            reasoning_effort=os.environ.get("DSH_REASONING_EFFORT") or None,
            max_tokens=_optional_int_env("DSH_MAX_TOKENS"),
            profile=os.environ.get("DSH_PROFILE", "sdk"),
            patches=patches,
            max_prompt_chars=_int_env("MAX_PROMPT_CHARS", 100_000),
            heartbeat_seconds=_int_env("HEARTBEAT_SECONDS", 15),
            initialize_timeout_seconds=_int_env("DSH_INITIALIZE_TIMEOUT_SECONDS", 60),
            local_session_id=os.environ.get("LOCAL_SESSION_ID", "local-dev-session-0000000000000000"),
        )
