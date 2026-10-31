import concurrent.futures
import json
import multiprocessing
from pathlib import Path

import pytest

from app.chunk_builder import ChunkConfig
from app.db import SourceRepository
from app.search.exact_search import ContinuationInvalid
from app.search.index_manager import IndexManager
from app.search.service import LocalSearchService
from tests.test_document_repository import source
from tests.test_local_search import corpus


def collect(service, query, **kwargs):
    hits, pages, token = [], [], None
    for _ in range(10000):
        page = service.exact(query, continuation=token, **kwargs)
        hits.extend(page['results'])
        pages.append(page)
        if not page['partial']:
            return hits, pages
        token = page['continuation']
    pytest.fail('continuation did not complete')


def test_exact_2001_documents_and_page_order(tmp_path):
    repo = SourceRepository(tmp_path/'sources.db')
    # Genuine repository publication, unique IDs, no Tantivy candidate prerequisite.
    for i in range(2001):
        name = f'doc-{i:04}'
        s = source(repo, tmp_path/name, f'prefix {i} needle suffix', name)
        repo.save_revision(s, f'prefix {i} needle suffix', 'local', 'synthetic test')
    service = LocalSearchService(repo.documents, lambda k,d: None, 'alice')
    hits, pages = collect(service, 'needle', limit=100, timeout=2)
    assert [h['document_id'] for h in hits] == [f'doc-{i:04}' for i in range(2001)]
    assert len({(h['document_id'], h['start']) for h in hits}) == 2001
    assert pages[-1]['completed_documents'] == 2001
    assert pages[0]['continuation']


def test_literal_window_and_regex_subject_boundaries(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    text = 'x'*23+'ABCdef'+'x'*25+'ABCdef'+' end'
    s = source(repo, tmp_path/'files', text)
    repo.save_revision(s, text, 'local', 'test')
    service = LocalSearchService(repo.documents, lambda k,d: None, 'alice')
    hits, pages = collect(service, 'abcdef', case_sensitive=False, max_chars=25)
    assert [h['start'] for h in hits] == [23,54]
    assert all(p['scanned_chars'] <= 25 for p in pages)
    small = service.exact('x+$', regex_mode=True, max_chars=25)
    assert not small['results'] and small['reason'] == 'document_budget'
    assert small['required_chars'] == len(text)
    full = service.exact('x+$', regex_mode=True, max_chars=1000, continuation=small['continuation'])
    assert not full['partial'] and not full['results']
    hits, _ = collect(service, r'ABC[a-z]{3}', regex_mode=True, limit=1)
    assert [h['start'] for h in hits] == [23,54]


@pytest.mark.parametrize('change', ['query','user','scope','version','metadata','delete','revoke','tamper'])
def test_cursor_binding_and_invalidation(corpus, change):
    repo, _, _ = corpus
    denied = set()
    def auth(k,d):
        if d in denied:
            raise PermissionError()
    svc = LocalSearchService(repo.documents, auth, 'alice')
    token = svc.exact('e', limit=1)['continuation']
    kwargs = dict(query='e', limit=1, continuation=token)
    if change == 'query': kwargs['query'] = 'a'
    if change == 'user': svc = LocalSearchService(repo.documents, auth, 'bob')
    if change == 'scope': kwargs['document_ids'] = ['fuel']
    if change == 'version': repo.save_revision(repo.get('fuel'), 'new version', 'local', 'test')
    if change == 'metadata': repo.update('fuel', standard_title='changed')
    if change == 'delete': repo.delete('other')
    if change == 'revoke': denied.add('fuel')
    if change == 'tamper': kwargs['continuation'] = token[:-5]+'ABCDE'
    with pytest.raises(ContinuationInvalid):
        svc.exact(**kwargs)


def test_cursor_restart_replay_filters_and_history(corpus):
    repo, _, _ = corpus
    svc = LocalSearchService(repo.documents, lambda k,d: None, 'alice')
    page = svc.exact('e', limit=1)
    new = LocalSearchService(SourceRepository(repo.db_path).documents, lambda k,d: None, 'alice')
    a = new.exact('e', limit=1, continuation=page['continuation'])
    b = svc.exact('e', limit=1, continuation=page['continuation'])
    assert a['results'] == b['results']
    assert not svc.exact('e', filters={'metadata':{'standard_year':1900}})['results']
    old = repo.documents.version('fuel').id
    repo.save_revision(repo.get('fuel'), 'replacement', 'local', 'test')
    assert svc.exact('U-10Zr', versions={'fuel':old})['results']
    repo.delete('fuel')
    assert not svc.exact('U-10Zr', versions={'fuel':old})['results']


@pytest.mark.parametrize('mutation', ['title','metadata','version','builder','delete','aba'])
def test_publish_revalidates_all_facts(corpus, monkeypatch, mutation):
    repo, manager, service = corpus
    old = manager.current()['id']
    from app.search.tantivy_adapter import TantivyAdapter
    update = TantivyAdapter.update
    def race(self, *args):
        update(self, *args)
        if mutation == 'title': repo.update('fuel', standard_title='new title')
        if mutation == 'metadata': repo.update('fuel', standard_year=2031)
        if mutation == 'version': repo.save_revision(repo.get('fuel'), 'replacement', 'local', 'race')
        if mutation == 'builder': repo.documents.rebuild_chunks('fuel', config=ChunkConfig(300,30))
        if mutation == 'delete': repo.delete('fuel')
        if mutation == 'aba':
            repo.update('fuel', standard_title='temporary')
            repo.update('fuel', standard_title=None)
    monkeypatch.setattr(TantivyAdapter, 'update', race)
    with pytest.raises(RuntimeError, match='changed'):
        manager.sync(full=True)
    assert manager.current()['id'] == old
    assert manager.status()['stale']
    monkeypatch.setattr(TantivyAdapter, 'update', update)
    manager.sync()
    assert not manager.status()['stale']
    if mutation == 'delete':
        assert not service.search('U-10Zr')['results']


def test_status_updates_coalesce_and_revision_survives_ack(corpus):
    repo, manager, _ = corpus
    old = manager.current()['id']
    repo.update('fuel', dify_status='failed', error_message='remote failure', workflow_status='failed')
    assert manager.sync()['noop']
    assert manager.current()['id'] == old
    with repo.connect() as conn:
        before = conn.execute("SELECT revision FROM search_revisions WHERE document_id='fuel'").fetchone()[0]
    repo.update('fuel', standard_title='first')
    repo.update('fuel', standard_title='second')
    result = manager.sync()
    assert result['changed'] == 1
    with repo.connect() as conn:
        assert not conn.execute('SELECT 1 FROM search_changes').fetchone()
        after = conn.execute("SELECT revision FROM search_revisions WHERE document_id='fuel'").fetchone()[0]
    assert after == before+2
    repo.update('fuel', standard_title='third')
    with repo.connect() as conn:
        assert conn.execute("SELECT revision FROM search_changes WHERE document_id='fuel'").fetchone()[0] == after+1


def test_publish_ack_is_one_transaction(corpus, monkeypatch):
    repo, manager, service = corpus
    old = manager.current()['id']
    repo.update('fuel', standard_title='updated')
    ack = manager._ack
    def crash(conn, dirty):
        # Pointer has been updated in this transaction, but ACK fails.
        assert conn.execute('SELECT generation_id FROM lexical_current').fetchone()[0] != old
        raise RuntimeError('crash before ACK')
    monkeypatch.setattr(manager, '_ack', crash)
    with pytest.raises(RuntimeError, match='ACK'): manager.sync()
    assert manager.current()['id'] == old
    assert service.search('fuel')['results']
    with repo.connect() as conn: assert conn.execute('SELECT 1 FROM search_changes').fetchone()
    monkeypatch.setattr(manager, '_ack', ack)
    manager.sync()
    assert not manager.status()['stale']


def test_exception_after_commit_keeps_ready_generation(corpus, monkeypatch):
    repo, manager, service = corpus
    repo.update('fuel', standard_title='updated')
    monkeypatch.setattr(manager, '_cleanup_locked', lambda: (_ for _ in ()).throw(RuntimeError('postcommit')))
    with pytest.raises(RuntimeError): manager.sync()
    assert manager.current()['status'] == 'ready'
    assert not manager.status()['stale']
    assert service.search('fuel')['results']


def test_rollback_rejects_metadata_and_schema(corpus):
    repo, manager, _ = corpus
    old = manager.current()['id']
    repo.update('fuel', standard_title='new title')
    manager.sync()
    with pytest.raises(ValueError, match='metadata'): manager.rollback(old)
    with repo.connect() as conn:
        row = conn.execute('SELECT manifest FROM lexical_generations WHERE id=?', (old,)).fetchone()
        manifest = json.loads(row[0]); manifest['schema'] = -1
        conn.execute('UPDATE lexical_generations SET manifest=? WHERE id=?', (json.dumps(manifest),old))
    with pytest.raises(ValueError, match='schema'): manager.rollback(old)


def _hold_reader(database, ready, release):
    manager = IndexManager(SourceRepository(Path(database)).documents)
    with manager.reader() as (adapter, manifest, gid):
        ready.set()
        assert release.wait(30)
        assert adapter.search('fuel', manifest, 10, {})


def test_gc_respects_cross_process_reader(corpus):
    repo, manager, _ = corpus
    manager.retain_history = 0
    old = manager.current()['id']
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_reader, args=(str(repo.db_path), ready, release))
    process.start()
    try:
        assert ready.wait(20)
        manager.sync(full=True)
        assert (manager.root/old).is_dir()
        assert old in manager.cleanup()['busy']
    finally:
        release.set()
        process.join(20)
    assert process.exitcode == 0
    assert old in manager.cleanup()['removed']
    assert not (manager.root/old).exists()
    assert next(r for r in manager.state() if r['id']==old)['status'] == 'retired'


def test_gc_failed_orphans_and_disk_status(corpus):
    _, manager, _ = corpus
    orphan = manager.root/('a'*32); orphan.mkdir(); (orphan/'junk').write_bytes(b'x'*100)
    assert orphan.name in manager.cleanup()['removed']
    state = manager.status()
    assert state['usable'] and not state['stale']
    assert state['total_bytes'] >= state['current_bytes'] > 0


@pytest.mark.parametrize('identifier', ['GB/T 31840.3-2025','ZC-YJLHV22 26/35kV 3×500mm²','U-10Zr','U-Mo','99.95%','12345.67元'])
def test_technical_identifiers_raw_offsets(tmp_path, identifier):
    repo = SourceRepository(tmp_path/'db')
    text = '# 技术标题\n\n'+('IndivisibleText'*40)+'\n\n'+identifier+'\n'
    s = source(repo,tmp_path/'files',text)
    repo.save_revision(s,text,'local','test')
    IndexManager(repo.documents).sync()
    svc = LocalSearchService(repo.documents,lambda k,d:None)
    exact = svc.exact(identifier)['results'][0]
    assert text[exact['start']:exact['end']] == identifier
    assert svc.search(identifier)['results'][0]['document_id'] == 's'
    chunks = list(repo.documents.iter_chunks('s'))
    assert all(text[c['locator']['start']:c['locator']['end']] == c['content'] for c in chunks)


def test_html_table_spans_units_and_projection(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    text = '# 实验\n\n表 1 测试（单位：元）\n\n<table><tr><th>类别</th><th>金额</th></tr>'
    text += '<tr><td rowspan="2">U-Mo</td><td>12345.6700元</td></tr><tr><td>99.95%</td></tr>'
    text += ''.join(f'<tr><td>R{i}</td><td>{i}.123456元</td></tr>' for i in range(30))+'</table>'
    s = source(repo,tmp_path/'files',text)
    repo.save_revision(s,text,'local','test')
    repo.documents.rebuild_chunks('s',config=ChunkConfig(300,30))
    chunks = [c for c in repo.documents.iter_chunks('s') if c.get('table_id')]
    assert len(chunks)>1
    table = repo.documents.node('s',chunks[0]['table_id'])
    assert any(c['row_span']==2 for c in table['cells'])
    assert all(c['content_is_projection'] and c['cell_ids'] for c in chunks)
    assert any('12345.6700元' in c['content'] for c in chunks)
    assert all('金额' in c['content'] and '单位' in c['content'] for c in chunks)
    assert all(text[c['locator']['start']:c['locator']['end']] == table['raw'] for c in chunks)


def test_cursor_expiry_and_cross_knowledge_base(corpus, tmp_path, monkeypatch):
    repo, _, _ = corpus
    svc = LocalSearchService(repo.documents,lambda k,d:None,'alice')
    token = svc.exact('e',limit=1)['continuation']
    other = SourceRepository(tmp_path/'other'/'db','other')
    s = source(other,tmp_path/'other'/'files','needle')
    other.save_revision(s,'needle','local','test')
    with pytest.raises(ContinuationInvalid):
        LocalSearchService(other.documents,lambda k,d:None,'alice').exact('e',continuation=token)
    from app.search import exact_search
    now = exact_search.time.time()
    monkeypatch.setattr(exact_search.time,'time',lambda:now+901)
    with pytest.raises(ContinuationInvalid):
        svc.exact('e',continuation=token)


def test_regex_timeout_retains_safe_cursor(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    text = 'hit '+'a'*100000+'!'
    s = source(repo,tmp_path/'files',text)
    repo.save_revision(s,text,'local','test')
    svc = LocalSearchService(repo.documents,lambda k,d:None,'alice')
    first = svc.exact('hit|(a+)+$',regex_mode=True,timeout=0.01)
    assert [h['match'] for h in first['results']] == ['hit']
    assert first['partial'] and first['reason']=='time_budget'
    second = svc.exact('hit|(a+)+$',regex_mode=True,timeout=0.01,continuation=first['continuation'])
    assert not second['results'] and second['partial']
    assert second['next_offset'] == first['next_offset']


def test_noop_publication_does_not_lose_concurrent_change(corpus, monkeypatch):
    repo, manager, _ = corpus
    original = manager._snapshot
    def race():
        result = original()
        repo.update('fuel',standard_year=2040)
        return result
    monkeypatch.setattr(manager,'_snapshot',race)
    with pytest.raises(RuntimeError,match='changed'):manager.sync()
    with repo.connect() as conn:
        assert conn.execute("SELECT 1 FROM search_changes WHERE document_id='fuel'").fetchone()


def test_manifest_config_change_during_build(corpus, monkeypatch):
    repo, manager, _ = corpus
    from app.search import snapshot as snapshots
    from app.search.tantivy_adapter import TantivyAdapter
    update = TantivyAdapter.update
    old = manager.current()['id']
    def race(self,*args):
        update(self,*args)
        monkeypatch.setattr(snapshots,'INDEX_CONFIG','changed-while-building')
    monkeypatch.setattr(TantivyAdapter,'update',race)
    with pytest.raises(RuntimeError,match='changed'):manager.sync(full=True)
    assert manager.current()['id'] == old


def test_long_indivisible_sentence_and_builder_history(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    text = '# Long heading\n\n'+'无断句长文本'*900+'\n\n'+'English words without punctuation '*300
    s = source(repo,tmp_path/'files',text)
    repo.save_revision(s,text,'local','test')
    old = list(repo.documents.iter_chunks('s'))
    repo.documents.rebuild_chunks('s',config=ChunkConfig(200,20))
    chunks = list(repo.documents.iter_chunks('s'))
    assert max(len(c['content']) for c in chunks)>200
    assert all(c['section_titles']==['Long heading'] for c in chunks)
    assert all(text[c['locator']['start']:c['locator']['end']]==c['content'] for c in chunks)
    assert repo.documents.node('s',old[-1]['id']) == old[-1]


def test_exact_embedded_nul_unicode_and_window_offsets(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    text = '前缀😀\x00'+'长'*30+'GB/T 31840.3-2025'+'尾'*30+'\x00GB/T 31840.3-2025'
    s = source(repo,tmp_path/'files',text)
    repo.save_revision(s,text,'local','test')
    svc = LocalSearchService(repo.documents,lambda k,d:None,'alice')
    hits, pages = collect(svc,'GB/T 31840.3-2025',max_chars=23)
    assert len(hits)==2
    assert [text[h['start']:h['end']] for h in hits]==['GB/T 31840.3-2025']*2
    regex_hits, _ = collect(svc,r'31840\.3-2025',regex_mode=True,limit=1)
    assert len(regex_hits)==2
    page = svc.exact('not present')
    assert page['scanned_chars']==len(text) and not page['partial']


def test_tail_overlap_fix_is_versioned_and_keeps_old_build(tmp_path):
    repo = SourceRepository(tmp_path/'db')
    text = '# 标题\n\n'+('A sentence.\n'*200)
    s = source(repo,tmp_path/'files',text)
    repo.save_revision(s,text,'local','test')
    legacy = repo.documents.rebuild_chunks('s',config=ChunkConfig(300,60,'structure-v2'))
    old = list(repo.documents.iter_chunks('s'))
    new = repo.documents.rebuild_chunks('s',config=ChunkConfig(300,60))
    chunks = list(repo.documents.iter_chunks('s'))
    assert new != legacy
    assert len(chunks)<len(old)
    assert len([c for c in chunks if c['locator']['end']==len(text)])==1
    assert repo.documents.node('s',old[-1]['id'])==old[-1]
    assert all(a['locator']['end']>=b['locator']['start'] for a,b in zip(chunks,chunks[1:]))


def test_rest_continuation_principal_and_status_compatibility(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth import SessionManager
    from tests.test_api import offline_settings
    settings = offline_settings(tmp_path)
    settings.auth_enabled = True
    app = create_app(settings)
    sessions = SessionManager(settings.admin_username,settings.admin_password_hash)
    with TestClient(app) as client:
        admin = sessions.create(settings.admin_username,True,'local')
        client.cookies.set(sessions.cookie_name,admin)
        client.post('/api/admin/organizations',json={'name':'Acme','tenant_key':'acme'})
        client.put('/api/admin/organizations/members',json={'tenant_key':'acme','user_id':'ldap:alice','role':'member'})
        client.post('/api/admin/knowledge-bases',json={'knowledge_base_id':'acme-kb','tenant_key':'acme','name':'Acme','dataset_id':'ds','api_key':'test'})
        repo = app.state.registry.get('@kb:acme-kb').repository
        s = source(repo,tmp_path/'files','needle needle needle')
        repo.save_revision(s,'needle needle needle','local','test')
        url = '/api/v2/knowledge-bases/acme-kb/local-search'
        assert client.post(url+'/sync',json={}).status_code==200
        assert isinstance(client.get(url+'/status').json(),list)
        assert client.get(url+'/diagnostics').json()['usable']
        assert client.post(url+'/cleanup').status_code==200
        client.cookies.set(sessions.cookie_name,sessions.create('alice',False,'ldap'))
        first = client.post(url+'/exact',json={'query':'needle','limit':1}).json()
        body = dict(query='needle',limit=1,continuation=first['continuation'])
        second = client.post(url+'/exact',json=body)
        assert second.status_code==200 and second.json()['results'][0]['start']==7
        assert client.get(url+'/diagnostics').status_code==403
        client.cookies.set(sessions.cookie_name,admin)
        assert client.post(url+'/exact',json=body).status_code==409


def test_mutation_during_exact_scan_returns_no_stale_result(corpus, monkeypatch):
    repo, _, _ = corpus
    from app.search import service as module
    scan = module.exact_search
    svc = LocalSearchService(repo.documents,lambda k,d:None,'alice')
    def race(*args,**kwargs):
        result = scan(*args,**kwargs)
        repo.delete('fuel')
        return result
    monkeypatch.setattr(module,'exact_search',race)
    with pytest.raises(ContinuationInvalid):
        svc.exact('U-10Zr')


def test_other_manager_cache_sees_coalesced_revision(corpus):
    repo, manager, service = corpus
    old = service.search('fuel')['generation_id']
    repo.update('fuel',standard_title='temporary')
    repo.update('fuel',standard_title=None)
    assert manager.sync()['noop']
    response = service.search('fuel')
    assert response['generation_id']==old and not response['stale']
