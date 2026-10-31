from fastapi.testclient import TestClient

from app.auth import SessionManager, hash_password
from app.config import Settings
from app.main import create_app


def test_audit_records_real_events_without_request_contents_and_requires_admin(tmp_path) -> None:
    settings = Settings(app_data_dir=tmp_path / "data", app_mock_external_services=True,
                        auth_enabled=True, admin_password_hash=hash_password("test-password"), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "sansuokb", "password": "wrong-secret"}).status_code == 401
        assert client.post("/api/auth/login", json={"username": "sansuokb", "password": "test-password"}).status_code == 200
        created = client.post("/api/integrations", json={"name": "test", "knowledge_base_ids": ["default-kb"], "tool_names": ["find_files"]})
        assert created.status_code == 201
        token = created.json()["token"]
        response = client.post("/api/external/v1/tools/find_files", headers={"Authorization": f"Bearer {token}"},
                               json={"knowledge_base_id": "default-kb", "arguments": {"query": "private-query"}})
        assert response.status_code == 200
        events = client.get("/api/admin/audit", params={"resource_type": "session", "action": "login"}).json()
        assert events["total"] == 2
        assert [(event["actor"], event["status_code"]) for event in events["items"]] == [("local:sansuokb", 200), ("anonymous", 401)]
        tools = client.get("/api/admin/audit", params={"resource_type": "tool", "page_size": 1}).json()
        assert tools["total"] == 1 and tools["items"][0]["actor"].startswith("integration:")
        contents = (settings.app_data_dir / "audit.db").read_bytes()
        for secret in (b"wrong-secret", b"test-password", b"private-query", token.encode()):
            assert secret not in contents
        client.cookies.clear()
        client.cookies.set(sessions.cookie_name, sessions.create("alice", False, "ldap"))
        assert client.get("/api/admin/audit").status_code == 403
