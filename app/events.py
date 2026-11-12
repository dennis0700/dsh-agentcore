"""Reduce dsh SDK notifications to the small event set streamed to callers.

Only root-session progress is forwarded. Tool arguments and tool output are
deliberately dropped so responses stay small and workspace data stays inside
the microVM.
"""
from __future__ import annotations

from typing import Any

from deepseek_harness import Notification


def _assistant_text(data: dict[str, Any]) -> str:
    message = data.get("message")
    owner = message if isinstance(message, dict) else data
    content = owner.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def run_error(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the error carried by the last root turn/end, if any."""
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
        error = reason.get("error")
        if isinstance(error, dict):
            return {"message": error.get("message"), "code": error.get("code")}
        return None
    return None


def to_client_events(notification: Notification, root_session_id: str) -> list[dict[str, Any]]:
    method = notification.method
    payload = notification.payload

    if method == "session.status":
        if payload.get("sessionId") == root_session_id:
            return [{"type": "status", "status": payload.get("status")}]
        return []

    if method == "subagent.started":
        return [{"type": "subagent", "state": "start", "child": payload.get("childSessionId")}]
    if method == "subagent.finished":
        return [{"type": "subagent", "state": "end", "child": payload.get("childSessionId")}]

    if method != "session.event" or payload.get("sessionId") != root_session_id:
        return []
    event = payload.get("event")
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if kind == "assistant/message":
        text = _assistant_text(data)
        return [{"type": "text", "text": text}] if text else []
    if kind == "tool/call":
        return [{"type": "tool", "state": "start", "name": data.get("name"), "call_id": data.get("callId")}]
    if kind == "tool/result":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        source = message.get("source") if isinstance(message.get("source"), dict) else {}
        return [{
            "type": "tool",
            "state": "end",
            "call_id": source.get("callId"),
            "is_error": bool(message.get("isError")),
        }]
    if kind == "turn/end":
        reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
        out: dict[str, Any] = {"type": "turn_end", "reason": reason.get("kind")}
        error = reason.get("error")
        if isinstance(error, dict) and error.get("message"):
            out["error"] = {"message": error.get("message"), "code": error.get("code")}
        return [out]
    return []
