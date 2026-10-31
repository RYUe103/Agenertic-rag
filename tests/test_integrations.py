from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app.auth import SessionManager, hash_password
from app.config import Settings
from app.main import create_app
from app.schemas import VectorHit
from app.tool_registry import ToolApprovalStore


def test_approval_expiry_and_concurrent_consumption(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    store = ToolApprovalStore(tmp_path / 'approvals.db')
    item = store.create('alice', 'kb', 'find_files', {'query': 'a'})
    assert store.decide(item['id'], 'admin', True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: store.consume(item['id'], 'alice', 'kb', 'find_files', {'query': 'a'}), range(8)))
    assert sum(outcomes) == 1
    expired = store.create('alice', 'kb', 'find_files', {'query': 'b'})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tool_approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired['id'],))
    assert store.get(expired['id'])['status'] == 'expired'
    assert not store.decide(expired['id'], 'admin', True)


def test_upload_approval_uses_content_digest_and_binds_exact_content(tmp_path) -> None:
    store = ToolApprovalStore(tmp_path / 'approvals.db')
    arguments = {'filename': 'large.txt', 'content_base64': 'a' * 100_000, 'mime_type': 'text/plain'}
    item = store.create('alice', 'kb', 'upload_document', arguments)
    assert 'content_base64' not in item['arguments']
    assert item['arguments']['content_base64_length'] == 100_000
    assert store.decide(item['id'], 'admin', True)
    assert not store.consume(item['id'], 'alice', 'kb', 'upload_document', {**arguments, 'content_base64': 'b' * 100_000})
    assert store.consume(item['id'], 'alice', 'kb', 'upload_document', arguments)


def test_tool_approval_is_bound_to_call_and_consumed_once(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / 'data', app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password('test-password'), ingest_worker_count=1)
    with TestClient(create_app(settings)) as client:
        url = '/api/knowledge-bases/default-kb/tools/find_files'
        assert client.put('/api/admin/tools/find_files', json={'enabled': True, 'requires_approval': True}).status_code == 200
        assert client.put('/api/admin/tools/find_files', json={'enabled': True}).json()['requires_approval'] is True
        assert client.post(url + '/test', json={'query': 'one'}).status_code == 403
        approval = client.post(url + '/approvals', json={'query': 'one'}).json()
        ident = approval['id']
        assert client.post('/api/admin/tool-approvals/' + ident + '/approve').status_code == 200
        headers = {'X-Approval-Id': ident}
        assert client.post(url + '/test', headers=headers, json={'query': 'two'}).status_code == 403
        assert client.post(url + '/test', headers=headers, json={'query': 'one'}).status_code == 200
        assert client.post(url + '/test', headers=headers, json={'query': 'one'}).status_code == 403
        assert client.post('/api/admin/tool-approvals/' + ident + '/approve').status_code == 409
        created = client.post('/api/integrations', json={'name': 'client', 'knowledge_base_ids': ['default-kb'], 'tool_names': ['find_files']}).json()
        ext_headers = {'Authorization': 'Bearer ' + created['token']}
        payload = {'knowledge_base_id': 'default-kb', 'arguments': {'query': 'one'}}
        ext = client.post('/api/external/v1/tools/find_files/approvals', headers=ext_headers, json=payload).json()
        assert client.post('/api/admin/tool-approvals/' + ext['id'] + '/approve').status_code == 200
        assert client.post(url + '/test', headers={'X-Approval-Id': ext['id']}, json={'query': 'one'}).status_code == 403
        assert client.post('/api/external/v1/tools/find_files', headers=ext_headers, json={**payload, 'approval_id': ext['id']}).status_code == 200
        assert client.post('/api/external/v1/tools/find_files', headers=ext_headers, json={**payload, 'approval_id': ext['id']}).status_code == 403


def test_explicit_search_modes_are_scoped_and_call_dify(tmp_path, monkeypatch) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    with TestClient(app) as client:
        agent = client.get("/api/admin/agents").json()[0]
        agent["allowed_tools"] += ["search_bm25", "search_hybrid"]
        assert client.put("/api/admin/agents/" + agent["agent_id"], json=agent).status_code == 200
        runtime = app.state.registry.get("@kb:default-kb")
        source = runtime.repository.create({"source_id": "src_mode", "filename": "mode.pdf", "normalized_filename": "mode.pdf",
                                            "file_hash": "mode", "local_pdf_path": str(tmp_path / "mode.pdf"),
                                            "local_md_path": str(tmp_path / "mode.md"), "dify_document_id": "dify-mode"})
        calls = []
        def retrieve(query, top_k, conditions, search_method=None):
            calls.append((query, top_k, conditions, search_method))
            return [VectorHit(document_id="dify-mode", content="matched", score=0.7, segment_id="segment")]
        monkeypatch.setattr(runtime.dify, "retrieve", retrieve)
        created = client.post("/api/integrations", json={"name": "search", "knowledge_base_ids": ["default-kb"],
                              "tool_names": ["search_bm25"]}).json()
        headers = {"Authorization": "Bearer " + created["token"]}
        payload = {"knowledge_base_id": "default-kb", "arguments": {"query": "test", "top_k": 100}}
        response = client.post("/api/external/v1/tools/search_bm25", headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json()["items"][0]["source_id"] == source.source_id
        assert response.json()["items"][0]["retrieval_type"] == "bm25"
        assert calls == [("test", 50, None, "full_text_search")]
        assert client.post("/api/external/v1/tools/search_hybrid", headers=headers, json=payload).status_code == 403
        assert client.put("/api/integrations/" + created["integration"]["id"] + "/permissions", json={
            "knowledge_base_ids": ["default-kb"], "tool_names": ["search_hybrid"]}).status_code == 200
        response = client.post("/api/external/v1/tools/search_hybrid", headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json()["items"][0]["retrieval_type"] == "hybrid"
        assert calls[-1][-1] == "hybrid_search"
        assert client.put("/api/admin/tools/search_hybrid", json={"enabled": False}).status_code == 200
        assert client.post("/api/external/v1/tools/search_hybrid", headers=headers, json=payload).status_code == 403


def test_scoped_integration_lifecycle_and_audit(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"),
                        ingest_worker_count=1)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "sansuokb", "password": "test-password"}).status_code == 200
        created = client.post("/api/integrations", json={"name": "测试接入", "client_type": "custom",
                              "knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]})
        assert created.status_code == 201
        integration, token = created.json()["integration"], created.json()["token"]
        assert token.startswith("sk_kb_")
        assert token not in str(client.get("/api/integrations").json())
        assert token.encode() not in (settings.app_data_dir / "integrations.db").read_bytes()
        headers = {"Authorization": f"Bearer {token}"}
        listed = client.get("/api/external/v1/tools", headers=headers)
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()] == ["find_files"]
        call = {"knowledge_base_id": "default-kb", "arguments": {"query": "不存在"}}
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json=call).json() == {"items": [], "count": 0}
        assert client.post("/api/external/v1/tools/vector_search", headers=headers, json=call).status_code == 403
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json={**call, "knowledge_base_id": "other"}).status_code == 403
        assert client.get("/api/integrations/" + integration["id"] + "/calls").json()[0]["status"] == "forbidden"
        permissions_url = "/api/integrations/" + integration["id"] + "/permissions"
        updated = client.put(permissions_url, json={"knowledge_base_ids": ["default-kb"], "tool_names": ["vector_search"]})
        assert updated.status_code == 200
        assert [item["name"] for item in client.get("/api/external/v1/tools", headers=headers).json()] == ["vector_search"]
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json=call).status_code == 403
        assert client.put(permissions_url, json={"knowledge_base_ids": ["default-kb"], "tool_names": ["execute_shell"]}).status_code == 400
        rotated = client.post("/api/integrations/" + integration["id"] + "/rotate").json()["token"]
        assert client.get("/api/external/v1/tools", headers=headers).status_code == 401
        assert client.get("/api/external/v1/tools", headers={"Authorization": f"Bearer {rotated}"}).status_code == 200
        assert client.post("/api/integrations/" + integration["id"] + "/revoke").status_code == 204
        assert client.get("/api/external/v1/tools", headers={"Authorization": f"Bearer {rotated}"}).status_code == 401
        assert client.post("/api/integrations/" + integration["id"] + "/rotate").status_code == 409
        assert client.put(permissions_url, json={"knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]}).status_code == 409


def test_scoped_integration_cannot_grant_another_users_knowledge(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        payload = {"name": "alice", "knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]}
        assert client.post("/api/integrations", json=payload).status_code == 403
        assert client.get("/api/external/v1/tools").status_code == 401


def test_scoped_endpoint_requires_token_when_local_auth_disabled(tmp_path) -> None:
    app = create_app(Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                              auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1))
    with TestClient(app) as client:
        assert client.get("/api/external/v1/tools").status_code == 401


def test_ldap_user_matching_local_admin_name_cannot_access_local_resources(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("sansuokb", False, "ldap"))
        assert client.get("/api/knowledge-bases").json() == []
        assert client.get("/api/admin/knowledge-bases").json() == []
        assert client.get("/api/agents/%40kb%3Adefault-kb/sources/page").status_code == 403
        response = client.post("/api/integrations", json={"name": "test", "knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]})
        assert response.status_code == 403


def test_integration_cannot_outlive_owners_access(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "alice-kb", "name": "Alice", "dataset_id": "alice-data", "api_key": "key"}).status_code == 200
        response = client.post("/api/integrations", json={"name": "test", "knowledge_base_ids": ["alice-kb"], "tool_names": ["find_files"]})
        assert response.status_code == 201
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert [model['id'] for model in client.get('/v1/models', headers=headers).json()['data']] == ['@kb:alice-kb']
        payload = {"knowledge_base_id": "alice-kb", "arguments": {"query": "不存在"}}
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json=payload).status_code == 200
        with app.state.registry.store.connect() as connection:
            connection.execute("UPDATE knowledge_bases SET owner_user_id=?,owner_username=? WHERE knowledge_base_id=?", ("ldap:bob", "bob", "alice-kb"))
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json=payload).status_code == 403
        assert client.get('/v1/models', headers=headers).json()['data'] == []
        assert client.post('/v1/chat/completions', headers=headers, json={'model': '@kb:alice-kb', 'messages': [{'role': 'user', 'content': 'test'}]}).status_code == 403
        permissions = "/api/integrations/" + response.json()["integration"]["id"] + "/permissions"
        assert client.put(permissions, json={"knowledge_base_ids": ["alice-kb"], "tool_names": ["find_files"]}).status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create("sansuokb", True, "local"))
        assert client.put(permissions, json={"knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]}).status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create("bob", False, "ldap"))
        assert client.get("/api/integrations").json() == []
        assert client.get("/api/integrations/" + response.json()["integration"]["id"] + "/calls").status_code == 403


def test_openai_scoped_key_enforces_tool_permissions(tmp_path, monkeypatch) -> None:
    from app.agent import agent_execution_context
    from app.schemas import AskResponse

    settings = Settings(app_data_dir=tmp_path / 'data', app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password('test-password'), ingest_worker_count=1)
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post('/api/integrations', json={'name': 'openai', 'knowledge_base_ids': ['default-kb'],
                              'tool_names': ['find_files']}).json()
        headers = {'Authorization': 'Bearer ' + created['token']}
        assert [item['id'] for item in client.get('/v1/models', headers=headers).json()['data']] == ['@kb:default-kb']
        payload = {'model': '@kb:default-kb', 'messages': [{'role': 'user', 'content': '测试'}]}
        assert client.post('/v1/chat/completions', headers=headers, json={**payload, 'model': '@kb:other'}).status_code == 403
        assert client.post('/v1/chat/completions', headers=headers, json={**payload, 'model': 'default'}).status_code == 403
        runtime = app.state.registry.get('@kb:default-kb')
        actual = client.post('/v1/chat/completions', headers=headers, json=payload)
        assert actual.status_code == 200, actual.text
        assert all(event.get('tool_name') in (None, 'find_files') for event in actual.json()['agentic_rag']['trace'])
        calls = []
        monkeypatch.setattr(runtime.agent.tools, 'find_files', lambda *args: calls.append('find_files') or [])
        monkeypatch.setattr(runtime.agent.tools, 'vector_search', lambda *args: calls.append('vector_search') or [])
        def ask(question, base_url, conversation_id, history):
            context = agent_execution_context.get()
            assert context['code_executor'] is None
            state = {'process': [], 'tool_calls': 0}
            runtime.agent._invoke_tool(state, 'find_files', 'test')
            runtime.agent._invoke_tool(state, 'vector_search', 'test')
            return AskResponse(answer='ok', evidences=[], process=[], tool_calls=state['tool_calls'])
        monkeypatch.setattr(runtime.agent, 'ask', ask)
        result = client.post('/v1/chat/completions', headers=headers, json=payload)
        assert result.status_code == 200, result.text
        assert result.json()['model'] == '@kb:default-kb'
        assert calls == ['find_files']
        assert client.put('/api/admin/tools/find_files', json={'enabled': False}).status_code == 200
        assert client.post('/v1/chat/completions', headers=headers, json=payload).status_code == 200
        assert calls == ['find_files']
        assert client.post('/api/integrations/' + created['integration']['id'] + '/revoke').status_code == 204
        assert client.get('/v1/models', headers=headers).status_code == 401


def test_mcp_streamable_http_uses_scoped_tokens(tmp_path) -> None:
    pytest.importorskip("mcp")
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/auth/login", json={"username": "sansuokb", "password": "test-password"}).status_code == 200
        created = client.post("/api/integrations", json={"name": "mcp", "knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]}).json()
        token = created["token"]
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        assert client.post("/api/mcp/", json=initialize).status_code == 401
        assert client.post("/api/mcp/", headers={**headers, "host": "untrusted.example"}, json=initialize).status_code == 421
        response = client.post("/api/mcp/", headers=headers, json=initialize)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "agentic-rag-knowledge"
        listing = client.post("/api/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listing.status_code == 200
        assert [item["name"] for item in listing.json()["result"]["tools"]] == ["find_files"]
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "find_files", "arguments": {"knowledge_base_id": "default-kb", "arguments": {"query": "不存在"}}}}
        response = client.post("/api/mcp/", headers=headers, json=call)
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == {"items": [], "count": 0}
        call["params"]["arguments"]["knowledge_base_id"] = "other"
        assert client.post("/api/mcp/", headers=headers, json=call).json()["result"]["isError"] is True
        assert client.put("/api/integrations/" + created["integration"]["id"] + "/permissions", json={"knowledge_base_ids": ["default-kb"], "tool_names": ["vector_search"]}).status_code == 200
        listing = client.post("/api/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}})
        assert [item["name"] for item in listing.json()["result"]["tools"]] == ["vector_search"]
        assert client.post("/api/integrations/" + created["integration"]["id"] + "/revoke").status_code == 204
        assert client.post("/api/mcp/", headers=headers, json=initialize).status_code == 401
