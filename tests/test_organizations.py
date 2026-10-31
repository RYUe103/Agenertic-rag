from app.auth import SessionManager, hash_password
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_organization_management_is_admin_only_and_persistent(tmp_path) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data",
        app_mock_external_services=True,
        auth_enabled=True,
        admin_password_hash=hash_password("test-password"),
        ingest_worker_count=1,
        ingest_max_retries=0,
    )
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.get("/api/admin/organizations").status_code == 403

        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        created = client.post("/api/admin/organizations", json={"name": "Acme", "tenant_key": "acme"})
        assert created.status_code == 201
        assert created.json()["tenant_key"] == "acme"
        assert client.post("/api/admin/organizations", json={"name": "Duplicate", "tenant_key": "acme"}).status_code == 409
        assert client.put("/api/admin/organizations/members", json={"tenant_key": "missing", "user_id": "alice", "role": "member"}).status_code == 404
        member = client.put("/api/admin/organizations/members", json={"tenant_key": "acme", "user_id": "alice", "role": "member"})
        assert member.status_code == 200
        assert member.json()["user_id"] == "alice"
        overview = client.get("/api/admin/organizations").json()
        assert overview["organizations"][0]["tenant_key"] == "acme"
        assert overview["members"][0]["tenant_key"] == "acme"

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.get("/api/admin/organizations").json()["organizations"][0]["name"] == "Acme"


def test_ldap_configuration_is_admin_only_and_masks_password(tmp_path, monkeypatch) -> None:
    from app.auth import LdapAuthenticator

    settings = Settings(
        app_data_dir=tmp_path / "data",
        app_mock_external_services=True,
        auth_enabled=True,
        admin_password_hash=hash_password("test-password"),
        ingest_worker_count=1,
        ingest_max_retries=0,
    )
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    monkeypatch.setattr(LdapAuthenticator, "authenticate", lambda self, username, password: username == "alice" and password == "secret")
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.get("/api/admin/ldap").status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        saved = client.put("/api/admin/ldap", json={
            "enabled": True, "server_url": "ldap://example.test", "user_dn_template": "uid={username},dc=example,dc=com",
            "base_dn": "dc=example,dc=com", "user_filter": "(uid={username})", "bind_dn": "cn=admin,dc=example,dc=com",
            "bind_password": "secret", "timeout_seconds": 5,
        })
        assert saved.status_code == 200
        assert saved.json()["bind_password_configured"] is True
        assert "bind_password" not in saved.json()
        assert client.post("/api/admin/ldap/test", json={"username": "alice", "password": "secret"}).json()["ok"] is True
    restarted = create_app(settings)
    with TestClient(restarted) as client:
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.get("/api/admin/ldap").json()["server_url"] == "ldap://example.test"


def test_ldap_group_mapping_syncs_role_on_login(tmp_path, monkeypatch) -> None:
    from app.auth import LdapAuthentication, LdapAuthenticator

    settings = Settings(
        app_data_dir=tmp_path / "data", app_mock_external_services=True, auth_enabled=True,
        admin_password_hash=hash_password("test-password"), ingest_worker_count=1,
        ldap_enabled=True, ldap_server_url="ldap://example.test",
        ldap_group_role_map={"researchers": "writer"},
    )
    app = create_app(settings)
    monkeypatch.setattr(LdapAuthenticator, "authenticate_with_groups", lambda self, username, password: LdapAuthentication(username, ("researchers",)))
    with TestClient(app) as client:
        sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.put("/api/admin/access/roles", json={"name": "writer", "permissions": ["document.write"]}).status_code == 200
        result = client.post("/api/auth/login", json={"username": "alice", "password": "secret"})
        assert result.status_code == 200
        assert client.post("/api/auth/login", json={"username": settings.admin_username, "password": "test-password"}).status_code == 200
        assert next(row for row in client.get("/api/admin/access").json()["users"] if row["user_id"] == "ldap:alice")["role_name"] == "writer"

    monkeypatch.setattr(LdapAuthenticator, "authenticate_with_groups", lambda self, username, password: LdapAuthentication(username, ("unmapped",)))
    restarted = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(restarted) as client:
        assert client.post("/api/auth/login", json={"username": "bob", "password": "secret"}).status_code == 200
        assert client.post("/api/auth/login", json={"username": settings.admin_username, "password": "test-password"}).status_code == 200
        assert next(row for row in client.get("/api/admin/access").json()["users"] if row["user_id"] == "ldap:bob")["role_name"] == "viewer"


def test_organization_members_can_access_tenant_knowledge_base(tmp_path) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data", app_mock_external_services=True, auth_enabled=True,
        admin_password_hash=hash_password("test-password"), ingest_worker_count=1, ingest_max_retries=0,
    )
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.post("/api/admin/organizations", json={"name": "Acme", "tenant_key": "acme"}).status_code == 201
        assert client.put("/api/admin/organizations/members", json={"tenant_key": "acme", "user_id": "ldap:alice", "role": "member"}).status_code == 200
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "acme-kb", "tenant_key": "acme", "name": "Acme KB", "dataset_id": "ds", "api_key": "key"}).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        visible = client.get("/api/knowledge-bases").json()[0]
        assert visible["knowledge_base_id"] == "acme-kb"
        assert visible["can_write_documents"] is False
        assert client.get("/api/agents/%40kb%3Aacme-kb/sources/page").status_code == 200
        assert client.put("/api/admin/knowledge-bases/acme-kb", json={"knowledge_base_id": "acme-kb", "name": "changed"}).status_code == 403
        assert client.delete("/api/admin/knowledge-bases/acme-kb").status_code == 403
        assert client.post("/api/agents/%40kb%3Aacme-kb/ingest-jobs", json={"filenames": ["test.txt"]}).status_code == 403
        job = app.state.delete_queue_repository.create("@kb:acme-kb", "acme-kb", "Acme KB", [])
        assert client.post(f"/api/delete-jobs/{job.job_id}/cancel").status_code == 403
        assert client.post("/api/agents/%40kb%3Aacme-kb/sources/src_missing/refresh-dify-status").status_code == 403
        integration = client.post("/api/integrations", json={"name": "Alice client", "client_type": "custom", "knowledge_base_ids": ["acme-kb"], "tool_names": ["find_files"]})
        assert integration.status_code == 201
        token = integration.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        call = {"knowledge_base_id": "acme-kb", "arguments": {"query": "missing"}}
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json=call).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create("bob", False, "ldap"))
        assert client.get("/api/knowledge-bases").json() == []
        assert client.get("/api/agents/%40kb%3Aacme-kb/sources/page").status_code == 403
        assert client.get("/api/agents/%40kb%3Aacme-kb/sources/src_missing/versions/1").status_code == 403
        assert client.delete("/api/admin/organizations/acme/members/ldap%3Aalice").status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.put("/api/admin/access/roles", json={"name": "writer", "permissions": ["document.write"]}).status_code == 200
        assert client.put("/api/admin/access/user-role", json={"user_id": "ldap:alice", "role_name": "writer"}).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.get("/api/knowledge-bases").json()[0]["can_write_documents"] is True
        assert client.post("/api/agents/%40kb%3Aacme-kb/sources/src_missing/refresh-dify-status").status_code == 404
        assert client.post(f"/api/delete-jobs/{job.job_id}/cancel").status_code != 403
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        assert client.delete("/api/admin/organizations/acme/members/ldap%3Aalice").status_code == 204
        assert client.post("/api/external/v1/tools/find_files", headers=headers, json=call).status_code == 403
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.get("/api/knowledge-bases").json() == []


def test_integration_write_requires_current_owner_permission(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / 'data', app_mock_external_services=True, auth_enabled=True,
                        admin_password_hash=hash_password('test-password'), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, 'local'))
        agent = client.get('/api/admin/agents').json()[0]
        agent['allowed_tools'] = list(dict.fromkeys([*agent['allowed_tools'], 'delete_document']))
        assert client.put('/api/admin/agents/' + agent['agent_id'], json=agent).status_code == 200
        assert client.post('/api/admin/organizations', json={'name': 'Acme', 'tenant_key': 'acme'}).status_code == 201
        assert client.put('/api/admin/organizations/members', json={'tenant_key': 'acme', 'user_id': 'ldap:alice'}).status_code == 200
        assert client.post('/api/admin/knowledge-bases', json={'knowledge_base_id': 'acme-kb', 'tenant_key': 'acme', 'name': 'Acme', 'dataset_id': 'ds', 'api_key': 'key'}).status_code == 200
        assert client.put('/api/admin/access/roles', json={'name': 'writer', 'permissions': ['document.write', 'tool.invoke']}).status_code == 200
        assert client.put('/api/admin/access/user-role', json={'user_id': 'ldap:alice', 'role_name': 'writer'}).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        created = client.post('/api/integrations', json={'name': 'client', 'knowledge_base_ids': ['acme-kb'], 'tool_names': ['delete_document']})
        assert created.status_code == 201
        headers = {'Authorization': 'Bearer ' + created.json()['token']}
        payload = {'knowledge_base_id': 'acme-kb', 'arguments': {'source_id': 'src_test'}}
        assert client.post('/api/external/v1/tools/delete_document/approvals', headers=headers, json=payload).status_code == 201
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, 'local'))
        assert client.put('/api/admin/access/user-role', json={'user_id': 'ldap:alice', 'role_name': 'viewer'}).status_code == 200
        assert client.post('/api/external/v1/tools/delete_document/approvals', headers=headers, json=payload).status_code == 403
        assert client.post('/api/external/v1/tools/delete_document', headers=headers, json=payload).status_code == 403


def test_admin_tool_registry_controls_real_tool_visibility(tmp_path) -> None:
    settings = Settings(
        app_data_dir=tmp_path / "data", app_mock_external_services=True, auth_enabled=True,
        admin_password_hash=hash_password("test-password"), ingest_worker_count=1, ingest_max_retries=0,
    )
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create(settings.admin_username, True, "local"))
        tools = client.get("/api/admin/tools")
        assert tools.status_code == 200
        assert any(item["name"] == "find_files" and "output_schema" in item for item in tools.json())
        assert client.put("/api/admin/tools/find_files", json={"enabled": False}).status_code == 200
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "tool-kb", "name": "Tools", "dataset_id": "ds", "api_key": "key"}).status_code == 200
        visible = client.get("/api/knowledge-bases/tool-kb/tools")
        assert visible.status_code == 200
        assert all(item["name"] != "find_files" for item in visible.json())


def test_agent_tool_switch_and_call_statistics_apply_in_graph(tmp_path, monkeypatch) -> None:
    from app.agent import AgentService, agent_execution_context

    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=False, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    seen = []
    original = AgentService._invoke_tool

    def observe(self, state, name, *args, **kwargs):
        seen.append((name, agent_execution_context.get() is not None))
        return original(self, state, name, *args, **kwargs)

    monkeypatch.setattr(AgentService, "_invoke_tool", observe)
    with TestClient(app) as client:
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "tool-kb", "name": "Tools", "dataset_id": "ds", "api_key": "key"}).status_code == 200
        assert client.post("/api/agents/%40kb%3Atool-kb/retrieve", json={"question": "查找 ABC 文件"}).status_code == 200
        assert seen and all(has_context for _, has_context in seen)
        stats = client.get("/api/knowledge-bases/tool-kb/tools").json()
        assert sum(tool["statistics"]["calls"] for tool in stats) > 0
        name = seen[0][0]
        previous_calls = client.get(f"/api/knowledge-bases/tool-kb/tools/{name}/calls").json()
        previous_agent_count = sum(call["channel"] == "agent" for call in previous_calls)
        assert client.put(f"/api/admin/tools/{name}", json={"enabled": False}).status_code == 200
        seen.clear()
        assert client.post("/api/agents/%40kb%3Atool-kb/retrieve", json={"question": "查找 ABC 文件"}).status_code == 200
        calls = client.get(f"/api/knowledge-bases/tool-kb/tools/{name}/calls").json()
        assert sum(call["channel"] == "agent" for call in calls) == previous_agent_count


def test_parallel_agent_requests_keep_execution_permissions_separate(tmp_path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.agent import AgentService, agent_execution_context
    from app.schemas import RetrievalResponse

    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    barrier = Barrier(2, timeout=5)
    observed = {}

    def retrieve(self, question, source_base_url, conversation_id, history):
        barrier.wait()
        observed[question] = agent_execution_context.get()["code_executor"] is not None
        return RetrievalResponse(agent_id=self.agent_id, agent_name=self.agent_name, knowledge_base_id=self.knowledge_base_id,
                                 question=question, answer_prompt="", evidences=[], sources=[], process=[], trace=[], tool_calls=0)

    monkeypatch.setattr(AgentService, "retrieve", retrieve)
    with TestClient(app) as client:
        admin_cookie = sessions.create(settings.admin_username, True, "local")
        alice_cookie = sessions.create("alice", False, "ldap")
        client.cookies.set(sessions.cookie_name, admin_cookie)
        assert client.post("/api/admin/organizations", json={"name": "Acme", "tenant_key": "acme"}).status_code == 201
        assert client.put("/api/admin/organizations/members", json={"tenant_key": "acme", "user_id": "ldap:alice"}).status_code == 200
        assert client.post("/api/admin/knowledge-bases", json={"knowledge_base_id": "shared", "tenant_key": "acme", "name": "Shared", "dataset_id": "ds", "api_key": "key"}).status_code == 200
        assert client.put("/api/admin/access/roles", json={"name": "no_execution", "permissions": ["knowledge.read", "document.read"]}).status_code == 200
        assert client.put("/api/admin/access/user-role", json={"user_id": "ldap:alice", "role_name": "no_execution"}).status_code == 200
        policy = client.get("/api/admin/sandbox/policy").json()
        policy.update(enabled=True, allow_users=True, allow_python=True)
        assert client.put("/api/admin/sandbox/policy", json=policy).status_code == 200
        def send(label, cookie):
            return client.post("/api/agents/%40kb%3Ashared/retrieve", json={"question": label, "conversation_id": label},
                               headers={"Cookie": f"{sessions.cookie_name}={cookie}"}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: send(*item), [("admin", admin_cookie), ("alice", alice_cookie)]))
        assert results == [200, 200]
        assert observed == {"admin": True, "alice": False}
