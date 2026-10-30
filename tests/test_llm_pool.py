from __future__ import annotations

import pytest

from app.clients.llm import LLMRequestError
from app.clients.llm_pool import LLMModelPool


class FakeLLM:
    configured = True

    def __init__(self, result: str = "ok", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def answer(self, question, evidences, answer_prompt=""):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_model_pool_fails_over_immediately_on_upstream_429() -> None:
    primary = FakeLLM(error=LLMRequestError("busy", retryable=True, status_code=429, retry_after=30))
    backup = FakeLLM(result="backup answer")
    pool = LLMModelPool([("primary", primary), ("backup", backup)], "answer")

    assert pool.answer("question", []) == "backup answer"
    assert primary.calls == 1
    assert backup.calls == 1

    assert pool.answer("question 2", []) == "backup answer"
    assert primary.calls == 1
    assert backup.calls == 2


def test_model_pool_does_not_hide_authentication_error() -> None:
    primary = FakeLLM(error=LLMRequestError("unauthorized", retryable=False, status_code=401))
    backup = FakeLLM(result="must not run")
    pool = LLMModelPool([("primary", primary), ("backup", backup)], "planner")

    with pytest.raises(LLMRequestError):
        pool.answer("question", [])
    assert backup.calls == 0
