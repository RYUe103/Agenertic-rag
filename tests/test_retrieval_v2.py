"""Real local Tantivy/Repository; explicitly fake vector transport for contract tests."""
import json
import time
import pytest
from fastapi.testclient import TestClient
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.document_models import ExternalIndexMapping
from app.retrieval_v2.models import RetrievalRequest, Evidence, FusionConfig
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.fusion import fuse, diversify
from app.retrieval_v2.planner import plan
from app.retrieval_v2.adapters import DifyVectorAdapter, BackendError
from app.retrieval_v2.tools import ToolService, catalog
from app.tool_registry import ToolConfigStore, ToolCallStore
from tests.test_document_repository import source


@pytest.fixture
def setup(tmp_path):
    repo=SourceRepository(tmp_path/'db','kb')
    source(repo,tmp_path/'a','<!-- page: 3 -->\n# Fuel\n\nU-10Zr reactor performance.\n\n'+
           'A detailed fuel discussion. '*200+'\n\n## Data\n\n| Year | kg |\n|---|---|\n| 2019 | 42 |\n','a')
    source(repo,tmp_path/'b','# Finance\n\nRevenue increased by 123 million.\n','b')
    repo.documents.backfill_all(); IndexManager(repo.documents).sync()
    service=UnifiedRetrievalService({'kb':repo.documents},AccessPolicy(lambda k:None,'user'))
    return repo,service


def request(query='fuel',**kwargs):
    return RetrievalRequest(query=query,scopes=[{'knowledge_base_id':'kb'}],**kwargs)


@pytest.mark.parametrize('mode',['lexical','vector','hybrid','auto','exact'])
def test_modes(setup,mode):
    repo,service=setup
    result=service.search(request(mode=mode))
    if mode=='vector':
        assert result.status=='insufficient_evidence'
        assert result.diagnostics['backends']['kb:vector']['code']=='vector_unconfigured'
    else:
        assert result.results
        assert all(h.verification_status=='verified_current' for h in result.results)
        assert all(h.version_id==repo.documents.version(h.document_id).id for h in result.results)


@pytest.mark.parametrize('query,kind,channels',[
    ('GB/T 31840.3-2025 规定了什么','identifier',['lexical','exact']),
    ('找到所有出现 U-10Zr 的位置','occurrence',['exact']),
    ('What is the difference between 2018 and 2019','numeric_table',['lexical','vector']),
    ('金属燃料辐照肿胀的主要影响因素有哪些','semantic',['lexical','vector']),
])
def test_planner(query,kind,channels):
    result=plan(request(query))
    assert result['kind']==kind and result['channels']==channels
    assert plan(request(query,mode='vector'))['channels']==['vector']


def test_scopes_metadata_and_multi_kb(setup,tmp_path):
    repo,service=setup
    assert not service.search(RetrievalRequest(query='fuel',mode='lexical',scopes=[{'knowledge_base_id':'kb','document_ids':['b']}])).results
    assert not service.search(request(mode='lexical',filters={'metadata':{'filename':'missing'}})).results
    assert service.search(request(mode='lexical',filters={'metadata':{'filename':'a.txt'}})).results
    other=SourceRepository(tmp_path/'other-kb'/'sources.db','other')
    source(other,tmp_path/'otherfiles','fuel in second knowledge base','a');other.documents.backfill_all()
    IndexManager(other.documents).sync();service.repositories['other']=other.documents
    result=service.search(RetrievalRequest(query='fuel',mode='lexical',scopes=[{'knowledge_base_id':'kb'},{'knowledge_base_id':'other'}]))
    assert {h.knowledge_base_id for h in result.results}=={'kb','other'}
    service.access.authorize_kb=lambda kb:(_ for _ in ()).throw(PermissionError()) if kb=='other' else None
    with pytest.raises(PermissionError):
        service.search(RetrievalRequest(query='fuel',scopes=[{'knowledge_base_id':'other'}]))


def test_stale_and_explicit_history(setup):
    repo,service=setup
    old=repo.documents.version('a').id
    repo.save_revision(repo.get('a'),'replacement material','local','reparse')
    result=service.search(request(mode='lexical'))
    assert not result.results and result.diagnostics['rejected']['stale']
    history=service.search(RetrievalRequest(query='fuel',mode='lexical',scopes=[{'knowledge_base_id':'kb','versions':{'a':old}}]))
    assert history.results and history.results[0].verification_status=='verified_historical'
    repo.delete('a')
    assert not service.search(RetrievalRequest(query='fuel',mode='exact',scopes=[{'knowledge_base_id':'kb','versions':{'a':old}}])).results


def test_permission_revalidation_and_batched_acl(setup):
    repo,service=setup
    calls=[]
    def acl(kb,ids):
        calls.append(ids)
        return ids if len(calls)==1 else ['b']
    service.access.authorize_documents=acl
    result=service.search(request(mode='lexical'))
    assert not result.results and not result.documents
    assert len(calls)==2
    assert result.diagnostics['rejected']['concurrent_scope_change']==1


def test_kb_revocation_during_query_fails_closed(setup):
    repo,service=setup
    calls=[]
    def check(kb):
        calls.append(kb)
        if len(calls)>1: raise PermissionError()
    service.access.authorize_kb=check
    with pytest.raises(PermissionError):service.search(request(mode='lexical'))


class FakeVector:
    def __init__(self, records):self.records=records
    def search(self,*args):return {'candidates':self.records}


def mapped_vector(repo, text='U-10Zr reactor performance.', **segment):
    vid=repo.documents.version('a').id
    repo.documents.put_mapping('a',ExternalIndexMapping(id='map',version_id=vid,adapter='dify',
        index_version=vid,status='completed',external_document_id='remote',dataset_id='dataset',
        binding_method='upload_ack',remote_status='completed',remote_enabled=True,remote_archived=False,
        source_content_hash=repo.documents.version('a').content_hash,
        submitted_content_hash=repo.documents.version('a').content_hash))
    return {'dataset_id':'dataset','external':{'score':.9,'segment':{'id':'segment',
        'document_id':'remote','enabled':True,'status':'completed','content':text,**segment}}}


def test_external_unique_text_and_no_fabricated_segment_chunk(setup):
    repo,service=setup
    service.vectors['kb']=FakeVector([mapped_vector(repo)])
    result=service.search(request(mode='vector'))
    assert result.results and result.results[0].chunk_id!='segment'
    h=result.results[0]
    r=h.matched_ranges[0]
    assert repo.documents.version('a').markdown[r['start']:r['end']]=='U-10Zr reactor performance.'


@pytest.mark.parametrize('change',[
    {'document_id':'unknown'},{'id':None},{'enabled':False},{'status':'indexing'},
    {'content':'not source text'},{'content':'A detailed fuel discussion.'},
    {'document':{'id':'other-remote'}},
])
def test_untrusted_external_rejected(setup,change):
    repo,service=setup
    service.vectors['kb']=FakeVector([mapped_vector(repo,**change)])
    result=service.search(request(mode='vector'))
    assert not result.results and result.diagnostics['rejected']['unverified_external']==1
    assert 'not source text' not in result.model_dump_json()


def test_external_unauthorized_and_old_version(setup):
    repo,service=setup
    record=mapped_vector(repo)
    service.vectors['kb']=FakeVector([record])
    service.access.authorize_documents=lambda kb,ids:['b']
    assert not service.search(request(mode='vector')).results
    service.access.authorize_documents=None
    repo.save_revision(repo.get('a'),'U-10Zr reactor performance.\nNew version','local','new')
    assert not service.search(request(mode='vector')).results


def ev(cid='c',doc='d',adapter='lexical',rank=1):
    return Evidence(knowledge_base_id='kb',document_id=doc,version_id='v',chunk_id=cid,
        source_adapter=adapter,source_rank=rank,source_score=9999,content='source',verification_status='verified_current')


def test_rrf_math_dedup_weights_diversity_and_stability():
    config=FusionConfig(vector_weight=2)
    ranked,docs,dedup=fuse({'lexical':[ev(),ev()], 'vector':[ev(adapter='vector',rank=2)]},config)
    assert len(ranked)==1 and dedup==2
    assert ranked[0].fused_score==pytest.approx(1/61+2/62)
    assert len(ranked[0].provenance)==2
    assert docs[0]['candidate_count']==1
    many=[ev(str(i)) for i in range(5)]+[ev('other','other')]
    assert len(diversify(many,2,10))==3
    a=fuse({'lexical':[ev('b'),ev('a')]},config)[0]
    assert [h.chunk_id for h in a]==['a','b']


def test_exact_occurrences_continuation_nul_and_bounds(setup):
    repo,service=setup
    repo.save_revision(repo.get('b'),'alpha\0needle\nneedle\nneedle','local','nul')
    body=request('needle',mode='exact',budget={'backend_candidates':1})
    result=service.search(body)
    assert result.results[0].locator['start']==6
    cursor=result.exact['kb']['continuation']
    assert cursor
    next_page=service.search(body.model_copy(update={'continuations':{'kb':cursor}}))
    assert next_page.results[0].locator['start']==13
    service.access.principal='different-user'
    invalid=service.search(body.model_copy(update={'continuations':{'kb':cursor}}))
    assert invalid.diagnostics['backends']['kb:exact']['error']=='ContinuationInvalid'


def test_locations_table_and_bounded_context(setup):
    repo,service=setup
    sec=repo.documents.nodes('a','sections')[-1]['id']
    result=service.search(request('2019',mode='lexical',filters={'section_id':sec},expand_neighbors=2,budget={'context_chars':50}))
    assert result.results
    assert result.results[0].table_ids
    assert sum(len(x.content) for x in result.results+result.expansions)<=50
    assert not service.search(request(mode='lexical',filters={'page':999})).results


def test_reranker_failure_and_real_interface_order(setup):
    repo,service=setup
    class Fail:
        def rank(self,*args):raise RuntimeError('secret')
    baseline=service.search(request(mode='lexical'))
    service.reranker=Fail()
    failed=service.search(request(mode='lexical'))
    assert [h.key() for h in baseline.results]==[h.key() for h in failed.results]
    assert failed.diagnostics['reranker']['status']=='failed'
    assert 'secret' not in failed.model_dump_json()
    class Ranking:
        def rank(self,q,texts,timeout):return [(i,float(i)) for i in range(len(texts))]
    service.reranker=Ranking()
    ranked=service.search(request(mode='lexical'))
    assert ranked.results[0].rerank_score is not None
    assert ranked.results[0].fused_score>0


def test_backend_and_reranker_timeouts_preserve_local_hits(setup):
    repo,service=setup
    class Slow:
        def search(self,*args):time.sleep(.6);return {'candidates':[]}
        def rank(self,*args):time.sleep(.6);return []
    service.vectors['kb']=Slow();service.reranker=Slow()
    started=time.monotonic()
    result=service.search(request(mode='hybrid',budget={'timeout_seconds':.3,'rerank_seconds':.03}))
    assert time.monotonic()-started<.5
    assert result.results and result.diagnostics['backends']['kb:vector']['status']=='timeout'


def test_dify_contract_semantic_only_and_capability(monkeypatch):
    from app.clients.dify import DifyClient
    adapter=DifyVectorAdapter(DifyClient('https://example.test/v1','not-a-secret','ds',search_method='hybrid_search'))
    calls=[]
    def transport(method,path,deadline,**kwargs):
        calls.append((method,path,kwargs))
        return {'indexing_technique':'high_quality','provider':'vendor'} if method=='GET' else {'records':[]}
    monkeypatch.setattr(adapter,'_json',transport)
    assert adapter.search('question',5,time.monotonic()+1)['candidates']==[]
    assert calls[1][2]['json']['retrieval_model']['search_method']=='semantic_search'
    monkeypatch.setattr(adapter,'_json',lambda *a,**k:{'indexing_technique':'economy'})
    with pytest.raises(BackendError,match='semantic_capability'):adapter.search('q',5,time.monotonic()+1)
    with pytest.raises(BackendError,match='250'):adapter.search('a'*251,5,time.monotonic()+1)


def test_tools_read_governance_audit_and_large_inputs(setup,tmp_path):
    repo,service=setup
    config=ToolConfigStore(tmp_path/'tools'); audit=ToolCallStore(tmp_path/'calls')
    tools=ToolService(service,config,audit,lambda p:None,'user')
    values={'knowledge_base_id':'kb','document_id':'a'}
    assert len(catalog())==17
    assert tools.invoke('v2.read_document',values).data['content']
    assert tools.invoke('v2.get_document_outline',values).data['items']
    chunk=repo.documents.nodes('a','chunks')[0]
    for name in ['get_chunk','expand_evidence','get_adjacent_chunks','get_evidence_context']:
        assert tools.invoke('v2.'+name,{**values,'node_id':chunk['id']}).status=='success'
    table=repo.documents.nodes('a','tables')[0]
    for name in ['get_table','read_document_table','get_table_cells']:
        assert tools.invoke('v2.'+name,{**values,'node_id':table['id']}).data['items']
    assert tools.invoke('v2.get_chunk',{**values,'node_id':repo.documents.nodes('b','chunks')[0]['id']}).status=='error'
    config.update('v2.read_document',False,False,'tool.invoke')
    assert tools.invoke('v2.read_document',values).error['code']=='forbidden'
    assert tools.invoke('v2.read_document',{**values,'max_chars':100001}).error['code']=='invalid_input'
    assert audit.stats('kb')['v2.read_document']


def test_minimal_rest_routes_and_existing_interface(tmp_path):
    from tests.test_api import offline_settings
    from app.main import create_app
    app=create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/v2/retrieval/tools').status_code==200
        kb=client.get('/api/admin/knowledge-bases').json()[0]['knowledge_base_id']
        r=client.post('/api/v2/retrieval/search',json={'query':'test','mode':'exact','scopes':[{'knowledge_base_id':kb}]})
        assert r.status_code==200 and r.json()['status']=='success'
        assert client.post('/api/v2/retrieval/tools/v2.get_chunk',json={'arguments':{}}).status_code==422
        assert client.get('/api/agents').status_code==200


def test_concurrent_version_switch_rejects_all_output(setup):
    repo,service=setup
    class Mutate:
        def rank(self,q,texts,timeout):
            repo.save_revision(repo.get('a'),'new authoritative version','local','race')
            return [(i,float(i)) for i in range(len(texts))]
    service.reranker=Mutate()
    result=service.search(request(mode='lexical'))
    assert not result.results and not result.documents
    assert result.diagnostics['rejected']['concurrent_scope_change']==1


def test_forged_locator_is_rejected(setup):
    repo,service=setup
    with repo.connect() as conn:
        rows=conn.execute('SELECT id,payload FROM retrieval_chunks').fetchall()
        for row in rows:
            payload=json.loads(row['payload']);payload['locator']['page_numbers']=[98765]
            conn.execute('UPDATE retrieval_chunks SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
    result=service.search(request(mode='lexical'))
    assert not result.results and result.diagnostics['rejected']['unavailable']


def test_cross_document_version_and_unknown_filter_rejected(setup):
    repo,service=setup
    with pytest.raises(ValueError):
        service.search(RetrievalRequest(query='fuel',scopes=[{'knowledge_base_id':'kb','versions':{'a':repo.documents.version('b').id}}]))
    with pytest.raises(ValueError):request(filters={'unsafe_filter':True})
    with pytest.raises(ValueError):request(regex_mode=True)
    with pytest.raises(ValueError):request(budget={'total_candidates':501})


def test_advanced_tools_require_separate_permission(setup,tmp_path):
    repo,service=setup
    def permissions(permission):
        if permission=='tool.advanced':raise PermissionError()
    tools=ToolService(service,ToolConfigStore(tmp_path/'config'),ToolCallStore(tmp_path/'audit'),permissions,'user')
    assert tools.invoke('v2.read_document',{'knowledge_base_id':'kb','document_id':'a'}).status=='success'
    assert tools.invoke('v2.search_bm25',request().model_dump()).error['code']=='forbidden'


def test_approval_switch_is_enforced(setup,tmp_path):
    repo,service=setup
    from app.tool_registry import ToolApprovalStore
    config=ToolConfigStore(tmp_path/'config');approvals=ToolApprovalStore(tmp_path/'approvals')
    tools=ToolService(service,config,ToolCallStore(tmp_path/'audit'),lambda p:None,'user',approvals=approvals)
    config.update('v2.read_document',True,True,'tool.invoke')
    values={'knowledge_base_id':'kb','document_id':'a'}
    assert tools.invoke('v2.read_document',values).error['code']=='approval_required'
    from app.retrieval_v2.tools import ReadInput
    approval=approvals.create('user','kb','v2.read_document',ReadInput(**values).model_dump())
    approvals.decide(approval['id'],'admin',True)
    assert tools.invoke('v2.read_document',values,approval['id']).status=='success'
    assert tools.invoke('v2.read_document',values,approval['id']).error['code']=='approval_required'


def test_table_cell_pagination_and_html_spans(setup,tmp_path):
    repo,service=setup
    repo.save_revision(repo.get('b'),'<table><tr><th colspan="2">kg</th></tr><tr><td>A</td><td>42</td></tr></table>', 'local','html')
    tools=ToolService(service,ToolConfigStore(tmp_path/'config'),ToolCallStore(tmp_path/'audit'),lambda p:None,'user')
    table=repo.documents.nodes('b','tables')[0]
    values={'knowledge_base_id':'kb','document_id':'b','node_id':table['id'],'limit':1}
    first=tools.invoke('v2.get_table_cells',values).data
    assert first['items'][0]['column_span']==2 and first['next_start']==1
    second=tools.invoke('v2.get_table_cells',{**values,'start':first['next_start']}).data
    assert second['items'][0]['content']=='A'


def test_rest_tenant_revocation(tmp_path):
    from app.main import create_app
    from tests.test_api import offline_settings
    app=create_app(offline_settings(tmp_path))
    with TestClient(app) as client:
        kb=client.get('/api/admin/knowledge-bases').json()[0]['knowledge_base_id']
        result=client.post('/api/v2/retrieval/search',json={'query':'q','scopes':[{'knowledge_base_id':'missing-tenant-kb'}]})
        assert result.status_code==404
        assert client.post('/api/v2/retrieval/search',json={'query':'q','scopes':[{'knowledge_base_id':kb}]}).status_code==200


def test_real_permission_membership_and_role_changes(tmp_path):
    from app.main import create_app
    from app.auth import SessionManager
    from tests.test_api import offline_settings
    settings=offline_settings(tmp_path);settings.auth_enabled=True
    app=create_app(settings);sessions=SessionManager(settings.admin_username,settings.admin_password_hash)
    with TestClient(app) as client:
        admin=sessions.create();alice=sessions.create('alice',False,'ldap')
        client.cookies.set(sessions.cookie_name,admin)
        assert client.post('/api/admin/organizations',json={'name':'Acme','tenant_key':'acme'}).status_code==201
        assert client.put('/api/admin/organizations/members',json={'tenant_key':'acme','user_id':'ldap:alice','role':'member'}).status_code==200
        assert client.post('/api/admin/knowledge-bases',json={'knowledge_base_id':'acme-kb','tenant_key':'acme','name':'Acme','dataset_id':'ds','api_key':'test'}).status_code==200
        repo=app.state.registry.get('@kb:acme-kb').repository
        source(repo,tmp_path/'files','tenant-private fuel');repo.documents.backfill_all()
        body={'query':'fuel','mode':'exact','scopes':[{'knowledge_base_id':'acme-kb'}]}
        client.cookies.set(sessions.cookie_name,alice)
        assert client.post('/api/v2/retrieval/search',json=body).json()['data']['results']
        assert client.post('/api/v2/retrieval/tools/v2.search_bm25',json={'arguments':body}).status_code==403
        assert client.post('/api/v2/retrieval/search',json={**body,'scopes':[{'knowledge_base_id':'default-kb'}]}).status_code==403
        client.cookies.set(sessions.cookie_name,admin)
        assert client.delete('/api/admin/organizations/acme/members/ldap%3Aalice').status_code==204
        client.cookies.set(sessions.cookie_name,alice)
        assert client.post('/api/v2/retrieval/search',json=body).status_code==403


def test_mapping_change_during_resolution_fails_closed(setup):
    repo,service=setup
    record=mapped_vector(repo)
    class Change:
        def search(self,*args):
            with repo.connect() as conn:
                row=conn.execute("SELECT payload FROM external_index_mappings WHERE id='map'").fetchone()
                payload=json.loads(row[0]);payload['index_version']='changed-ack'
                conn.execute("UPDATE external_index_mappings SET payload=? WHERE id='map'",(json.dumps(payload),))
            return {'candidates':[record]}
    service.vectors['kb']=Change()
    result=service.search(request(mode='vector'))
    assert not result.results and result.diagnostics['rejected']['concurrent_scope_change']


def test_all_backends_timeout_is_explicit(setup,monkeypatch):
    from app.retrieval_v2 import adapters
    repo,service=setup
    def slow(*args):time.sleep(.4);return {'candidates':[]}
    monkeypatch.setattr(adapters,'lexical',slow)
    class Slow:
        search=staticmethod(slow)
    service.vectors['kb']=Slow()
    start=time.monotonic()
    result=service.search(request(mode='hybrid',budget={'timeout_seconds':.15}))
    assert time.monotonic()-start<.3
    assert result.status=='insufficient_evidence'
    assert all(d['status']=='timeout' for d in result.diagnostics['backends'].values())
