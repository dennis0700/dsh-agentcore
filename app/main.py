"""AgentCore Runtime entrypoint that drives DeepSeek Harness through its Python SDK."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
from typing import Any, AsyncIterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

from .config import Settings
from .events import run_error, to_client_events
from .harness import HarnessManager, SessionBusyError

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("dsh_agent")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")

settings = Settings.from_env()
manager = HarnessManager(settings)


@contextlib.asynccontextmanager
async def lifespan(_app):
    yield
    await asyncio.to_thread(manager.shutdown)


app = BedrockAgentCoreApp(lifespan=lifespan)


class BadRequest(ValueError):
    pass


def _parse(payload: Any, context: RequestContext) -> tuple[str, str, bool]:
    if not isinstance(payload, dict):
        raise BadRequest("payload must be a JSON object")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest("'prompt' must be a non-empty string")
    if len(prompt) > settings.max_prompt_chars:
        raise BadRequest(f"'prompt' exceeds {settings.max_prompt_chars} characters")
    stream = payload.get("stream", True)
    if not isinstance(stream, bool):
        raise BadRequest("'stream' must be a boolean")
    session_id = context.session_id or settings.local_session_id
    if not SESSION_ID_RE.match(session_id):
        raise BadRequest("invalid runtime session id")
    return prompt, session_id, stream


async def _run(prompt: str, session_id: str) -> AsyncIterator[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(item: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def worker() -> None:
        task_id = app.add_async_task("dsh_run", {"session_id": session_id})
        try:
            session = manager.acquire(session_id)
        except SessionBusyError:
            emit({"type": "error", "code": "SESSION_BUSY",
                  "message": "another invocation is still running in this session"})
            app.complete_async_task(task_id)
            return
        except Exception as exc:
            log.exception("failed to start dsh session")
            emit({"type": "error", "code": "START_FAILED", "message": str(exc)})
            app.complete_async_task(task_id)
            return
        try:
            def forward(notification) -> None:
                for event in to_client_events(notification, session_id):
                    emit(event)

            result = manager.run(session, prompt, forward)
            manager.persist()
            final: dict[str, Any] = {
                "type": "final",
                "session_id": result.session_id,
                "final_response": result.final_response,
                "finish_reason": result.finish_reason,
            }
            error = run_error(result.events)
            if error:
                final["error"] = error
            emit(final)
        except Exception as exc:
            log.exception("dsh run failed")
            manager.persist()
            emit({"type": "error", "code": "RUN_FAILED", "message": str(exc)})
        finally:
            manager.release(session_id)
            app.complete_async_task(task_id)

    threading.Thread(target=worker, name=f"dsh-run-{session_id[:12]}", daemon=True).start()

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=settings.heartbeat_seconds)
        except asyncio.TimeoutError:
            yield {"type": "heartbeat"}
            continue
        yield item
        if item["type"] in ("final", "error"):
            return


@app.entrypoint
async def invoke(payload, context: RequestContext):
    try:
        prompt, session_id, stream = _parse(payload, context)
    except BadRequest as exc:
        return {"type": "error", "code": "BAD_REQUEST", "message": str(exc)}

    if stream:
        return _run(prompt, session_id)

    last: dict[str, Any] = {"type": "error", "code": "NO_RESULT", "message": "run produced no result"}
    async for item in _run(prompt, session_id):
        if item["type"] in ("final", "error"):
            last = item
    return last


if __name__ == "__main__":
    # AgentCore requires 0.0.0.0:8080. The overrides exist for local tests only.
    app.run(host=os.environ.get("AGENT_HOST", "0.0.0.0"), port=int(os.environ.get("AGENT_PORT", "8080")))
