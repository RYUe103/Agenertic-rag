import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import SourceRepository
from app.document_repository import DocumentReadService
from app.document_structure import build_structure
from app.auth import SessionManager
from app.main import create_app
from tests.test_api import offline_settings


def source(repo, root, text, name='s'):
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / 'source.txt'
    pdf.write_text('original bytes')
    md = root / 'document.md'
    md.write_text(text)
    return repo.create({'source_id': name, 'filename': name+'.txt', 'file_hash': name,
                       'normalized_filename': name, 'local_pdf_path': str(pdf),
                       'local_md_path': str(md), 'parse_status': 'success'})


def test_structure_lossless_chunks_and_locators():
    text = '<!-- page: 2 -->\n# Title\n\n' + 'alpha sentence. ' * 700 + '\n\n## Sub\nbody\n'
    data = build_structure(text, 'v1')
    assert ''.join(b.content for b in data['blocks']) == text
    assert data['chunks'][0].locator.start == 0
    assert data['chunks'][-1].locator.end == len(text)
    assert all(a.locator.end >= b.locator.start for a,b in zip(data['chunks'],data['chunks'][1:]))
    assert len(data['chunks']) >= 3
    assert len(data['sections']) == 2
    assert data['sections'][1].parent_id == data['sections'][0].id
    for chunk in data['chunks']:
        assert text[chunk.locator.start:chunk.locator.end] == chunk.content
        assert chunk.locator.block_ids
        assert chunk.locator.page_numbers == [2]
    assert data['pages'][0].number == 2


def test_tables_html_and_markdown():
    text = '| A | B |\n|---|---|\n| x | 20 元 |\n\n<table><tr><th colspan="2">Unit: kg</th></tr><tr><td rowspan="2">a</td><td>3</td></tr><tr><td>4</td></tr></table>\n'
    data = build_structure(text, 'v')
    assert len(data['tables']) == 2
    cells = data['tables'][1].cells
    assert cells[0].column_span == 2 and cells[0].is_header
    assert cells[1].row_span == 2
    assert cells[-1].row == 2 and cells[-1].column == 1
    assert data['tables'][0].cells[-1].content == '20 元'
    assert data['tables'][0].cells[-1].unit == '元'
    assert data['tables'][1].unit == 'kg'
    assert all(b.locator.bbox is None for b in data['blocks'])
    assert not data['pages']


def test_mineru_conservative_geometry():
    data = build_structure('Known\n\nrepeat\n\nrepeat\n', 'v', [
        {'text': 'Known', 'page_idx': 0, 'bbox': [1, 2, 3, 4]},
        {'text': 'repeat', 'page_idx': 8, 'bbox': [1, 2, 3, 4]},
    ])
    block = data['blocks'][0]
    assert block.locator.page_numbers == [1]
    assert block.locator.bbox is None
    assert block.attributes['mineru']['bbox'] == [1, 2, 3, 4]
    assert all(not b.locator.page_numbers for b in data['blocks'] if b.content.strip() == 'repeat')


def test_backfill_idempotence_and_restart(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    s = source(repo, tmp_path/'files', '# Original\nhello\n')
    repo.update(s.source_id, dify_document_id='remote-doc', dify_dataset_id='dataset')
    for _ in range(2):
        repo.documents.backfill(s.source_id)
    versions = repo.documents.versions(s.source_id)
    assert len(versions) == 1
    assert repo.documents.artifact(s.source_id, 'original') == b'original bytes'
    assert repo.documents.mappings(s.source_id)[0]['external_document_id'] == 'remote-doc'
    restarted = SourceRepository(tmp_path/'db')
    restarted.documents.backfill(s.source_id)
    assert restarted.documents.version(s.source_id).id == versions[0].id


def test_versions_immutable_and_old_access_after_reparse(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    s = source(repo, tmp_path/'files', '# Old\nold')
    repo.save_revision(s, '# Old\nold', 'local', 'first')
    first = repo.documents.version('s')
    Path(s.local_md_path).write_text('# New\nnew')
    repo.save_revision(s, '# New\nnew', 'local', 'second')
    assert repo.documents.version('s').version_no == 2
    assert repo.documents.version('s', first.id).markdown == '# Old\nold'
    assert repo.documents.nodes('s', 'sections', first.id)[0]['title'] == 'Old'
    repo.delete('s')
    with pytest.raises(KeyError):
        repo.documents.version('s', first.id)


def test_cross_document_version_and_node_ids_rejected(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    for name in ('a', 'b'):
        source(repo, tmp_path/name, 'body', name)
    va = repo.documents.version('a')
    node = repo.documents.nodes('a', 'chunks')[0]
    with pytest.raises(KeyError):
        repo.documents.version('b', va.id)
    with pytest.raises(KeyError):
        repo.documents.node('b', node['id'])
    with pytest.raises(ValueError):
        SourceRepository(tmp_path/'db', 'wrong-kb')


def test_adjacent_page_and_section_reads(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    source(repo, tmp_path/'files', '<!-- page: 3 -->\n# Intro\n' + 'Sentence. ' * 400)
    chunks = list(repo.documents.iter_chunks('s'))
    assert len(repo.documents.adjacent_chunks('s', chunks[1]['id'])) == 3
    assert repo.documents.located_blocks('s', page=3)
    assert repo.documents.located_blocks('s', page=99) == []
    sec = repo.documents.nodes('s', 'sections')[0]
    assert repo.documents.located_blocks('s', section_id=sec['id'])


def test_read_service_checks_every_request(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    source(repo, tmp_path/'files', 'body')
    calls = []
    def check(kb, doc):
        calls.append((kb, doc))
        if len(calls) > 1:
            raise PermissionError('revoked')
    service = DocumentReadService(repo.documents, check)
    service.read('version', 's')
    with pytest.raises(PermissionError):
        service.read('versions', 's')


def test_backfill_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    repo = SourceRepository(tmp_path/'db')
    source(repo, tmp_path/'files', 'body')
    import app.document_repository as module
    original = module.build_structure
    def fail(*args):
        raise ValueError('test failure')
    monkeypatch.setattr(module, 'build_structure', fail)
    with pytest.raises(ValueError):
        repo.documents.backfill('s')
    with repo.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM document_versions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM document_backfill_errors').fetchone()[0] == 1
    monkeypatch.setattr(module, 'build_structure', original)
    repo.documents.backfill('s')
    assert repo.documents.version('s').markdown == 'body'


def test_ingestion_creates_structure_for_supported_files(ingestion, sample_pdf, sample_docx, sample_txt):
    for path in (sample_pdf, sample_docx, sample_txt):
        s, _ = ingestion.ingest(path)
        assert s.parse_status == 'success'
        version = ingestion.repository.documents.version(s.source_id)
        assert version.markdown == Path(s.local_md_path).read_text()
        assert ingestion.repository.documents.nodes(s.source_id, 'chunks')
        if path == sample_docx:
            assert ingestion.repository.documents.nodes(s.source_id, 'tables')


def test_docx_merge_survives_local_parser(tmp_path):
    from docx import Document
    from app.document_parser import docx_to_markdown
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = 'merged'
    path = tmp_path/'merged.docx'
    doc.save(path)
    data = build_structure(docx_to_markdown(path), 'v')
    assert data['tables'][0].cells[0].row_span == 2


def test_v2_api_and_permission_isolation(tmp_path, sample_txt):
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash)
    with TestClient(app) as client:
        client.cookies.set(sessions.cookie_name, sessions.create('sansuokb', True, 'local'))
        with sample_txt.open('rb') as handle:
            assert client.post('/api/ingest', files=[('files', (sample_txt.name, handle, 'text/plain'))]).status_code == 202
        sid = client.get('/api/sources').json()[0]['source_id']
        prefix = '/api/v2/knowledge-bases/default-kb/documents/' + sid
        structure = client.get(prefix+'/structure')
        assert structure.status_code == 200
        chunk = structure.json()['chunks'][0]
        assert client.get(prefix+'/nodes/'+chunk['id']).status_code == 200
        assert client.get(prefix+'/content', params={'version_id': 'wrong'}).status_code == 404
        client.cookies.set(sessions.cookie_name, sessions.create('other', False, 'ldap'))
        assert client.get(prefix+'/versions').status_code == 403
        assert client.get(prefix+'/nodes/'+chunk['id']).status_code == 403
        assert client.get(prefix+'/artifacts/original').status_code == 403


def test_no_magic_separator_in_chunking():
    text = 'a\n__SPLIT__\nb'
    assert [c.content for c in build_structure(text, 'v')['chunks']] == [text]
    from app.clients.dify import DifyClient
    c = DifyClient('url', 'key', 'id', chunking_mode='custom', chunk_separator='\n__SPLIT__\n')
    assert c._process_rule()['process_rule']['mode'] == 'automatic'


def test_reparse_and_retry_do_not_duplicate_versions(ingestion, sample_txt):
    s, _ = ingestion.ingest(sample_txt)
    first = ingestion.repository.documents.version(s.source_id)
    ingestion.retry_failed(s.source_id, force_reparse=True)
    second = ingestion.repository.documents.version(s.source_id)
    assert second.version_no == first.version_no + 1
    ingestion.retry_failed(s.source_id)
    ingestion.repository.documents.backfill(s.source_id)
    assert len(ingestion.repository.documents.versions(s.source_id)) == 2
    # Simulate restart after parsing writes new content, before publishing the version.
    Path(s.local_md_path).write_text('recovered content')
    ingestion.retry_failed(s.source_id)
    assert ingestion.repository.documents.version(s.source_id).markdown == 'recovered content'
    assert len(ingestion.repository.documents.versions(s.source_id)) == 3


def test_legacy_revisions_migrate_when_current_file_missing(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    s = source(repo, tmp_path/'files', 'latest')
    repo.save_revision(s, 'old', 'local', 'first')
    repo.save_revision(s, 'latest', 'local', 'second')
    # Simulate a pre-V2 database, retaining only the original legacy history.
    with repo.connect() as conn:
        for table in ('documents', 'document_versions', 'document_nodes', 'document_version_artifacts', 'retrieval_chunks', 'chunk_builds', 'chunk_selection'):
            conn.execute('DELETE FROM ' + table)
    Path(s.local_md_path).unlink()
    repo.documents.backfill('s')
    repo.documents.backfill('s')
    assert [v.markdown for v in repo.documents.versions('s')] == ['old', 'latest']
    assert repo.documents.version('s').markdown == 'latest'


def test_mapping_lifecycle_and_chunk_ownership(tmp_path):
    from app.document_models import ExternalIndexMapping
    repo = SourceRepository(tmp_path/'db')
    s = source(repo, tmp_path/'files', 'old')
    repo.documents.backfill('s')
    first = repo.documents.version('s')
    s = repo.update('s', dify_document_id='remote', dify_dataset_id='dataset', dify_status='success')
    repo.save_revision(s, 'new', 'local', 'reparse')
    second = repo.documents.version('s')
    assert repo.documents.mappings('s') == []  # No premature external acknowledgment.
    repo.update('s', dify_status='processing')
    assert repo.documents.mappings('s', first.id)[0]['status'] == 'superseded'
    old_chunk = repo.documents.nodes('s', 'chunks', first.id)[0]
    with pytest.raises(KeyError):
        repo.documents.put_mapping('s', ExternalIndexMapping(id='bad', version_id=second.id,
            adapter='tantivy', index_version='future', status='pending', chunk_id=old_chunk['id']))


def test_tenant_reads_cross_kb_and_deleted_history(tmp_path):
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash)
    with TestClient(app) as client:
        admin = sessions.create(settings.admin_username, True, 'local')
        client.cookies.set(sessions.cookie_name, admin)
        assert client.post('/api/admin/organizations', json={'name': 'Acme', 'tenant_key': 'acme'}).status_code == 201
        assert client.put('/api/admin/organizations/members', json={'tenant_key': 'acme', 'user_id': 'ldap:alice', 'role': 'member'}).status_code == 200
        assert client.post('/api/admin/knowledge-bases', json={'knowledge_base_id': 'acme-kb', 'tenant_key': 'acme', 'name': 'Acme', 'dataset_id': 'ds', 'api_key': 'test'}).status_code == 200
        repo = app.state.registry.get('@kb:acme-kb').repository
        source(repo, tmp_path/'files', 'private')
        version = repo.documents.version('s')
        prefix = '/api/v2/knowledge-bases/acme-kb/documents/s'
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.get(prefix+'/content').status_code == 200
        assert client.get('/api/v2/knowledge-bases/default-kb/documents/s/content').status_code == 403
        client.cookies.set(sessions.cookie_name, admin)
        assert client.get('/api/v2/knowledge-bases/default-kb/documents/s/content').status_code == 404
        assert client.delete('/api/admin/organizations/acme/members/ldap%3Aalice').status_code == 204
        client.cookies.set(sessions.cookie_name, sessions.create('alice', False, 'ldap'))
        assert client.get(prefix+'/content').status_code == 403
        client.cookies.set(sessions.cookie_name, admin)
        repo.delete('s')
        assert client.get(prefix+'/content', params={'version_id': version.id}).status_code == 404


def test_legacy_separator_configuration_migration(tmp_path):
    from app.config_store import ConfigStore, MemorySecretStore
    from app.schemas import KnowledgeBaseInput
    secrets = MemorySecretStore()
    store = ConfigStore(tmp_path/'config.db', secrets)
    store.save_knowledge_base(KnowledgeBaseInput(knowledge_base_id='kb', name='KB',
        dataset_id='ds', chunking_mode='custom', chunk_separator='\n__SPLIT__\n'))
    for _ in range(2):
        store = ConfigStore(tmp_path/'config.db', secrets)
    with store.connect() as conn:
        archived = conn.execute('SELECT * FROM legacy_chunk_configuration').fetchall()
        assert len(archived) == 1
        assert archived[0]['chunk_separator'] == '\n__SPLIT__\n'
        assert conn.execute('SELECT chunking_mode FROM knowledge_bases').fetchone()[0] == 'automatic'


def test_parser_artifacts_preserved_without_false_geometry(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    s = source(repo, tmp_path/'files', 'Known\n')
    path = Path(s.source_path).parent/'mineru-content-list.json'
    path.write_text(json.dumps([{'text': 'Known', 'page_idx': 99}]))
    repo.save_revision(s, 'Known\n', 'local', 'local reparse')
    assert repo.documents.nodes('s', 'pages') == []
    assert repo.documents.artifact('s', 'mineru-content-list.json') == path.read_bytes()
    path.write_bytes(b'invalid-json')
    repo.save_revision(s, 'Known\n', 'mineru', 'incomplete structure')
    assert repo.documents.version('s').markdown == 'Known\n'
    assert repo.documents.artifact('s', 'mineru-content-list.json') == b'invalid-json'
