from __future__ import annotations
import pytest

import time
import threading
import base64
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.config_store import FileSecretStore
from app.auth import SessionManager, hash_password
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


def test_knowledge_metadata_configuration_updates_runtime_and_rejects_duplicates(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        config = client.get('/api/admin/knowledge-bases').json()[0]
        config['metadata_fields'] = [{'key': 'project_name', 'label': '项目名称', 'value_type': 'string', 'description': '项目'}]
        config['metadata_ai_enabled'] = True
        config['metadata_extraction_prompt'] = '提取项目名称'
        url = '/api/admin/knowledge-bases/' + config['knowledge_base_id']
        response = client.put(url, json=config)
        assert response.status_code == 200
        assert response.json()['metadata_fields'] == config['metadata_fields']
        runtime = app.state.registry.get('@kb:' + config['knowledge_base_id'])
        assert runtime.ingestion.metadata_fields[0].key == 'project_name'
        assert runtime.ingestion.metadata_prompt == '提取项目名称'
        config['metadata_fields'].append(dict(config['metadata_fields'][0]))
        assert client.put(url, json=config).status_code == 422
        assert len(client.get('/api/admin/knowledge-bases').json()[0]['metadata_fields']) == 1


def test_knowledge_statistics_are_real_and_scoped_to_access(tmp_path, sample_txt) -> None:
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('sansuokb', True, 'local'))
        with sample_txt.open('rb') as handle:
            assert client.post('/api/ingest', files=[('files', (sample_txt.name, handle, 'text/plain'))]).status_code == 202
        result = client.get('/api/knowledge-bases/statistics')
        assert result.status_code == 200
        default = next(item for item in result.json() if item['knowledge_base_id'] == 'default-kb')
        assert default['documents']['total'] == 1 and default['documents']['indexed'] == 1
        assert default['chunk_count'] is None
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.get('/api/knowledge-bases/statistics').json() == []


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


def test_sandbox_policy_and_run_records_are_scoped_to_users(tmp_path, monkeypatch) -> None:
    from app.sandbox import SandboxStore

    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    monkeypatch.setattr(SandboxStore, '_execute', staticmethod(lambda *args: ('done', '', 'success', [('report.txt', b'private')])) )
    payload = {'name': 'analysis', 'language': 'python', 'code': 'print(1)'}
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('sansuokb', True, 'local'))
        policy = client.get('/api/admin/sandbox/policy').json()
        policy['enabled'] = True
        assert client.put('/api/admin/sandbox/policy', json=policy).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.get('/api/admin/sandbox/policy').status_code == 403
        assert client.post('/api/sandbox/runs', json=payload).status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create('sansuokb', True, 'local'))
        policy['allow_users'] = True
        assert client.put('/api/admin/sandbox/policy', json=policy).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        created = client.post('/api/sandbox/runs', json=payload)
        assert created.status_code == 201
        run_id = created.json()['id']
        artifact_id = created.json()['artifacts'][0]['id']
        assert client.get(f'/api/sandbox/runs/{run_id}').json()['stdout'] == 'done'
        assert client.get(f'/api/sandbox/runs/{run_id}/artifacts/{artifact_id}').content == b'private'
        client.cookies.set(sessions.cookie_name, sessions.create('bob', False, 'ldap'))
        assert client.get('/api/sandbox/runs').json() == []
        assert client.get(f'/api/sandbox/runs/{run_id}').status_code == 404
        assert client.get(f'/api/sandbox/runs/{run_id}/artifacts/{artifact_id}').status_code == 404


def test_sandbox_document_selection_checks_knowledge_base_authorization(tmp_path, monkeypatch) -> None:
    from app.sandbox import SandboxStore

    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    captured = []
    monkeypatch.setattr(SandboxStore, '_execute', staticmethod(lambda command, container, payload, timeout, marker: (captured.append(payload) or '', '', 'success', [])))
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('sansuokb', True, 'local'))
        policy = client.get('/api/admin/sandbox/policy').json()
        policy.update(enabled=True, allow_users=True, allow_documents=True)
        assert client.put('/api/admin/sandbox/policy', json=policy).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.post('/api/admin/knowledge-bases', json={'knowledge_base_id': 'alice-kb', 'name': 'Alice', 'dataset_id': 'dataset', 'api_key': 'key'}).status_code == 200
        directory = tmp_path / 'document'
        directory.mkdir()
        (directory / 'document.md').write_text('secret document', encoding='utf-8')
        app.state.registry.get('@kb:alice-kb').repository.create({'source_id': 'src_one', 'filename': 'one.pdf', 'normalized_filename': 'one.pdf', 'file_hash': 'one', 'local_pdf_path': str(directory / 'source.pdf'), 'local_md_path': str(directory / 'document.md')})
        payload = {'name': 'read', 'language': 'python', 'code': 'pass', 'agent_id': '@kb:alice-kb', 'source_ids': ['src_one']}
        assert client.post('/api/sandbox/runs', json=payload).status_code == 201
        assert 'secret document' in captured[0]
        client.cookies.set(sessions.cookie_name, sessions.create('bob', False, 'ldap'))
        assert client.post('/api/sandbox/runs', json=payload).status_code == 403
        assert len(captured) == 1


def test_knowledge_base_owner_and_dify_credentials(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    settings.dify_base_url = "https://dify.example/v1"
    settings.dify_dataset_id = "global-dataset"
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    value = {"knowledge_base_id": "alice-kb", "name": "Alice", "dataset_id": "alice-dataset", "api_key": "alice-key", "use_legacy_data": True}
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        created = client.post("/api/admin/knowledge-bases", json=value)
        assert created.status_code == 200
        assert created.json()["dataset_id"] == "alice-dataset"
        assert created.json()["owner_username"] == "alice"
        assert client.get("/api/knowledge-bases").json()[0]["can_write_documents"] is True
        row = app.state.registry.store.get_knowledge_base("alice-kb")
        assert not row["use_legacy_data"]
        assert app.state.registry.store.secrets.get("alice-kb") == "alice-key"
        client.cookies.set(sessions.cookie_name, sessions.create("bob", False, "ldap"))
        assert client.post("/api/admin/knowledge-bases", json=value).status_code == 400
        assert client.put("/api/admin/knowledge-bases/alice-kb", json=value).status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create("sansuokb", True, "local"))
        updated = client.put("/api/admin/knowledge-bases/alice-kb", json=value)
        assert updated.status_code == 200
        assert updated.json()["owner_username"] == "alice"


def test_shared_agent_routes_are_scoped_to_selected_knowledge_base(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        created = client.post("/api/admin/knowledge-bases", json={
            "knowledge_base_id": "alice-kb", "name": "Alice", "dataset_id": "alice-dataset", "api_key": "alice-key",
        })
        assert created.status_code == 200
        bases = client.get("/api/knowledge-bases").json()
        assert bases == [{"knowledge_base_id": "alice-kb", "name": "Alice", "runtime_id": "@kb:alice-kb", "can_write_documents": True}]
        assert client.get("/api/agents/%40kb%3Aalice-kb/sources/page").status_code == 200
        response = client.post("/api/agents/%40kb%3Aalice-kb/retrieve", json={"question": "测试"})
        assert response.status_code == 200
        assert response.json()["knowledge_base_id"] == "alice-kb"
        tools = client.get("/api/knowledge-bases/alice-kb/tools")
        assert tools.status_code == 200
        assert any(item["name"] == "find_files" and item["read_only"] for item in tools.json())
        tested = client.post("/api/knowledge-bases/alice-kb/tools/find_files/test", json={"query": "不存在"})
        assert tested.status_code == 200
        assert tested.json() == {"items": [], "count": 0}
        assert client.post("/api/knowledge-bases/alice-kb/tools/find_files/test", json={"unexpected": 1}).status_code == 422
        job = client.post("/api/agents/%40kb%3Aalice-kb/ingest-jobs", json={"filenames": ["test.txt"]})
        assert job.status_code == 201
        job_id = job.json()["job_id"]
        assert app.state.ingest_queue_repository.job_record(job_id)["agent_id"] == "@kb:alice-kb"
        assert client.get(f"/api/jobs/{job_id}").status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create("bob", False, "ldap"))
        assert client.get("/api/knowledge-bases").json() == []
        assert client.get("/api/agents/%40kb%3Aalice-kb/sources/page").status_code == 403
        assert client.get("/api/agents/%40kb%3Aalice-kb/sources/any/segments").status_code == 403
        assert client.get("/api/agents/%40kb%3Aalice-kb/sources/any/outline").status_code == 403
        assert client.get("/api/knowledge-bases/alice-kb/tools").status_code == 403
        assert client.get(f"/api/jobs/{job_id}").status_code == 403
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 403
        assert client.get("/api/sources").status_code == 403
        assert client.post("/api/ask", json={"question": "测试"}).status_code == 403
        assert client.delete("/api/admin/agents/default").status_code == 403
    restarted = create_app(settings)
    assert restarted.state.registry.get("@kb:alice-kb").profile.knowledge_base_id == "alice-kb"


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


def test_openai_compatible_api_reuses_agent_runtime(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    settings.external_api_enabled = True
    settings.external_api_key = "openai-secret"
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get('/v1/models').status_code == 401
        models = client.get('/v1/models', headers={'Authorization': 'Bearer openai-secret'})
        assert models.status_code == 200 and models.json()['data'][0]['id'] == 'default'
        response = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer openai-secret'}, json={
            'model': 'default', 'messages': [{'role': 'user', 'content': '测试检索'}], 'stream': False,
        })
        assert response.status_code == 200
        assert response.json()['object'] == 'chat.completion'
        assert response.json()['choices'][0]['message']['role'] == 'assistant'
        assert 'usage' not in response.json()
        headers = {'Authorization': 'Bearer openai-secret'}
        url = '/v1/chat/completions'
        message = [{'role': 'user', 'content': '测试'}]
        assert client.post(url, headers=headers, json={'model': 'missing', 'messages': message}).status_code == 404
        assert client.post(url, headers=headers, json={'model': 'default', 'messages': message, 'stream': True}).status_code == 400
        assert client.post(url, headers=headers, json={'messages': [{'role': 'assistant', 'content': '测试'}]}).status_code == 400
        assert client.post(url, headers=headers, json={'messages': [{'role': [], 'content': '测试'}]}).status_code == 400
        assert client.post(url, headers=headers, json={'messages': [{'role': 'user', 'content': ['测试']}]}).status_code == 400


def test_write_document_tool_requires_write_permission_and_approval(tmp_path) -> None:
    settings = offline_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        agent = client.get('/api/admin/agents').json()[0]
        agent['allowed_tools'] = list(dict.fromkeys([*agent['allowed_tools'], 'delete_document']))
        assert client.put('/api/admin/agents/' + agent['agent_id'], json=agent).status_code == 200
        url = '/api/knowledge-bases/default-kb/tools/delete_document'
        args = {'source_id': 'src_missing'}
        assert client.post(url + '/test', json=args).status_code == 403
        approval = client.post(url + '/approvals', json=args)
        assert approval.status_code == 201
        assert client.post('/api/admin/tool-approvals/' + approval.json()['id'] + '/approve').status_code == 200
        response = client.post(url + '/test', headers={'X-Approval-Id': approval.json()['id']}, json=args)
        assert response.status_code == 404


def test_upload_document_tool_creates_async_ingest_job(tmp_path) -> None:
    import base64
    settings = offline_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        agent = client.get('/api/admin/agents').json()[0]
        agent['allowed_tools'] = list(dict.fromkeys([*agent['allowed_tools'], 'upload_document']))
        assert client.put('/api/admin/agents/' + agent['agent_id'], json=agent).status_code == 200
        url = '/api/knowledge-bases/default-kb/tools/upload_document'
        args = {'filename': 'tool-upload.txt', 'content_base64': base64.b64encode(b'hello from tool' * 3000).decode(), 'mime_type': 'text/plain'}
        approval = client.post(url + '/approvals', json=args)
        assert approval.status_code == 201
        approval_id = approval.json()['id']
        assert client.post('/api/admin/tool-approvals/' + approval_id + '/approve').status_code == 200
        response = client.post(url + '/test', headers={'X-Approval-Id': approval_id}, json=args)
        assert response.status_code == 200
        assert response.json()['status'] == 'ingest_queued'


def test_document_status_tool_returns_real_status(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        agent = client.get('/api/admin/agents').json()[0]
        agent['allowed_tools'] = list(dict.fromkeys([*agent['allowed_tools'], 'get_document_status']))
        assert client.put('/api/admin/agents/' + agent['agent_id'], json=agent).status_code == 200
        repo = app.state.registry.get('@kb:default-kb').repository
        repo.create({'source_id': 'src_status', 'filename': 'status.txt', 'normalized_filename': 'status.txt',
                     'file_hash': 'status', 'local_pdf_path': str(tmp_path / 'status.txt'), 'local_md_path': str(tmp_path / 'status.md')})
        result = client.post('/api/knowledge-bases/default-kb/tools/get_document_status/test', json={'source_id': 'src_status'})
        assert result.status_code == 200
        assert result.json()['items'][0]['parse_status'] == 'pending'


def test_extract_table_and_export_csv_are_real_tools(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        agent = client.get('/api/admin/agents').json()[0]
        agent['allowed_tools'] = list(dict.fromkeys([*agent['allowed_tools'], 'extract_table', 'export_csv', 'export_excel', 'analyze_data']))
        assert client.put('/api/admin/agents/' + agent['agent_id'], json=agent).status_code == 200
        repo = app.state.registry.get('@kb:default-kb').repository
        md = tmp_path / 'table.md'; md.write_text('| 名称 | 数值 |\n| --- | ---: |\n| A | 1 |\n', encoding='utf-8')
        repo.create({'source_id': 'src_table', 'filename': 'table.md', 'normalized_filename': 'table.md',
                     'file_hash': 'table', 'local_pdf_path': str(tmp_path / 'table.pdf'), 'local_md_path': str(md)})
        table = client.post('/api/knowledge-bases/default-kb/tools/extract_table/test', json={'source_id': 'src_table'})
        assert table.status_code == 200
        assert '| 名称 | 数值 |' in table.json()['items'][0]['content']
        exported = client.post('/api/knowledge-bases/default-kb/tools/export_csv/test', json={
            'columns': ['名称', '数值'], 'rows': [['A', 1], ['B', 2]],
        })
        assert exported.status_code == 200
        assert exported.json()['items'][0]['content'] == '名称,数值\r\nA,1\r\nB,2\r\n'
        excel = client.post('/api/knowledge-bases/default-kb/tools/export_excel/test', json={
            'filename': 'rows.xlsx', 'columns': ['名称', '数值'], 'rows': [['A', 1]],
        })
        assert excel.status_code == 200
        assert excel.json()['items'][0]['filename'] == 'rows.xlsx'
        assert base64.b64decode(excel.json()['items'][0]['content_base64']).startswith(b'PK')
        analysis = client.post('/api/knowledge-bases/default-kb/tools/analyze_data/test', json={
            'columns': ['名称', '数值'], 'rows': [['A', 1], ['B', 3], ['C', '']]
        })
        assert analysis.status_code == 200
        assert analysis.json()['items'][0]['row_count'] == 3
        assert analysis.json()['items'][0]['columns'][1]['average'] == 2.0


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
        assert health.json()["build_version"] == "2026.10.31-semi-platform-ui.1"
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


def test_upload_recovery_persists_sizes_and_rejects_mismatch(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        path = '/api/agents/default/ingest-jobs'
        assert client.post(path, json={'filenames': ['one.txt'], 'file_sizes': []}).status_code == 400
        created = client.post(path, json={'filenames': ['one.txt'], 'file_sizes': [5]})
        job_id = created.json()['job_id']
        saved = client.get(f'/api/jobs/{job_id}').json()
        assert saved['agent_id'] == 'default' and saved['items'][0]['expected_size'] == 5
        upload = f'/api/jobs/{job_id}/upload-batch'
        assert client.post(upload, data={'positions': '[0]'}, files=[('files', ('one.txt', b'shorter', 'text/plain'))]).status_code == 400
        assert client.get(f'/api/jobs/{job_id}').json()['items'][0]['status'] == 'pending'
        assert client.post(upload, data={'positions': '[0]'}, files=[('files', ('one.txt', b'hello', 'text/plain'))]).status_code == 200


def test_upload_batch_can_be_retried_before_start(tmp_path) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post('/api/agents/default/ingest-jobs', json={'filenames': ['one.txt', 'two.txt']})
        job_id = created.json()['job_id']
        path = f'/api/jobs/{job_id}/upload-batch'
        assert client.post(path, data={'positions': '[0]'}, files=[('files', ('one.txt', b'first', 'text/plain'))]).status_code == 200
        retried = client.post(path, data={'positions': '[0]'}, files=[('files', ('one.txt', b'first', 'text/plain'))])
        assert retried.status_code == 200 and retried.json()['uploaded'] == 1
        second = client.post(path, data={'positions': '[1]'}, files=[('files', ('two.txt', b'second', 'text/plain'))])
        assert second.status_code == 200 and second.json()['uploaded'] == 2
        assert client.post(f'/api/jobs/{job_id}/cancel').json()['status'] == 'cancelled'
        assert client.post(path, data={'positions': '[1]'}, files=[('files', ('two.txt', b'second', 'text/plain'))]).status_code == 409


def test_sources_page_and_markdown_preview(tmp_path, sample_txt, monkeypatch) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_txt.open("rb") as handle:
        response = client.post("/api/ingest", files=[("files", (sample_txt.name, handle, "text/plain"))])
        assert response.status_code == 202
        page = client.get("/api/agents/default/sources/page?page=1&page_size=15").json()
        assert page["total"] == 1
        assert page["total_pages"] == 1
        source_id = page["items"][0]["source_id"]
        versions = client.get(f"/api/agents/default/sources/{source_id}/versions").json()
        assert len(versions) == 1 and versions[0]["version_no"] == 1
        snapshot = client.get(f"/api/agents/default/sources/{source_id}/versions/1")
        assert snapshot.status_code == 200 and "预算说明" in snapshot.json()["markdown"]
        assert client.get(f"/api/agents/default/sources/{source_id}/versions/999").status_code == 404
        preview = client.get(f"/api/agents/default/sources/{source_id}/markdown?version=document")
        assert preview.status_code == 200
        assert "预算说明" in preview.json()["markdown"]
        assert set(preview.json()["available_versions"]) == {"document", "extracted"}
        assert client.get(
            f"/api/agents/default/sources/{source_id}/markdown?version=../../etc/passwd"
        ).status_code == 404
        source = app.state.registry.get("default").repository.get(source_id)
        Path(source.local_md_path).write_text("<!-- page: 1 -->\n# 第一章\n内容\n<!-- page: 2 -->\n## 第二节\n索引正文", encoding="utf-8")
        outline = client.get(f"/api/agents/default/sources/{source_id}/outline")
        assert outline.status_code == 200
        assert "第一章" in outline.json()["outline"]
        assert [item["page"] for item in outline.json()["headings"]] == [1, 2]
        calls = []
        def segments(document_id, keyword, page, limit):
            calls.append((document_id, keyword, page, limit))
            return [{"id": "seg-1", "content": "索引正文", "position": 1, "status": "completed", "private": "hidden"}]
        monkeypatch.setattr(app.state.registry.get("default").dify, "get_segments", segments)
        result = client.get(f"/api/agents/default/sources/{source_id}/segments?page=2&page_size=1&keyword=索引")
        assert result.status_code == 200
        assert result.json()["items"][0]["content"] == "索引正文"
        assert result.json()["items"][0]["page"] == 2
        assert result.json()["items"][0]["chapter"] == "第一章 / 第二节"
        assert "private" not in result.text and result.json()["may_have_more"]
        assert calls == [(source.dify_document_id, "索引", 2, 1)]
        assert client.get("/api/agents/default/sources/missing/segments").status_code == 404


def test_reparse_job_runs_in_background_and_appears_in_tasks(tmp_path, sample_txt, monkeypatch) -> None:
    app = create_app(offline_settings(tmp_path))
    entered, release = threading.Event(), threading.Event()
    with TestClient(app) as client, sample_txt.open("rb") as handle:
        assert client.post("/api/ingest", files=[("files", (sample_txt.name, handle, "text/plain"))]).status_code == 202
        source_id = client.get("/api/sources").json()[0]["source_id"]
        ingestion = app.state.registry.get("default").ingestion
        original = ingestion.reparse_source
        def slow_reparse(source_id, source_platform_user="sansuokb"):
            entered.set()
            assert release.wait(5)
            return original(source_id, source_platform_user=source_platform_user)
        monkeypatch.setattr(ingestion, "reparse_source", slow_reparse)
        try:
            created = client.post("/api/agents/default/reparse-jobs", json={"source_ids": [source_id, source_id]})
            assert created.status_code == 202 and created.json()["total"] == 1
            job_id = created.json()["job_id"]
            assert entered.wait(3)
            assert client.post("/api/agents/default/reparse-jobs", json={"source_ids": [source_id]}).status_code == 409
            tasks = client.get("/api/tasks", params={"task_type": "document_reparse", "agent_id": "default"}).json()
            assert tasks["total"] == 1 and tasks["items"][0]["status"] == "RUNNING"
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "completed":
                break
            time.sleep(0.05)
        assert job["status"] == "completed" and job["succeeded"] == 1


def test_reindex_job_uses_existing_markdown_and_appears_in_tasks(tmp_path, sample_txt, monkeypatch) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_txt.open('rb') as handle:
        assert client.post('/api/ingest', files=[('files', (sample_txt.name, handle, 'text/plain'))]).status_code == 202
        source_id = client.get('/api/sources').json()[0]['source_id']
        runtime = app.state.registry.get('default')
        source = runtime.repository.update(source_id, pdf_platform_status='failed')
        before = Path(source.local_md_path).read_text(encoding='utf-8')
        revisions = runtime.repository.versions(source_id)
        submissions = []
        monkeypatch.setattr(runtime.dify, 'retry_document', lambda document_id, path, name: submissions.append(Path(path).read_text(encoding='utf-8')) or True)
        monkeypatch.setattr('app.ingestion.document_to_markdown', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not parse')))
        response = client.post('/api/agents/default/reindex-jobs', json={'source_ids': [source_id, source_id]})
        assert response.status_code == 202 and response.json()['total'] == 1
        job_id = response.json()['job_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get('/api/jobs/' + job_id).json()
            if job['status'] == 'completed':
                break
            time.sleep(0.05)
        assert job['succeeded'] == 1
        assert submissions == [before]
        assert runtime.repository.versions(source_id) == revisions
        tasks = client.get('/api/tasks', params={'agent_id': 'default', 'task_type': 'document_reindex'}).json()
        assert tasks['total'] == 1 and tasks['items'][0]['status'] == 'SUCCEEDED'


def test_batch_delete_creates_persisted_task_and_checks_active_ingest(tmp_path, sample_txt) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_txt.open('rb') as handle:
        assert client.post('/api/ingest', files=[('files', (sample_txt.name, handle, 'text/plain'))]).status_code == 202
        source_id = client.get('/api/sources').json()[0]['source_id']
        queued = client.post('/api/agents/default/ingest-jobs', json={'filenames': ['waiting.txt']}).json()
        url = '/api/agents/default/sources/delete-batch'
        assert client.post(url, json={'source_ids': [source_id]}).status_code == 409
        assert client.post('/api/jobs/' + queued['job_id'] + '/cancel').status_code == 200
        assert client.post(url, json={'source_ids': ['missing']}).status_code == 404
        result = client.post(url, json={'source_ids': [source_id, source_id]})
        assert result.status_code == 202 and result.json()['total'] == 1
        tasks = client.get('/api/tasks', params={'agent_id': 'default', 'task_type': 'document_delete'}).json()
        assert tasks['total'] == 1 and tasks['items'][0]['task_id'] == result.json()['job_id']


def test_document_filters_stats_and_task_center_use_persisted_data(tmp_path, sample_txt) -> None:
    app = create_app(offline_settings(tmp_path))
    with TestClient(app) as client, sample_txt.open("rb") as handle:
        response = client.post("/api/ingest", files=[("files", (sample_txt.name, handle, "text/plain"))])
        assert response.status_code == 202

        successful = client.get(
            "/api/agents/default/sources/page?page=1&page_size=30&parse_status=success&document_type=txt"
        )
        assert successful.status_code == 200
        assert successful.json()["total"] == 1
        assert client.get(
            "/api/agents/default/sources/page?page=1&page_size=30&document_type=pdf"
        ).json()["total"] == 0

        stats = client.get("/api/agents/default/sources/stats")
        assert stats.status_code == 200
        assert stats.json()["total"] == 1
        assert stats.json()["parsed"] == 1

        created = client.post(
            "/api/agents/default/ingest-jobs", json={"filenames": ["queued-document.txt"]}
        )
        assert created.status_code == 201
        tasks = client.get("/api/tasks?agent_id=default&task_type=document_ingest")
        assert tasks.status_code == 200
        assert tasks.json()["total"] == 1
        assert tasks.json()["items"][0]["task_id"] == created.json()["job_id"]
        assert tasks.json()["items"][0]["status"] == "PENDING"


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
        assert runtime.repository.get(source.source_id, include_deleting=True).filename == "keep.txt"
        with pytest.raises(KeyError):
            runtime.repository.get(source.source_id)
        assert Path(source.source_path).exists()
        runtime.ingestion.dify.delete_document = lambda _: None
        assert client.delete(f'/api/agents/default/sources/{source.source_id}').status_code == 204
        assert not Path(source.source_path).exists()


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
