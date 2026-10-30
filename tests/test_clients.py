from __future__ import annotations

import pytest

from app.clients.dify import DifyClient
from app.clients.llm import LLMClient, LLMRequestError


def test_dify_retrieval_uses_semantic_search(monkeypatch) -> None:
    client = DifyClient("https://api.example.test", "secret", "dataset")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured.update(kwargs)
        return {"records": []}

    monkeypatch.setattr(client, "_request", fake_request)

    client.retrieve("篮球比赛预算", 8)

    model = captured["json"]["retrieval_model"]
    assert model["search_method"] == "semantic_search"
    assert model["top_k"] == 8


def test_planner_exposes_specific_tool_guidance(monkeypatch) -> None:
    client = LLMClient("https://llm.example.test", "secret", "model")
    captured = {}

    def fake_post(path, body):
        captured.update(body)
        return {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"name": "grep_files", "arguments": '{"query":"预算 合计 元"}'},
                    }],
                },
            }],
        }

    monkeypatch.setattr(client, "_post", fake_post)

    call = client.plan("篮球比赛预算总额是多少？", ["vector_search", "grep_files", "find_files"])

    definitions = {item["function"]["name"]: item["function"]["description"] for item in captured["tools"]}
    parameters = {item["function"]["name"]: item["function"]["parameters"] for item in captured["tools"]}
    assert call is not None and call.name == "grep_files"
    assert "金额" in definitions["grep_files"]
    assert "纯语义向量" in definitions["vector_search"]
    assert "文件名" in definitions["find_files"]
    assert "合计" in parameters["grep_files"]["properties"]["query"]["description"]
    assert "只输入一个专有名词" in captured["messages"][0]["content"]
    assert "GPR是什么" in captured["messages"][0]["content"]
    assert "intent=search_content" in captured["messages"][0]["content"]


def test_planner_exposes_structured_schemas_for_new_tools(monkeypatch) -> None:
    client = LLMClient("https://llm.example.test", "secret", "model")
    captured = {}

    def fake_post(path, body):
        captured.update(body)
        return {"choices": [{"message": {"tool_calls": []}}]}

    monkeypatch.setattr(client, "_post", fake_post)
    tools = [
        "search_within_document", "metadata_search", "read_pages", "get_adjacent_chunks",
        "get_document_outline", "compare_document_metadata",
    ]
    client.plan("下一步", tools)

    definitions = {item["function"]["name"]: item["function"] for item in captured["tools"]}
    assert definitions["search_within_document"]["parameters"]["required"] == ["source_id", "query"]
    assert "publish_from" in definitions["metadata_search"]["parameters"]["properties"]
    assert definitions["read_pages"]["parameters"]["properties"]["pages"]["type"] == "array"
    assert "segment_id" in definitions["get_adjacent_chunks"]["parameters"]["required"]
    assert definitions["get_document_outline"]["parameters"]["required"] == ["source_id"]
    assert definitions["compare_document_metadata"]["parameters"]["properties"]["source_ids"]["minItems"] == 2


def test_planner_does_not_swallow_non_retryable_llm_error(monkeypatch) -> None:
    client = LLMClient("https://llm.example.test", "bad-key", "model")

    def fail(_path, _body):
        raise LLMRequestError("unauthorized", retryable=False, status_code=401)

    monkeypatch.setattr(client, "_post", fail)
    with pytest.raises(LLMRequestError):
        client.plan("问题", ["vector_search"])
