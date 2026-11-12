"""Build the dsh patch that routes model requests to an OpenAI-compatible endpoint.

dsh reaches non-DeepSeek endpoints through its `llm-pi-ai` adapter. A route the
installed catalog does not describe needs `api`, `baseURL` and an explicit model
list; everything else here is optional tuning read from the environment.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ROUTE = "openai-compatible"
API_KEY_ENV = "LLM_API_KEY"

# pi-ai treats an unrecognised baseURL as api.openai.com itself, which sends
# `store`, the `developer` role and `max_completion_tokens`. Most compatible
# gateways (vLLM, SGLang, LiteLLM, one-api, ...) expect the older shape.
DEFAULT_COMPAT: dict[str, Any] = {
    "supportsStore": False,
    "supportsDeveloperRole": False,
    "maxTokensField": "max_tokens",
}


class LlmConfigError(ValueError):
    pass


def _json_env(name: str) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmConfigError(f"{name} is not valid JSON: {exc}") from exc


def _positive_int_env(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise LlmConfigError(f"{name} must be a positive integer")
    return value


def build_route() -> dict[str, Any]:
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not base_url or not model:
        raise LlmConfigError("LLM_BASE_URL and LLM_MODEL are required for the openai-compatible provider")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LlmConfigError(f"LLM_BASE_URL must be an http(s) URL, got {base_url!r}")
    if not os.environ.get(API_KEY_ENV, "").strip():
        # Keyless gateways still need a bearer value; pi-ai sends it as-is.
        os.environ[API_KEY_ENV] = "unused"
    elif parsed.scheme == "http":
        log.warning("LLM_BASE_URL uses plain http; the API key is sent unencrypted to %s", parsed.netloc)

    model_entry: dict[str, Any] = {
        "id": model,
        "name": os.environ.get("LLM_MODEL_NAME", "").strip() or model,
        # Compaction triggers from this value, so it must match the real model.
        "contextWindow": _positive_int_env("LLM_CONTEXT_WINDOW", 128_000),
        "input": ["image", "text"] if os.environ.get("LLM_SUPPORTS_IMAGES") == "true" else ["text"],
    }
    max_tokens = _positive_int_env("LLM_MAX_OUTPUT_TOKENS", None)
    if max_tokens is not None:
        model_entry["maxTokens"] = max_tokens

    efforts = _json_env("LLM_REASONING_EFFORTS")
    if efforts is not None:
        if efforts is not False and not isinstance(efforts, dict):
            raise LlmConfigError("LLM_REASONING_EFFORTS must be false or an object like {\"off\": null, \"high\": \"high\"}")
        model_entry["reasoningEfforts"] = efforts

    compat = dict(DEFAULT_COMPAT)
    extra = _json_env("LLM_COMPAT")
    if extra is not None:
        if not isinstance(extra, dict):
            raise LlmConfigError("LLM_COMPAT must be a JSON object")
        compat.update(extra)

    route: dict[str, Any] = {
        "displayName": os.environ.get("LLM_DISPLAY_NAME", "").strip() or "OpenAI-compatible",
        "api": "openai-completions",
        "baseURL": base_url,
        "apiKeyEnv": API_KEY_ENV,
        "models": [model_entry],
        "compat": compat,
    }
    headers = _json_env("LLM_HEADERS")
    if headers is not None:
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise LlmConfigError("LLM_HEADERS must be a JSON object of strings")
        route["headers"] = headers
    timeout_ms = _positive_int_env("LLM_TIMEOUT_MS", None)
    if timeout_ms is not None:
        route["timeoutMs"] = timeout_ms
    return route


def write_patch(path: Path) -> Path:
    """Write the llm-pi-ai patch. JSON is valid YAML, so no YAML dependency is needed."""
    patch = [
        {"id": "llm-pi-ai", "config": {"providers": {ROUTE: build_route()}}},
        # web_search uses DeepSeek's search API; without a DeepSeek key it can only fail.
        *([] if os.environ.get("DEEPSEEK_API_KEY") else [{"id": "tool-web", "config": {"fetch": True, "search": False}}]),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(patch, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
