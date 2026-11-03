from fastapi.testclient import TestClient
from app.auth import SessionManager, hash_password
from app.config import Settings
from app.main import create_app


def test_generated_ids_and_managed_fields(tmp_path):
    settings = Settings(app_data_dir=tmp_path, auth_enabled=True,
        admin_password_hash=hash_password('admin-test'), app_mock_external_services=True,
        llm_api_key='', llm_base_url='', llm_model='')
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        body = dict(name='测试知识库', dataset_id='dify-existing-id', api_key='test-only-key',
                    knowledge_base_id='chosen-by-user', tenant_key='other-tenant',
                    embedding_model='untrusted-model', pdf_parser='qwenvl')
        first = client.post('/api/admin/knowledge-bases', json=body)
        assert first.status_code == 200, first.text
        row = first.json()
        assert row['knowledge_base_id'].startswith('kb-')
        assert row['knowledge_base_id'] != body['knowledge_base_id']
        assert row['dataset_id'] == body['dataset_id']
        assert row['tenant_key'] == '' and row['embedding_model'] == ''
        assert row['pdf_parser'] == 'default'
        body.pop('knowledge_base_id')
        body['dataset_id'] = 'another-dify-id'
        second = client.post('/api/admin/knowledge-bases', json=body)
        assert second.status_code == 200, second.text
        assert second.json()['knowledge_base_id'] != row['knowledge_base_id']
        identifier = row['knowledge_base_id']
        url = '/api/admin/knowledge-bases/' + identifier
        client.cookies.set(sessions.cookie_name, sessions.create())
        configured = {**row, 'embedding_model':'managed-embedding', 'pdf_parser':'mineru'}
        assert client.put(url, json=configured).status_code == 200
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        changed = client.put(url, json={**row, 'embedding_model':'evil', 'pdf_parser':'local', 'tenant_key':'evil'})
        assert changed.status_code == 200, changed.text
        assert changed.json()['embedding_model'] == 'managed-embedding'
        assert changed.json()['pdf_parser'] == 'mineru'
        assert changed.json()['tenant_key'] == ''
        assert client.put(url, json={**row, 'knowledge_base_id':'renamed'}).status_code == 400
        client.cookies.set(sessions.cookie_name, sessions.create('bob', False, 'ldap'))
        assert client.put(url, json=row).status_code == 403
