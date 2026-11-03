import sqlite3
import pytest
from fastapi.testclient import TestClient
from app.auth import hash_password
from app.config import Settings
from app.main import create_app
from app.local_accounts import LocalAccountStore
from app.access import AccessStore, UserRoleInput


def test_local_account_storage(tmp_path):
    store = LocalAccountStore(tmp_path / 'accounts.db')
    with pytest.raises(ValueError):
        store.create('ADMIN', 'LongPassword-123', reserved_username='admin')
    with pytest.raises(ValueError):
        store.create('alice', 'short', reserved_username='admin')
    store.create('alice', 'LongPassword-123', reserved_username='admin')
    with pytest.raises(sqlite3.IntegrityError):
        store.create('alice', 'OtherPassword-123', reserved_username='admin')
    assert LocalAccountStore(store.path).authenticate('alice', 'LongPassword-123')
    assert not store.authenticate('alice', 'wrong')
    assert not store.authenticate('missing', 'LongPassword-123')
    assert b'LongPassword-123' not in store.path.read_bytes()


def test_local_login_and_isolation(tmp_path):
    settings = Settings(app_data_dir=tmp_path, app_mock_external_services=True,
        auth_enabled=True, admin_password_hash=hash_password('AdminPassword-123'),
        llm_api_key='', llm_base_url='', llm_model='')
    accounts = LocalAccountStore(tmp_path / 'local-accounts.db')
    accounts.create('alice', 'LongPassword-123', reserved_username=settings.admin_username)
    access = AccessStore(tmp_path / 'access.db', f'local:{settings.admin_username}')
    access.assign(UserRoleInput(user_id='local_user:alice', role_name='viewer'))
    with TestClient(create_app(settings)) as client:
        assert client.post('/api/auth/login', json={'username':'alice','password':'wrong'}).status_code == 401
        assert client.post('/api/auth/login', json={'username':'alice','password':'LongPassword-123'}).status_code == 200
        session = client.get('/api/auth/session').json()
        assert session['authenticated'] and not session['is_admin']
        assert 'knowledge.read' in session['permissions']
        assert 'admin' not in session['permissions']
        assert client.get('/api/admin/access').status_code == 403
        assert client.get('/api/knowledge-bases/statistics').json() == []
        assert client.get('/api/agents/default/sources').status_code in (403, 404)
        assert client.post('/api/auth/logout').status_code in (200,204)
        assert not client.get('/api/auth/session').json()['authenticated']
        assert client.post('/api/auth/login', json={'username':settings.admin_username,'password':'AdminPassword-123'}).status_code == 200
        assert client.get('/api/auth/session').json()['is_admin']
