"""Own the single DeepSeekHarness process that serves this microVM."""
from __future__ import annotations

import logging
import threading
from typing import Callable

from deepseek_harness import (
    DeepSeekHarness,
    DeepSeekHarnessConfig,
    Notification,
    RunResult,
    Session,
    UserQuestionAnswer,
    UserQuestionAnswerItem,
    UserQuestionRequest,
)
from deepseek_harness.errors import JsonRpcError, TransportClosedError

from . import state
from .config import Settings

log = logging.getLogger(__name__)

NO_USER_REPLY = (
    "No interactive user is available in this deployment. "
    "Proceed with your best judgment and state the assumptions you made."
)


class SessionBusyError(RuntimeError):
    pass


def _answer_without_user(request: UserQuestionRequest, _cancelled: threading.Event) -> UserQuestionAnswer:
    return UserQuestionAnswer(answers=[
        UserQuestionAnswerItem(id=str(q["id"]), selected=[], custom=NO_USER_REPLY)
        for q in request.questions
    ])


class HarnessManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._harness: DeepSeekHarness | None = None
        self._sessions: dict[str, Session] = {}
        self._busy: set[str] = set()

    def _prepare_dirs(self) -> None:
        s = self.settings
        s.project_dir.mkdir(parents=True, exist_ok=True)
        s.state_dir.mkdir(parents=True, exist_ok=True)
        s.dsh_home.mkdir(parents=True, exist_ok=True)

    def _start_harness(self) -> DeepSeekHarness:
        s = self.settings
        self._prepare_dirs()
        if not state.has_sessions(s.dsh_home):
            state.restore(s.state_dir, s.dsh_home)
        harness = DeepSeekHarness(DeepSeekHarnessConfig(
            provider=s.provider,
            model=s.model,
            reasoning_effort=s.reasoning_effort,
            max_tokens=s.max_tokens,
            cwd=str(s.project_dir),
            runtime_cwd=str(s.project_dir),
            profile=s.profile,
            patches=s.patches,
            dsh_home=str(s.dsh_home),
            initialize_timeout_seconds=float(s.initialize_timeout_seconds),
            on_user_question=_answer_without_user,
        ))
        harness.start()
        log.info("dsh runtime started (profile=%s model=%s)", s.profile, s.model)
        return harness

    def _open_session(self, harness: DeepSeekHarness, session_id: str) -> Session:
        if state.has_sessions(self.settings.dsh_home):
            try:
                session = harness.resume_session(session_id)
                log.info("resumed dsh session %s", session_id)
                return session
            except JsonRpcError as exc:
                log.info("no resumable dsh session %s (%s); starting new", session_id, exc)
        return harness.start_session(session_id)

    def acquire(self, session_id: str) -> Session:
        """Reserve the session for one run. Raises SessionBusyError if a run is active."""
        with self._lock:
            if session_id in self._busy:
                raise SessionBusyError(session_id)
            self._busy.add(session_id)
            try:
                if self._harness is None:
                    self._harness = self._start_harness()
                    self._sessions.clear()
                session = self._sessions.get(session_id)
                if session is None:
                    session = self._open_session(self._harness, session_id)
                    self._sessions[session_id] = session
                return session
            except BaseException:
                self._busy.discard(session_id)
                raise

    def release(self, session_id: str) -> None:
        with self._lock:
            self._busy.discard(session_id)

    def run(
        self,
        session: Session,
        prompt: str,
        on_notification: Callable[[Notification], None],
    ) -> RunResult:
        try:
            return session.run(prompt, on_notification=on_notification)
        except TransportClosedError:
            self._reset()
            raise

    def persist(self) -> None:
        with self._persist_lock:
            try:
                state.persist(self.settings.dsh_home, self.settings.state_dir)
            except OSError:
                log.exception("failed to persist dsh state to %s", self.settings.state_dir)

    def _reset(self) -> None:
        with self._lock:
            harness, self._harness = self._harness, None
            self._sessions.clear()
        if harness is not None:
            log.warning("dsh runtime exited; it will restart on the next invocation")
            try:
                harness.close()
            except Exception:
                log.exception("error closing dsh runtime")

    def shutdown(self) -> None:
        if self._harness is not None:
            self.persist()
        self._reset()
