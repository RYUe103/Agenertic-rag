import concurrent.futures
import json
from pathlib import Path

import pytest
from app.chunk_builder import ChunkConfig
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.search.service import LocalSearchService
from tests.test_document_repository import source


@pytest.fixture
def corpus(tmp_path):
    repo = SourceRepository(tmp_path/'sources.db')
    texts = {
        'cable':'<!-- page: 3 -->\n# 电缆标准 GB/T 31840.3-2025\n\n电缆型号 ZC-YJLHV22 26/35kV 3×500mm²，合格率为99.95%。\n',
        'fuel':'# U-10Zr fuel\n\nU-10Zr metallic fuel performance in a nuclear reactor.\n',
        'table':'# 项目预算（单位：元）\n\n| 年份 | 金额 | 比例 |\n|---|---|---|\n| 2025 | 12345.67元 | 99.95% |\n',
        'other':'# Meeting minutes\n\nWe discussed cable suppliers and delivery.\n',
    }
    for name,text in texts.items():
        source(repo,tmp_path/name,text,name)
    repo.documents.backfill_all()
    manager = IndexManager(repo.documents)
    manager.sync()
    service = LocalSearchService(repo.documents,lambda kb,doc:None)
    return repo,manager,service


@pytest.mark.parametrize('query,expected',[
    ('电缆','cable'),('GB/T 31840.3-2025','cable'),('ZC-YJLHV22','cable'),
    ('26/35kV','cable'),('3×500mm²','cable'),('U-10Zr','fuel'),
    ('NUCLEAR reactor','fuel'),('预算','table'),('12345.67元','table'),('Meeting minutes','other'),
])
def test_real_bm25_mixed_language_and_identifiers(corpus,query,expected):
    _,_,service = corpus
    hits = service.search(query)['results']
    assert hits and hits[0]['document_id']==expected
    assert hits[0]['score']>0 and hits[0]['chunk_id'] and hits[0]['locator']['block_ids']


def test_filters_before_top_k_and_permissions(corpus):
    repo,_,_ = corpus
    calls = []
    def authorize(kb,doc):
        calls.append(doc)
        if doc=='cable':
            raise PermissionError('not granted')
    service=LocalSearchService(repo.documents,authorize)
    hits=service.search('cable 电缆',top_k=1)['results']
    assert hits[0]['document_id']=='other'
    assert 'cable' in calls
    assert service.search('电缆',filters={'page':99})['results']==[]
    assert service.search('电缆',filters={'page':3})['results']==[]
    assert service.search('fuel',document_ids=['other'])['results']==[]


def test_exact_regex_case_and_budgets(corpus):
    _,_,service=corpus
    hit=service.exact('GB/T 31840.3-2025')['results'][0]
    assert hit['document_id']=='cable' and hit['chunk_ids']
    assert service.exact('NUCLEAR',case_sensitive=True)['results']==[]
    assert service.exact('NUCLEAR',case_sensitive=False)['results']
    assert service.exact(r'\d+\.\d+%',regex_mode=True)['results']
    assert service.exact('a',max_chars=3)['partial']
    with pytest.raises(ValueError):service.exact('x'*257)
    with pytest.raises(ValueError):service.exact(r'(x)\1',regex_mode=True)


def test_immutable_build_upgrade_rollback_and_original_versions(corpus):
    repo,manager,service=corpus
    version=repo.documents.version('cable')
    original=repo.documents.nodes('cable','chunks')[0]['id']
    old=repo.documents.selected_builder(version.id)
    new=repo.documents.rebuild_chunks('cable',config=ChunkConfig(400,60))
    assert new!=old
    assert repo.documents.node('cable',original)
    assert len(repo.documents.versions('cable'))==1
    assert service.search('电缆')['results'][0]['builder_version']==old
    manager.sync()
    assert service.search('电缆')['results'][0]['builder_version']==new
    repo.documents.select_chunk_build('cable',version.id,old)
    manager.sync()
    assert service.search('电缆')['results'][0]['builder_version']==old
    assert manager.sync()['noop']


def test_version_publish_and_delete_pre_filter(corpus):
    repo,manager,service=corpus
    old=repo.documents.version('cable').id
    s=repo.get('cable')
    repo.save_revision(s,'# 新版\n\n新的验收要求 replacementtoken。','local','updated')
    assert service.search('电缆')['results'][0]['version_id']==old
    manager.sync()
    assert service.search('replacementtoken')['results'][0]['version_id']!=old
    assert not service.search('电缆')['results']
    repo.delete('cable')
    assert not service.search('replacementtoken')['results']
    assert manager.sync()['deleted']==1
    assert not service.exact('replacementtoken')['results']


def test_corruption_recovery_restart_and_failed_build(corpus,monkeypatch):
    repo,manager,service=corpus
    assert LocalSearchService(SourceRepository(repo.db_path).documents,lambda k,d:None).search('fuel')['results']
    current=manager.current()['id']
    (manager.root/current/'meta.json').write_text('corrupt')
    with pytest.raises(RuntimeError):service.search('fuel')
    manager.sync()
    assert service.search('fuel')['results']
    previous=manager.current()['id']
    from app.search.tantivy_adapter import TantivyAdapter
    original=TantivyAdapter.update
    monkeypatch.setattr(TantivyAdapter,'update',lambda *a: (_ for _ in ()).throw(RuntimeError('failure')))
    with pytest.raises(RuntimeError):manager.sync(full=True)
    assert manager.current()['id']==previous
    assert service.search('fuel')['results']
    monkeypatch.setattr(TantivyAdapter,'update',original)
    manager.sync(full=True)
    assert manager.current()['id']!=previous


def test_structure_long_paragraph_overlap_and_table_rows(tmp_path):
    repo=SourceRepository(tmp_path/'db')
    paragraph='完整句子包含技术指标和上下文。'*100
    table='| 年份 | 金额 |\n|---|---|\n'+''.join(f'| {2000+i} | {i}.123456元 |\n' for i in range(80))
    s=source(repo,tmp_path/'files','# 测试标题\n\n'+paragraph+'\n\n'+table)
    repo.documents.backfill('s')
    builder=repo.documents.rebuild_chunks('s',config=ChunkConfig(300,60))
    chunks=list(repo.documents.iter_chunks('s'))
    prose=[c for c in chunks if not c.get('table_id') and '完整句子' in c['content']]
    assert all(c['content'].rstrip().endswith('。') for c in prose)
    assert prose[1]['locator']['start']<prose[0]['locator']['end']
    tables=[c for c in chunks if c.get('table_id')]
    assert len(tables)>1
    assert all('年份' in c['content'] and c['cell_ids'] for c in tables)
    assert any('79.123456元' in c['content'] for c in tables)
    assert all(c['section_titles']==['测试标题'] for c in chunks if '完整句子' in c['content'])
    assert repo.documents.chunks_by_ids('s',[chunks[0]['id']])[0]==chunks[0]
    assert len(repo.documents.chunks_page('s',limit=2))==2
    assert len(repo.documents.adjacent_chunks('s',chunks[1]['id']))==3
    before=len(chunks)
    repo.documents.rebuild_chunks('s',config=ChunkConfig(300,60))
    assert len(list(repo.documents.iter_chunks('s')))==before


def test_concurrent_queries_during_rebuild(corpus):
    repo,manager,service=corpus
    def search(_):
        return LocalSearchService(repo.documents,lambda k,d:None).search('电缆')['results'][0]['document_id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        future=pool.submit(manager.sync,True)
        assert all(value=='cable' for value in pool.map(search,range(12)))
        future.result()


def test_delete_remote_failure_denies_reads_and_retries(ingestion,sample_txt):
    s,_=ingestion.ingest(sample_txt)
    s=ingestion.repository.update(s.source_id,dify_document_id='real-id',dify_dataset_id=ingestion.dify.dataset_id)
    def fail(_): raise RuntimeError('remote unavailable')
    ingestion.dify.delete_document=fail
    with pytest.raises(RuntimeError):ingestion.delete_source(s.source_id)
    with pytest.raises(KeyError):ingestion.repository.get(s.source_id)
    with pytest.raises(KeyError):ingestion.repository.documents.version(s.source_id)
    assert ingestion.repository.list_all()==[]
    ingestion.dify.delete_document=lambda _:None
    ingestion.delete_source(s.source_id)
    ingestion.delete_source(s.source_id)
    assert not Path(s.source_path).exists()


def test_generation_rollback_and_deleted_generation_rejected(corpus):
    repo,manager,service=corpus
    old=manager.current()['id']
    manager.sync(config=ChunkConfig(400,40))
    assert manager.current()['id']!=old
    manager.rollback(old)
    assert service.search('电缆')['generation_id']==old
    repo.delete('cable')
    with pytest.raises(ValueError):manager.rollback(old)


def test_stale_build_cannot_publish_and_interrupted_build_recovers(corpus,monkeypatch):
    repo,manager,service=corpus
    from app.search.tantivy_adapter import TantivyAdapter
    original=TantivyAdapter.update
    old=manager.current()['id']
    def race(self,*args):
        original(self,*args)
        repo.save_revision(repo.get('fuel'),'replacement after build','local','concurrent update')
    monkeypatch.setattr(TantivyAdapter,'update',race)
    with pytest.raises(RuntimeError):manager.sync(full=True)
    assert manager.current()['id']==old
    monkeypatch.setattr(TantivyAdapter,'update',original)
    with repo.connect() as conn:
        conn.execute("INSERT INTO lexical_generations VALUES('interrupted','building','{}',NULL)")
    IndexManager(SourceRepository(repo.db_path).documents).sync()
    assert service.search('replacement')['results']
    assert next(r for r in manager.state() if r['id']=='interrupted')['status']=='failed'


def _process_search(database):
    repo=SourceRepository(Path(database))
    manager=IndexManager(repo.documents)
    manager.sync()
    return LocalSearchService(repo.documents,lambda kb,doc:None).search('fuel')['results'][0]['document_id']


def test_multiple_processes_share_index(corpus):
    import multiprocessing
    repo,_,_=corpus
    with concurrent.futures.ProcessPoolExecutor(max_workers=2,mp_context=multiprocessing.get_context('spawn')) as pool:
        assert list(pool.map(_process_search,[str(repo.db_path)]*2))==['fuel','fuel']


def test_regex_timeout_and_cross_chunk_match(tmp_path):
    repo=SourceRepository(tmp_path/'db')
    text='A sentence ends.\n\nNext paragraph starts.\n'+'a'*100000+'!'
    source(repo,tmp_path/'files',text)
    repo.documents.backfill('s')
    service=LocalSearchService(repo.documents,lambda kb,doc:None)
    result=service.exact('ends.\n\nNext')
    assert result['results'] and result['results'][0]['chunk_ids']
    result=service.exact('(a+)+$',regex_mode=True,timeout=0.01)
    assert result['partial'] and result['reason']=='time_budget'
    with pytest.raises(ValueError):service.exact('[',regex_mode=True)


def test_title_weight_and_ranking(tmp_path):
    repo=SourceRepository(tmp_path/'db')
    source(repo,tmp_path/'a','# Superconductivity\n\nShort summary.','a')
    source(repo,tmp_path/'b','# Other topic\n\nA study of superconductivity in other materials.','b')
    repo.documents.backfill_all();IndexManager(repo.documents).sync()
    hits=LocalSearchService(repo.documents,lambda kb,doc:None).search('superconductivity')['results']
    assert hits[0]['document_id']=='a'


def test_table_cell_locations_and_caption(corpus):
    repo,manager,service=corpus
    hit=service.search('12345.67元')['results'][0]
    assert hit['cell_matches'][0]['row']==1
    assert hit['cell_matches'][0]['column']==1
    assert hit['cell_matches'][0]['bbox'] is None
    table=repo.documents.node('table',hit['locator']['table_ids'][0])
    assert '12345.67元' in table['raw']


def test_legacy_chunks_coexist_with_structure_build(tmp_path):
    repo=SourceRepository(tmp_path/'db')
    source(repo,tmp_path/'files','legacy text')
    repo.documents.backfill('s');vid=repo.documents.version('s').id
    legacy={'id':vid+':chunk:0','version_id':vid,'ordinal':0,'content':'legacy text','builder_version':'chars-1800-v1',
            'locator':{'version_id':vid,'start':0,'end':11,'block_ids':[],'page_numbers':[],'section_ids':[],'table_ids':[]}}
    with repo.connect() as conn:
        conn.execute('DELETE FROM chunk_selection')
        conn.execute('DELETE FROM retrieval_chunks')
        conn.execute('DELETE FROM chunk_builds')
        conn.execute('INSERT INTO document_nodes VALUES(?,?,?,?,?)',(legacy['id'],vid,'chunks',0,json.dumps(legacy)))
    service=LocalSearchService(repo.documents,lambda k,d:None)
    assert service.exact('legacy')['results'][0]['chunk_ids']==[legacy['id']]
    IndexManager(repo.documents).sync()
    assert repo.documents.node('s',legacy['id'])==legacy
    assert repo.documents.version('s').id==vid
    assert service.search('legacy')['results'][0]['chunk_id']!=legacy['id']


def test_search_api_tenant_isolation_and_limits(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth import SessionManager
    from tests.test_api import offline_settings
    settings=offline_settings(tmp_path); settings.auth_enabled=True
    app=create_app(settings); sessions=SessionManager(settings.admin_username,settings.admin_password_hash)
    with TestClient(app) as client:
        admin=sessions.create(settings.admin_username,True,'local')
        client.cookies.set(sessions.cookie_name,admin)
        client.post('/api/admin/organizations',json={'name':'Acme','tenant_key':'acme'})
        client.put('/api/admin/organizations/members',json={'tenant_key':'acme','user_id':'ldap:alice','role':'member'})
        client.post('/api/admin/knowledge-bases',json={'knowledge_base_id':'acme-kb','tenant_key':'acme','name':'Acme','dataset_id':'ds','api_key':'test'})
        repo=app.state.registry.get('@kb:acme-kb').repository
        source(repo,tmp_path/'private','Private reactor data')
        repo.documents.backfill_all()
        url='/api/v2/knowledge-bases/acme-kb/local-search'
        assert client.post(url+'/sync',json={}).status_code==200
        client.cookies.set(sessions.cookie_name,sessions.create('alice',False,'ldap'))
        assert client.post(url+'/bm25',json={'query':'reactor'}).json()['results']
        assert client.post(url+'/exact',json={'query':'reactor'}).json()['results']
        assert client.post(url+'/sync',json={}).status_code==403
        assert client.post(url+'/exact',json={'query':'[' ,'regex_mode':True}).status_code==400
        assert client.post(url+'/bm25',json={'query':'x','top_k':1000}).status_code==422
        client.cookies.set(sessions.cookie_name,sessions.create('bob',False,'ldap'))
        assert client.post(url+'/bm25',json={'query':'reactor'}).status_code==403
        assert client.post(url+'/exact',json={'query':'reactor'}).status_code==403
        client.cookies.set(sessions.cookie_name,admin)
        client.delete('/api/admin/organizations/acme/members/ldap%3Aalice')
        client.cookies.set(sessions.cookie_name,sessions.create('alice',False,'ldap'))
        assert client.post(url+'/bm25',json={'query':'reactor'}).status_code==403
