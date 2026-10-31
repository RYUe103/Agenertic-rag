from fastapi.testclient import TestClient

from app.auth import SessionManager, hash_password
from app.clients.dify import DifyClient
from app.config import Settings
from app.main import create_app
from app.schemas import VectorHit
from app.ingest_queue import IngestQueueRepository
from app.schemas import BatchIngestCreate


def test_dify_retrieval_mode_and_filter_are_per_request(monkeypatch):
    client = DifyClient('https://dify.invalid/v1', 'key', 'dataset')
    calls = []
    monkeypatch.setattr(client, '_request', lambda method, path, **kwargs: calls.append(kwargs['json']) or {'records': []})
    conditions = {'logical_operator': 'and', 'conditions': []}
    client.retrieve('query', 3, conditions, search_method='hybrid_search')
    client.retrieve('query', 2)
    assert calls[0]['retrieval_model']['search_method'] == 'hybrid_search'
    assert calls[0]['retrieval_model']['metadata_filtering_conditions'] == conditions
    assert calls[1]['retrieval_model']['search_method'] == 'semantic_search'
    assert 'metadata_filtering_conditions' not in calls[1]['retrieval_model']


def test_retrieval_test_returns_real_scoped_hits_and_checks_permissions(tmp_path, monkeypatch):
    settings = Settings(app_data_dir=tmp_path / 'data', app_mock_external_services=True, auth_enabled=True,
                        admin_password_hash=hash_password('test-password'), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.post('/api/admin/knowledge-bases', json={'knowledge_base_id': 'alice-kb', 'name': 'Alice', 'dataset_id': 'ds', 'api_key': 'key'}).status_code == 200
        runtime = app.state.registry.get('@kb:alice-kb')
        runtime.repository.create({'source_id': 'src_one', 'filename': 'one.pdf', 'normalized_filename': 'one.pdf',
                                   'file_hash': 'hash-one', 'local_pdf_path': '/tmp/one.pdf',
                                   'local_md_path': '/tmp/one.md', 'dify_document_id': 'doc-one'})
        calls = []
        def retrieve(query, top_k, conditions, search_method=None):
            calls.append((query, top_k, conditions, search_method))
            return [VectorHit(document_id='doc-one', content='actual result', score=0.9, segment_id='seg-one'),
                    VectorHit(document_id='unknown', content='unmatched')]
        monkeypatch.setattr(runtime.dify, 'retrieve', retrieve)
        payload = {'query': ' test ', 'top_k': 3, 'search_method': 'hybrid_search',
                   'metadata_filtering_conditions': {'logical_operator': 'and', 'conditions': []}}
        response = client.post('/api/knowledge-bases/alice-kb/retrieval-test', json=payload)
        assert response.status_code == 200
        assert response.json()['items'][0]['content'] == 'actual result'
        assert response.json()['items'][0]['source_id'] == 'src_one'
        assert response.json()['unmatched_count'] == 1
        assert calls == [('test', 3, payload['metadata_filtering_conditions'], 'hybrid_search')]
        assert client.post('/api/knowledge-bases/alice-kb/retrieval-test', json={**payload, 'top_k': 100}).status_code == 422
        client.cookies.set(sessions.cookie_name, sessions.create('bob', False, 'ldap'))
        assert client.post('/api/knowledge-bases/alice-kb/retrieval-test', json=payload).status_code == 403


def test_ingest_events_are_persistent_and_paginated(tmp_path):
    repository = IngestQueueRepository(tmp_path / 'queue.db')
    job = repository.create('default', BatchIngestCreate(filenames=['one.txt']))
    item = job.items[0]
    path = tmp_path / 'one.txt'
    path.write_text('example')
    repository.mark_uploaded(item.item_id, path)
    repository.start(job.job_id)
    claimed = repository.claim(1)
    assert len(claimed) == 1
    repository.finish_item(item.item_id, 'success', 'src_one', '处理完成')
    first_page = repository.events(job.job_id, limit=2)
    assert [event['status'] for event in first_page['items']] == ['success', 'running']
    assert first_page['items'][0]['filename'] == 'one.txt'
    second_page = IngestQueueRepository(tmp_path / 'queue.db').events(job.job_id, limit=2, before_id=first_page['next_before_id'])
    assert [event['status'] for event in second_page['items']] == ['queued', 'success']
    third_page = repository.events(job.job_id, limit=2, before_id=second_page['next_before_id'])
    assert [event['status'] for event in third_page['items']] == ['pending']
    assert third_page['next_before_id'] is None


def test_task_events_respect_knowledge_base_access(tmp_path):
    settings = Settings(app_data_dir=tmp_path / 'data', app_mock_external_services=True, auth_enabled=True,
                        admin_password_hash=hash_password('test-password'), ingest_worker_count=1)
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.post('/api/admin/knowledge-bases', json={'knowledge_base_id': 'alice-kb', 'name': 'Alice', 'dataset_id': 'ds', 'api_key': 'key'}).status_code == 200
        job = client.post('/api/agents/%40kb%3Aalice-kb/ingest-jobs', json={'filenames': ['test.txt']}).json()
        assert client.get(f"/api/jobs/{job['job_id']}/events").json()['items'][0]['status'] == 'pending'
        client.cookies.set(sessions.cookie_name, sessions.create('bob', False, 'ldap'))
        assert client.get(f"/api/jobs/{job['job_id']}/events").status_code == 403
