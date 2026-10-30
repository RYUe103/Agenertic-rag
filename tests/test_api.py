from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.config_store import FileSecretStore
from app.auth import hash_password
from app.main import _source_base_url, create_app


def offline_settings(tmp_path) -> Settings:
    return Settings(
        app_data_dir=tmp_path / "data",
        app_mock_external_services=True,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        auth_enabled=False,
        admin_password_hash=hash_password("test-password"),
        ingest_worker_count=1,
        ingest_max_retries=0,
    )


def test_source_base_url_preserves_reverse_proxy_port_and_prefix(tmp_path) -> None:
    request = Request({
        "type": "http", "method": "POST", "scheme": "http", "path": "/api/agents/default/ask",
        "raw_path": b"/api/agents/default/ask", "query_string": b"",
        "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"10.2.108.71"),
            (b"origin", b"http://10.2.108.71:1030"),
            (b"referer", b"http://10.2.108.71:1030/kb/"),
        ],
    })

    assert _source_base_url(request, offline_settings(tmp_path)) == "http://10.2.108.71:1030/kb/"


def test_source_base_url_prefers_configured_public_url(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    settings.app_public_base_url = "http://10.2.108.71:1030/kb"
    request = Request({
        "type": "http", "method": "POST", "scheme": "http", "path": "/api/ask",
        "raw_path": b"/api/ask", "query_string": b"", "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 12345), "headers": [(b"host", b"127.0.0.1:8765")],
    })

    assert _source_base_url(request, settings) == "http://10.2.108.71:1030/kb/"


def test_login_protects_application_api(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/agents").status_code == 401
        assert client.post("/api/auth/login", json={"username": "sansuokb", "password": "wrong"}).status_code == 401

        login = client.post("/api/auth/login", json={"username": "sansuokb", "password": "test-password"})
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        assert client.get("/api/agents").status_code == 200

        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/agents").status_code == 401


def test_external_api_key_is_scoped_to_read_only_qa(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    settings.external_api_enabled = True
    settings.external_api_key = "three-institute-secret"
    app = create_app(settings)
    payload = {"question": "测试检索", "conversation_id": "external-test", "history": []}

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.json()["external_api_configured"] is True

        missing = client.post("/api/agents/default/retrieve", json=payload)
        assert missing.status_code == 401
        assert missing.json()["detail"] == "API Key 无效或缺失"

        wrong = client.post(
            "/api/agents/default/retrieve",
            json=payload,
            headers={"Authorization": "Bearer wrong"},
        )
        assert wrong.status_code == 401

        authorized = client.post(
            "/api/agents/default/retrieve",
            json=payload,
            headers={"Authorization": "Bearer three-institute-secret"},
        )
        assert authorized.status_code == 200
        assert authorized.json()["agent_id"] == "default"

        answer = client.post(
            "/api/agents/default/ask",
            json=payload,
            headers={"Authorization": "Bearer three-institute-secret"},
        )
        assert answer.status_code == 200
        assert answer.json()["agent_id"] == "default"

        agents = client.get(
            "/api/agents",
            headers={"Authorization": "Bearer three-institute-secret"},
        )
        assert agents.status_code == 200
        assert agents.json()[0]["agent_id"] == "default"

        admin = client.get(
            "/api/admin/knowledge-bases",
            headers={"Authorization": "Bearer three-institute-secret"},
        )
        assert admin.status_code == 401
        assert admin.json()["detail"] == "请先登录"


def test_linux_file_secret_store_persists_and_hides_values(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    FileSecretStore(path).set("kb:source-platform", "platform-secret")

    assert FileSecretStore(path).get("kb:source-platform") == "platform-secret"
    assert path.stat().st_mode & 0o777 == 0o600


def test_health_and_ask(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["build_version"] == "2026.10.21-glossary-aho-corasick.1"
        assert health.json()["ask_concurrency_limit"] == 40
        assert health.json()["agent_engine"] == "langgraph"
        response = client.post("/api/ask", json={"question": "不存在的内容"})
        assert response.status_code == 200
        assert response.json()["evidence_insufficient"] is True
        assert response.json()["trace"]
        retrieval = client.post("/api/retrieve", json={"question": "不存在的内容"})
        assert retrieval.status_code == 200
        assert retrieval.json()["knowledge_base_id"] == "default-kb"
        assert "answer" not in retrieval.json()
        assert retrieval.json()["answer_prompt"]


def test_upload_preserves_original_filename(tmp_path, sample_pdf) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_pdf.open("rb") as handle:
        response = client.post("/api/ingest", files=[("files", ("原始 名称.pdf", handle, "application/pdf"))])
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["items"][0]["filename"] == "原始 名称.pdf"
        sources = client.get("/api/sources").json()
        assert sources[0]["filename"] == "原始 名称.pdf"
        answer = client.post("/api/ask", json={"question": "GB/T 1234-2024 的 Temperature 要求是什么？"})
        assert answer.status_code == 200
        assert answer.json()["evidence_insufficient"] is True


def test_sources_page_and_markdown_preview(tmp_path, sample_txt) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_txt.open("rb") as handle:
        response = client.post("/api/ingest", files=[("files", (sample_txt.name, handle, "text/plain"))])
        assert response.status_code == 202
        page = client.get("/api/agents/default/sources/page?page=1&page_size=15").json()
        assert page["total"] == 1
        assert page["total_pages"] == 1
        source_id = page["items"][0]["source_id"]
        preview = client.get(f"/api/agents/default/sources/{source_id}/markdown?version=document")
        assert preview.status_code == 200
        assert "预算说明" in preview.json()["markdown"]
        assert set(preview.json()["available_versions"]) == {"document", "extracted"}
        assert client.get(
            f"/api/agents/default/sources/{source_id}/markdown?version=../../etc/passwd"
        ).status_code == 404


def _wait_for_delete_job(client: TestClient, job_id: str) -> dict:
    job = {}
    for _ in range(200):
        job = client.get(f"/api/delete-jobs/{job_id}").json()
        if job["status"] == "completed":
            return job
        time.sleep(0.02)
    raise AssertionError(f"delete job did not complete: {job}")


def test_bulk_clear_requires_kb_name_and_deletes_managed_sources(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        for name, body in (("first.txt", b"first"), ("second.txt", b"second")):
            response = client.post("/api/agents/default/ingest", files=[("files", (name, body, "text/plain"))])
            assert response.status_code == 202

        sources = client.get("/api/agents/default/sources").json()
        source_dirs = [Path(item["local_source_path"]).parent for item in sources]
        assert len(sources) == 2

        wrong = client.post(
            "/api/agents/default/sources/delete-all", json={"confirmation": "错误名称"}
        )
        assert wrong.status_code == 400
        assert len(client.get("/api/agents/default/sources").json()) == 2

        kb_name = client.get("/api/admin/knowledge-bases").json()[0]["name"]
        started = client.post(
            "/api/agents/default/sources/delete-all", json={"confirmation": kb_name}
        )
        assert started.status_code == 202
        completed = _wait_for_delete_job(client, started.json()["job_id"])

        assert completed["total"] == 2
        assert completed["succeeded"] == 2
        assert completed["failed"] == 0
        assert client.get("/api/agents/default/sources").json() == []
        assert all(not path.exists() for path in source_dirs)


def test_bulk_clear_keeps_local_source_when_dify_delete_fails(tmp_path) -> None:
    class FailingDeleteDify:
        dataset_id = "fail-dataset"

        def delete_document(self, _document_id: str) -> None:
            raise RuntimeError("Dify refused deletion")

    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/agents/default/ingest",
            files=[("files", ("keep.txt", b"keep this file", "text/plain"))],
        )
        assert response.status_code == 202
        runtime = app.state.registry.default()
        source = runtime.repository.list_all()[0]
        source = runtime.repository.update(
            source.source_id,
            dify_dataset_id="fail-dataset",
            dify_document_id="real-document",
        )
        runtime.ingestion.dify = FailingDeleteDify()

        kb_name = client.get("/api/admin/knowledge-bases").json()[0]["name"]
        started = client.post(
            "/api/agents/default/sources/delete-all", json={"confirmation": kb_name}
        )
        completed = _wait_for_delete_job(client, started.json()["job_id"])

        assert completed["failed"] == 1
        assert "Dify refused deletion" in completed["items"][0]["message"]
        assert runtime.repository.get(source.source_id).filename == "keep.txt"
        assert Path(source.source_path).exists()


def test_bulk_clear_only_affects_selected_agent_knowledge_base(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        assert client.post(
            "/api/admin/knowledge-bases",
            json={
                "knowledge_base_id": "second-kb",
                "name": "第二资料库",
                "base_url": "https://api.dify.ai",
                "dataset_id": "mock-second",
                "api_key": "test-key",
                "use_legacy_data": False,
            },
        ).status_code == 200
        assert client.post(
            "/api/admin/agents",
            json={
                "agent_id": "second",
                "name": "第二助手",
                "knowledge_base_id": "second-kb",
                "allowed_tools": ["vector_search"],
            },
        ).status_code == 200
        assert client.post(
            "/api/agents/default/ingest",
            files=[("files", ("default.txt", b"default body", "text/plain"))],
        ).status_code == 202
        assert client.post(
            "/api/agents/second/ingest",
            files=[("files", ("second.txt", b"second body", "text/plain"))],
        ).status_code == 202

        started = client.post(
            "/api/agents/second/sources/delete-all", json={"confirmation": "第二资料库"}
        )
        completed = _wait_for_delete_job(client, started.json()["job_id"])

        assert completed["succeeded"] == 1
        assert client.get("/api/agents/second/sources").json() == []
        assert [item["filename"] for item in client.get("/api/agents/default/sources").json()] == ["default.txt"]


def test_bulk_clear_cancels_unstarted_ingest_job(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/agents/default/ingest-jobs", json={"filenames": ["pending.txt"]}
        )
        assert created.status_code == 201
        kb_name = client.get("/api/admin/knowledge-bases").json()[0]["name"]
        response = client.post(
            "/api/agents/default/sources/delete-all", json={"confirmation": kb_name}
        )
        assert response.status_code == 202
        assert response.json()["status"] == "completed"
        job = client.get(f"/api/jobs/{created.json()['job_id']}").json()
        assert job["status"] == "completed"
        assert job["items"][0]["status"] == "failed"
        assert "资料库清空" in job["items"][0]["message"]


def test_upload_form_enables_rewrite_preview(tmp_path, sample_txt) -> None:
    class PreviewLLM:
        configured = True

        def rewrite_markdown(self, filename: str, markdown: str, user_prompt: str = "") -> str:
            return f"# 改写预览\n\n{user_prompt}\n\n{markdown}"

    app = create_app(offline_settings(tmp_path))
    app.state.registry.default().ingestion.llm = PreviewLLM()
    with TestClient(app) as client, sample_txt.open("rb") as handle:
        response = client.post(
            "/api/ingest",
            files=[("files", (sample_txt.name, handle, "text/plain"))],
            data={
                "rewrite_enabled": "true",
                "rewrite_review_required": "true",
                "rewrite_prompt": "保留金额",
                "workflow_enabled": "false",
            },
        )
        assert response.status_code == 202
        job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
        assert job["items"][0]["status"] == "preview_ready"
        source = client.get("/api/sources").json()[0]
        assert source["rewrite_status"] == "preview_ready"
        assert source["dify_document_id"] is None


def test_uploads_docx_and_txt_and_serves_original_files(tmp_path, sample_docx, sample_txt) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_docx.open("rb") as docx_handle, sample_txt.open("rb") as txt_handle:
        response = client.post("/api/ingest", files=[
            ("files", (sample_docx.name, docx_handle, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ("files", (sample_txt.name, txt_handle, "text/plain")),
        ])
        assert response.status_code == 202
        job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
        assert [item["status"] for item in job["items"]] == ["success", "success"]
        sources = client.get("/api/sources").json()
        assert {item["document_type"] for item in sources} == {"docx", "txt"}
        for source in sources:
            downloaded = client.get(f"/api/files/{source['source_id']}")
            assert downloaded.status_code == 200
            assert downloaded.content


def test_persistent_ingest_job_uploads_in_batches_and_completes(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/agents/default/ingest-jobs",
            json={"filenames": ["first.txt", "second.txt"]},
        )
        assert created.status_code == 201
        job_id = created.json()["job_id"]

        first_batch = client.post(
            f"/api/jobs/{job_id}/upload-batch",
            files=[("files", ("first.txt", "first body".encode(), "text/plain"))],
            data={"positions": "[0]"},
        )
        assert first_batch.status_code == 200
        assert first_batch.json()["uploaded"] == 1

        incomplete = client.post(f"/api/jobs/{job_id}/start")
        assert incomplete.status_code == 409
        assert "1 个文件未上传" in incomplete.json()["detail"]

        second_batch = client.post(
            f"/api/jobs/{job_id}/upload-batch",
            files=[("files", ("second.txt", "second body".encode(), "text/plain"))],
            data={"positions": "[1]"},
        )
        assert second_batch.status_code == 200
        assert second_batch.json()["uploaded"] == 2
        assert client.post(f"/api/jobs/{job_id}/start").status_code == 200

        job = {}
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "completed":
                break
            time.sleep(0.03)

        assert job["status"] == "completed"
        assert job["succeeded"] == 2
        assert job["failed"] == 0
        sources = client.get("/api/sources").json()
        assert {source["filename"] for source in sources} == {"first.txt", "second.txt"}
        assert len(sources) == 2
        assert not any(settings_path.is_file() for settings_path in app.state.settings.ingest_staging_dir.rglob("*"))


def test_rejects_unsupported_upload(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post("/api/ingest", files=[("files", ("archive.xlsx", b"data", "application/octet-stream"))])
    assert response.status_code == 400
    assert "PDF、DOC、DOCX、TXT" in response.json()["detail"]


def test_multiple_agents_are_isolated(tmp_path, sample_pdf, monkeypatch) -> None:
    config = tmp_path / "agents.yaml"
    config.write_text("""
default_agent: first
agents:
  - id: first
    name: 第一个
    use_legacy_data: true
    allowed_tools: [vector_search, grep_files]
  - id: second
    name: 第二个
    allowed_tools: [vector_search]
""", encoding="utf-8")
    settings = offline_settings(tmp_path)
    settings.agent_config_path = config
    app = create_app(settings)

    with TestClient(app) as client, sample_pdf.open("rb") as handle:
        agents = client.get("/api/agents").json()
        assert [item["agent_id"] for item in agents] == ["first", "second"]
        assert agents[0]["is_default"] is True

        uploaded = client.post(
            "/api/agents/first/ingest",
            files=[("files", ("only-first.pdf", handle, "application/pdf"))],
        )
        assert uploaded.status_code == 202
        assert len(client.get("/api/agents/first/sources").json()) == 1
        assert client.get("/api/agents/second/sources").json() == []
        assert client.get("/api/sources").json()[0]["filename"] == "only-first.pdf"

        response = client.post("/api/agents/second/ask", json={"question": "内容"})
        assert response.status_code == 200
        assert response.json()["agent_id"] == "second"
        assert client.post("/api/agents/missing/ask", json={"question": "内容"}).status_code == 404


def test_admin_configuration_is_hot_reloaded_and_secrets_are_hidden(tmp_path, sample_pdf) -> None:
    app = create_app(offline_settings(tmp_path))
    knowledge_base = {
        "knowledge_base_id": "engineering-kb",
        "name": "工程知识库",
        "description": "测试隔离知识库",
        "answer_prompt": "你是工程知识库的专用回答助手。",
        "base_url": "https://api.dify.ai",
        "dataset_id": "dataset-test",
        "api_key": "secret-that-must-not-be-returned",
        "search_method": "semantic_search",
        "source_platform_mode": "http",
        "source_platform_base_url": "https://files.example.test/api",
        "source_platform_api_key": "file-platform-secret",
        "enabled": True,
        "use_legacy_data": False,
    }
    agent = {
        "agent_id": "engineering",
        "name": "工程助手",
        "description": "只使用工程知识库",
        "knowledge_base_id": "engineering-kb",
        "planner_prompt": "先做向量检索，再按需精确检索。",
        "glossary_documents": [
            {"name": "工程术语.md", "markdown": "# 管嘴\n同义词：Nozzle", "entries": [], "enabled": True},
            {"name": "工程缩写.json", "markdown": "", "entries": [{"term": "PWR", "aliases": ["压水堆"], "data": {"专业": "核工程"}}], "enabled": True},
        ],
        "allowed_tools": ["vector_search", "grep_files"],
        "max_tool_calls": 5,
        "max_retrieval_rounds": 2,
        "max_evidence_for_answer": 8,
        "enabled": True,
        "is_default": True,
    }

    with TestClient(app) as client:
        tested = client.post("/api/admin/knowledge-bases/test", json=knowledge_base)
        assert tested.status_code == 200
        assert tested.json()["ok"] is True

        created_kb = client.post("/api/admin/knowledge-bases", json=knowledge_base)
        assert created_kb.status_code == 200
        assert created_kb.json()["api_key_configured"] is True
        assert created_kb.json()["answer_prompt"] == knowledge_base["answer_prompt"]
        assert created_kb.json()["answer_prompt_custom"] is True
        assert "api_key" not in created_kb.json()
        assert "secret-that-must-not-be-returned" not in created_kb.text
        assert created_kb.json()["source_platform_mode"] == "http"
        assert created_kb.json()["source_platform_base_url"] == "https://files.example.test/api"
        assert created_kb.json()["source_platform_api_key_configured"] is True
        assert "file-platform-secret" not in created_kb.text

        created_agent = client.post("/api/admin/agents", json=agent)
        assert created_agent.status_code == 200
        assert created_agent.json()["planner_prompt"] == agent["planner_prompt"]
        assert created_agent.json()["glossary_documents"] == agent["glossary_documents"]

        summaries = client.get("/api/agents").json()
        assert any(item["agent_id"] == "engineering" for item in summaries)
        assert next(item for item in summaries if item["agent_id"] == "engineering")["allowed_tools"] == [
            "vector_search", "grep_files"
        ]
        runtime = app.state.registry.get("engineering")
        assert runtime.profile.max_tool_calls == 5
        assert runtime.profile.planner_prompt == agent["planner_prompt"]
        assert runtime.agent.glossary_documents[0].name == "工程术语.md"
        assert runtime.agent.answer_prompt == knowledge_base["answer_prompt"]

        default_answer = client.post("/api/ask", json={"question": "测试默认 Agent"})
        assert default_answer.status_code == 200
        assert default_answer.json()["agent_id"] == "engineering"

        blocked_kb_delete = client.delete("/api/admin/knowledge-bases/engineering-kb")
        assert blocked_kb_delete.status_code == 409
        blocked_default_delete = client.delete("/api/admin/agents/engineering")
        assert blocked_default_delete.status_code == 409

        with sample_pdf.open("rb") as handle:
            uploaded = client.post(
                "/api/agents/engineering/ingest",
                files=[("files", ("engineering-only.pdf", handle, "application/pdf"))],
            )
        assert uploaded.status_code == 202
        assert len(client.get("/api/agents/engineering/sources").json()) == 1
        assert client.get("/api/agents/default/sources").json() == []

        listed = client.get("/api/admin/knowledge-bases")
        assert "secret-that-must-not-be-returned" not in listed.text
        assert "file-platform-secret" not in listed.text
        assert all("api_key" not in item for item in listed.json())


def test_model_connections_are_configurable_and_bound_to_agent(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    model = {
        "model_connection_id": "backup-one",
        "name": "备用模型一",
        "base_url": "http://model.internal:8000/v1",
        "api_key": "model-secret",
        "model": "qwen3-32b",
        "api_style": "chat_completions",
        "timeout_seconds": 20,
        "priority": 20,
        "enabled": True,
    }

    with TestClient(app) as client:
        created = client.post("/api/admin/model-connections", json=model)
        assert created.status_code == 200
        assert created.json()["api_key_configured"] is True
        assert "model-secret" not in created.text

        tested = client.post("/api/admin/model-connections/test", json={**model, "api_key": None})
        assert tested.status_code == 200
        assert tested.json()["ok"] is True

        agent = client.get("/api/admin/agents").json()[0]
        agent["planner_model_ids"] = ["backup-one"]
        agent["answer_model_ids"] = ["backup-one"]
        saved = client.put(f"/api/admin/agents/{agent['agent_id']}", json=agent)
        assert saved.status_code == 200
        assert saved.json()["planner_model_ids"] == ["backup-one"]
        assert saved.json()["answer_model_ids"] == ["backup-one"]
        runtime = app.state.registry.get(agent["agent_id"])
        assert runtime.agent.llm.clients[0][0] == "backup-one"
        assert runtime.agent.answer_llm.clients[0][0] == "backup-one"

        assert client.delete("/api/admin/model-connections/backup-one").status_code == 409
