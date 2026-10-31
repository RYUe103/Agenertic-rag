from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.clients.llm import LLMClient, LLMRequestError
from app.schemas import CitationCheck, Evidence, EvidenceGrade, GlossaryDocument, MetadataFieldDefinition, MetadataValue, ToolCall


logger = logging.getLogger("agentic_rag.llm")


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0


class LLMModelPool:
    """Ordered LLM failover with a small per-connection circuit breaker."""

    def __init__(
        self, clients: list[tuple[str, LLMClient]], role: str,
        failure_threshold: int = 3, cooldown_seconds: float = 60,
    ) -> None:
        self.clients = clients
        self.role = role
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self._states = {model_id: _CircuitState() for model_id, _client in clients}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return any(client.configured for _model_id, client in self.clients)

    def _available(self) -> list[tuple[str, LLMClient]]:
        now = time.monotonic()
        with self._lock:
            ready = [item for item in self.clients if self._states[item[0]].open_until <= now]
        return ready or self.clients[-1:]

    def _success(self, model_id: str) -> None:
        with self._lock:
            self._states[model_id] = _CircuitState()

    def _failure(self, model_id: str, exc: LLMRequestError) -> None:
        now = time.monotonic()
        with self._lock:
            state = self._states[model_id]
            state.failures += 1
            if exc.status_code == 429 and exc.retry_after:
                state.open_until = max(state.open_until, now + exc.retry_after)
            elif state.failures >= self.failure_threshold:
                state.open_until = now + self.cooldown_seconds

    def _call(self, method: str, *args: Any, retry_on_none: bool = False, **kwargs: Any) -> Any:
        available = [(model_id, client) for model_id, client in self._available() if client.configured]
        if not available:
            return None
        failures: list[str] = []
        for index, (model_id, client) in enumerate(available):
            try:
                result = getattr(client, method)(*args, **kwargs)
                if retry_on_none and result is None and index + 1 < len(available):
                    failures.append(f"{model_id}:返回无效结果")
                    logger.warning("llm_failover role=%s method=%s model=%s reason=invalid_result", self.role, method, model_id)
                    continue
                self._success(model_id)
                if index:
                    logger.info("llm_failover_success role=%s method=%s model=%s", self.role, method, model_id)
                return result
            except LLMRequestError as exc:
                if not exc.retryable:
                    raise
                self._failure(model_id, exc)
                failures.append(f"{model_id}:{exc}")
                logger.warning(
                    "llm_failover role=%s method=%s model=%s status=%s reason=%s",
                    self.role, method, model_id, exc.status_code or "network", str(exc)[:240],
                )
                if index + 1 >= len(available):
                    raise RuntimeError(f"{self.role} 模型链全部失败：{' | '.join(failures)}") from exc
        return None

    def extract_metadata(self, filename: str, markdown: str, fields: list[MetadataFieldDefinition], custom_prompt: str = "") -> dict[str, MetadataValue]:
        return self._call("extract_metadata", filename, markdown, fields, custom_prompt, retry_on_none=True) or {}

    def rewrite_markdown(self, filename: str, markdown: str, user_prompt: str = "") -> str:
        return self._call("rewrite_markdown", filename, markdown, user_prompt)

    def plan(self, question: str, available_tools: list[str], planner_prompt: str = "") -> ToolCall | None:
        return self._call("plan", question, available_tools, planner_prompt, retry_on_none=True)

    def route_specialists(self, question: str, candidates: list[dict[str, str]]) -> list[str] | None:
        return self._call("route_specialists", question, candidates, retry_on_none=True)

    def answer(self, question: str, evidences: list[Evidence], answer_prompt: str = "") -> str:
        return self._call("answer", question, evidences, answer_prompt)

    def react(self, messages, tools, max_tokens=2000):
        return self._call('react', messages, tools, max_tokens)

    def expand_query_with_glossary(self, question: str, documents: list[GlossaryDocument]) -> dict[str, Any] | None:
        return self._call("expand_query_with_glossary", question, documents)

    def grade_evidence(self, question: str, evidences: list[Evidence]) -> EvidenceGrade | None:
        return self._call("grade_evidence", question, evidences, retry_on_none=True)

    def verify_answer(self, question: str, answer: str, evidences: list[Evidence]) -> CitationCheck | None:
        return self._call("verify_answer", question, answer, evidences, retry_on_none=True)
