from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import SessionManager, hash_password
from app.config import Settings
from app.main import create_app
from app.schemas import AgentTraceEvent, AskResponse
from app.agent_runs import AgentRunStore


def test_dashboard_and_runs_respect_organization_access(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.post('/api/admin/organizations', json={'name': 'Acme', 'tenant_key': 'acme'}).status_code == 201
        assert client.put('/api/admin/organizations/members', json={'tenant_key': 'acme', 'user_id': 'ldap:alice', 'role': 'member'}).status_code == 200
        assert client.post('/api/admin/knowledge-bases', json={'knowledge_base_id': 'acme-kb', 'name': 'Acme', 'tenant_key': 'acme', 'dataset_id': 'ds', 'api_key': 'key'}).status_code == 200
        repo = app.state.registry.get('@kb:acme-kb').repository
        repo.create({'source_id': 'src_visible', 'filename': 'visible.txt', 'normalized_filename': 'visible.txt',
                     'file_hash': 'visible', 'local_pdf_path': str(tmp_path / 'visible.txt'), 'local_md_path': str(tmp_path / 'visible.md')})
        runs = AgentRunStore(settings.app_data_dir / 'agent-runs.db')
        run = runs.start('ldap:alice', 'acme-kb', 'default', 'Agent', 'ask')
        runs.finish(run, 'succeeded', 12)
        assert runs.summary()['succeeded'] == 1 and runs.summary()['failed'] == 0
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        overview = client.get('/api/dashboard').json()
        assert overview['knowledge_bases'] == 1 and overview['documents']['total'] == 1
        assert overview['recent_documents'][0]['filename'] == 'visible.txt'
        assert overview['agent_run_total'] == 1
        assert client.get('/api/agent-runs/page').json()['total'] == 1
        client.cookies.set(sessions.cookie_name, sessions.create('bob', False, 'ldap'))
        overview = client.get('/api/dashboard').json()
        assert overview['knowledge_bases'] == 0 and overview['documents']['total'] == 0
        assert overview['recent_documents'] == [] and overview['agent_run_total'] == 0
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, 'local'))
        assert client.delete('/api/admin/organizations/acme/members/ldap%3Aalice').status_code == 204
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.get('/api/agent-runs/page').json()['total'] == 0
        assert client.get('/api/dashboard').json()['agent_run_total'] == 0


def test_agent_runs_are_private_and_conversations_are_isolated(tmp_path, monkeypatch) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "alice-kb", "name": "Alice", "dataset_id": "alice-data", "api_key": "key"}).status_code == 200
        seen = []
        def fake_ask(question, source_base_url, conversation_id, history):
            seen.append(conversation_id)
            return AskResponse(answer="private answer", evidences=[], process=[], tool_calls=1,
                               trace=[AgentTraceEvent(sequence=1, kind="tool", title="工具返回结果", detail="private detail", query="private query")])
        monkeypatch.setattr(app.state.registry.get("@kb:alice-kb").agent, "ask", fake_ask)
        url = "/api/agents/%40kb%3Aalice-kb/ask"
        payload = {"question": "private question", "conversation_id": "shared"}
        alice = client.post(url, json=payload)
        assert alice.status_code == 200
        run_id = alice.json()["run_id"]
        run = client.get(f"/api/agent-runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["status"] == "succeeded"
        assert run.json()["tool_calls"] == 1
        assert "private" not in str(run.json())
        assert run_id in [item["run_id"] for item in client.get("/api/agent-runs").json()]
        page = client.get("/api/agent-runs/page", params={"knowledge_base_id": "alice-kb", "status": "succeeded", "page_size": 1}).json()
        assert page["total"] == 1 and page["items"][0]["run_id"] == run_id
        assert client.get("/api/agent-runs/page", params={"status": "failed"}).json()["total"] == 0
        assert client.get("/api/agent-runs/page", params={"page": 2, "page_size": 1}).json()["items"] == []
        client.cookies.set(sessions.cookie_name, sessions.create("bob", False, "ldap"))
        assert client.get(f"/api/agent-runs/{run_id}").status_code == 403
        assert client.get("/api/agent-runs").json() == []
        assert client.get("/api/agent-runs/page").json()["total"] == 0
        assert client.get("/api/agent-runs/page", params={"knowledge_base_id": "alice-kb"}).status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create("sansuokb", True, "local"))
        assert client.post(url, json=payload).status_code == 200
        assert len(seen) == 2 and seen[0] != seen[1]
        assert client.get(f"/api/agent-runs/{run_id}").status_code == 200


def test_agent_failure_is_recorded_without_exception_details(tmp_path, monkeypatch) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    def fail(*args):
        raise RuntimeError("private upstream details")
    monkeypatch.setattr(app.state.registry.default().agent, "ask", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/ask", json={"question": "private question"}).status_code == 500
        rows = client.get("/api/agent-runs").json()
        assert rows[0]["status"] == "failed"
        assert "private" not in str(rows)


def test_agent_waits_for_approval_and_resumes(tmp_path, monkeypatch) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    agent = app.state.registry.default().agent
    def fake_ask(question, source_base_url, conversation_id, history):
        agent._invoke_tool({}, 'find_files', query='approval test')
        return AskResponse(answer='done', evidences=[], process=[], tool_calls=1)
    monkeypatch.setattr(agent, 'ask', fake_ask)
    with TestClient(app) as client:
        assert client.put('/api/admin/tools/find_files', json={'enabled': True, 'requires_approval': True}).status_code == 200
        response = client.post('/api/ask', json={'question': 'test'})
        assert response.status_code == 409
        detail = response.json()['detail']
        run_id, approval_id = detail['run_id'], detail['approval_id']
        assert client.get('/api/agent-runs/' + run_id).json()['status'] == 'waiting_approval'
        assert client.post('/api/agent-runs/' + run_id + '/resume').status_code == 409
        assert client.post('/api/admin/tool-approvals/' + approval_id + '/approve').status_code == 200
        resumed = client.post('/api/agent-runs/' + run_id + '/resume')
        assert resumed.status_code == 200 and resumed.json()['answer'] == 'done'
        assert client.get('/api/agent-runs/' + run_id).json()['status'] == 'resumed'
        assert client.post('/api/agent-runs/' + run_id + '/resume').status_code == 409
