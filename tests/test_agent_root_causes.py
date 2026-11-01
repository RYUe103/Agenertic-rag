"""Red tests recorded against 408dbf1 before implementation. Real Repository/ToolNode."""
import json
import pytest
from langchain_core.messages import AIMessage,ToolMessage
from tests.test_react_v2 import runtime,setup,ScriptedModel,call
from app.retrieval_v2.models import RetrievalRequest
from app.agent_v2 import citations


def candidate(repo,doc='a'):
    version=repo.documents.version(doc)
    return dict(knowledge_base_id='kb',document_id=doc,version_id=version.id,
                locator={'start':0,'end':20})


@pytest.mark.parametrize('prefix',['invalid','duplicate'])
def test_registration_fills_after_rejections_and_duplicates(runtime,prefix):
    make,service,store,repo=runtime
    a,b=candidate(repo),candidate(repo,'b')
    rows=[{**a,'version_id':'old'}]*4+[a,b] if prefix=='invalid' else [a]*4+[b]
    diag={};refs,snippets=make(ScriptedModel()).registry.collect(
        {'status':'success','trace_id':'test','data':{'results':rows}},2,diag)
    assert len(refs)==len(snippets)==2
    assert {r['document_id'] for r in refs.values()}=={'a','b'}
    assert diag['examined']>=5 and diag['registered']==2


def test_document_candidates_have_verified_names(runtime):
    make,service,store,repo=runtime
    result=service.retrieval.search(RetrievalRequest(query='fuel',mode='lexical',scopes=[{'knowledge_base_id':'kb'}]))
    assert result.documents
    for d in result.documents:
        assert d['document_name']==repo.get(d['document_id']).filename
        assert d['version_id'] and d['rank']>=1
    observed=citations.observation({'status':'success','data':result.model_dump()}, {},{},'v2.find_document')
    assert observed['data']['documents'][0]['document_name']


def test_oversize_metadata_does_not_evict_valid_evidence(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel())
    refs,snippets=runner.registry.collect({'status':'success','trace_id':'test','data':{'results':[candidate(repo)]}},2)
    output={'status':'success','evidence':snippets,'data':{'outline':[{'id':str(i),'title':'x'*3000} for i in range(12)]}}
    visible,partial=runner.compact_observation(output,{'S1':next(iter(refs))},refs,'v2.read_document')
    assert visible['status']=='success' and visible['evidence']
    assert partial and visible.get('navigation')


def test_oversized_first_evidence_does_not_starve_short_second(runtime):
    make,*_=runtime;runner=make(ScriptedModel())
    refs={'big':{},'small':{}}
    snippets=[{'evidence_id':'big','content':'x'*20000,'document_id':'a'},
              {'evidence_id':'small','content':'usable short evidence','document_id':'b'}]
    visible,partial=runner.compact_observation({'status':'success','evidence':snippets,'data':{}},
                                               {'S1':'big','S2':'small'},refs,'v2.search_knowledge')
    assert [e['source_id'] for e in visible['evidence']]==['S2']
    assert partial and visible.get('navigation')


def test_source_reread_keeps_identity_and_receipt(runtime):
    make,service,store,repo=runtime
    model=ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),
        call('read_document',{'knowledge_base_id':'kb','source_id':'S1'},'reread'),AIMessage(content='Fact [S1]'))
    runner=make(model);result=runner.run('fuel')
    assert result['result']['citation_validation']=='passed'
    with store.connect() as db:
        output=json.loads(db.execute("SELECT output FROM calls WHERE call='reread'").fetchone()[0])
    assert output['status']=='success' and output['_evidence']


def test_repeated_numbers_can_use_trusted_candidate_id(runtime):
    make,service,store,repo=runtime
    repo.save_revision(repo.get('a'),'Revenue 42 USD. Expense 42 USD.','local','numbers')
    def choose(messages):
        evidence=json.loads(messages[-1].content)['evidence'][0]
        choices=[n for n in evidence['numeric_candidates'] if n['raw_text']=='42']
        assert len(choices)==2 and choices[0]['candidate_id']!=choices[1]['candidate_id']
        return call('calculate',{'knowledge_base_id':'kb','operation':'subtract','operands':[
            {'source':'evidence','source_id':evidence['source_id'],'candidate_id':n['candidate_id']} for n in choices]},'calc')
    result=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),choose,AIMessage(content='Difference 0 [S1]'))).run('difference')
    assert result['result']['calculations'][0]['result']=='0'


@pytest.mark.parametrize('case',['count','timeout','all_invalid','acl','version'])
def test_registration_budget_and_fail_closed(runtime,case):
    make,service,store,repo=runtime;registry=make(ScriptedModel()).registry
    a,b=candidate(repo),candidate(repo,'b');kwargs={};rows=[{**a,'version_id':'stale'}]*5+[b];limit=2
    if case=='count':kwargs['max_candidates_examined']=3
    if case=='timeout':kwargs['seconds']=0
    if case=='all_invalid':rows=rows[:-1]
    if case=='acl':
        service.retrieval.access.authorize_documents=lambda kb,ids:['b'];rows=[a,b]
    if case=='version':
        repo.save_revision(repo.get('a'),'new revision','local','update');rows=[a,b]
    diag={};refs,_=registry.collect({'status':'success','trace_id':'t','data':{'results':rows}},limit,diag,**kwargs)
    if case in {'count','timeout'}:
        assert not refs and diag['budget_exhausted']
        assert diag['examined']==(3 if case=='count' else 0)
    elif case=='all_invalid':assert not refs and diag['rejected'] and not diag['budget_exhausted']
    else:assert {r['document_id'] for r in refs.values()}=={'b'} and diag['rejected']==1


def state_of(runner,result):
    with runner.store.saver() as saver:
        return runner.build(saver).get_state({'configurable':{'thread_id':result['thread_id']}}).values


@pytest.mark.parametrize('mutation',['delete','version','acl','hash','kb','unknown'])
def test_reread_revalidates_identity(runtime,mutation):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),AIMessage(content='fact [S1]')))
    result=runner.run('fuel');state=state_of(runner,result)
    args={'knowledge_base_id':'kb','source_id':'S1'}
    # Hidden in working context remains registered; loading must not authorize it yet.
    state['visible_evidence']=[]
    runner.scoped_arguments('v2.read_document',args,state)
    assert state['visible_evidence']==[]
    if mutation=='delete':repo.delete('a')
    if mutation=='version':repo.save_revision(repo.get('a'),'changed','local','update')
    if mutation=='acl':service.retrieval.access.authorize_documents=lambda kb,ids:[]
    if mutation=='hash':state['evidence'][state['sources']['S1']]['content_hash']='bad'
    if mutation=='kb':args['knowledge_base_id']='other'
    if mutation=='unknown':args['source_id']='S99'
    with pytest.raises((PermissionError,ValueError,KeyError)):
        runner.scoped_arguments('v2.read_document',args,state)


def test_reread_rest_and_cross_run_are_denied(runtime):
    make,service,store,repo=runtime
    assert service.invoke('v2.read_document',{'knowledge_base_id':'kb','source_id':'S1'}).error['code']=='forbidden'
    make(ScriptedModel(call(),AIMessage(content='fact [S1]'))).run('fuel')
    runner=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','source_id':'S1'}),AIMessage(content='bad [S1]')))
    result=runner.run('new question')
    assert result['result']['citation_validation']=='rejected'
    assert result['metrics']['registered_evidence_count']==0


def test_numeric_witnesses_skip_markup_and_preserve_coordinates():
    from app.agent_v2.numbers import extract
    raw='<table><tr><th>Label</th><th>2018 USD</th><th>2019 USD</th></tr><tr><td rowspan="2">Cost</td><td>42</td><td>42</td></tr><tr><td>5</td><td>8</td></tr></table>'
    result=extract({'content':raw,'evidence_id':'ev_x'},'run','S1')
    assert not any(n['raw_text']=='2' for n in result)  # rowspan is markup, not data.
    values=[n for n in result if n['raw_text']=='42']
    assert len(values)==2 and {n['column'] for n in values}=={1,2}
    assert values[0]['headers']==['2018 USD'] and values[1]['headers']==['2019 USD']
    for n in result:assert raw[n['start']:n['end']]==n['raw_text']


def test_numeric_visibility_foreign_id_and_overrides_rejected(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),AIMessage(content='fact [S1]')))
    result=runner.run('fuel');state=state_of(runner,result)
    witness=next(iter(state['numeric_candidates'].values()))
    args={'knowledge_base_id':'kb','operation':'add','operands':[{'source':'evidence','source_id':witness['source_id'],'candidate_id':witness['candidate_id']}]}
    canonical,*_=runner.scoped_arguments('v2.calculate',args,state)
    assert canonical['operands'][0]['value']==witness['raw_text']
    with pytest.raises(PermissionError):runner.scoped_arguments('v2.calculate',args,{**state,'visible_numeric_candidates':[]})
    bad=json.loads(json.dumps(args));bad['operands'][0]['value']='99999'
    with pytest.raises(PermissionError):runner.scoped_arguments('v2.calculate',bad,state)
    with pytest.raises(PermissionError):runner.scoped_arguments('v2.calculate',args,{**state,'run_id':'foreign_run'})


def test_numeric_candidate_paging_is_deterministic(runtime):
    make,service,store,repo=runtime
    repo.save_revision(repo.get('a'),' '.join(str(i) for i in range(40)),'local','numbers')
    recorded=[]
    def page(messages):
        out=json.loads(messages[-1].content)['evidence'][0]
        recorded.extend(n['raw_text'] for n in out['numeric_candidates'])
        return call('read_document',{'knowledge_base_id':'kb','source_id':out['source_id'],'number_start':out['next_number_start']},'page2')
    def finish(messages):
        out=json.loads(messages[-1].content)['evidence'][0]
        second=[n['raw_text'] for n in out['numeric_candidates']]
        assert not set(recorded)&set(second) and second[0]=='12'
        return AIMessage(content='numbers [S1]')
    assert make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),page,finish)).run('numbers')['result']['citation_validation']=='passed'


def test_candidate_labels_versions_and_revocation(runtime):
    make,service,store,repo=runtime
    from app.search.index_manager import IndexManager
    repo.update('a',filename='Specification-2019.pdf',standard_no='GB TEST',standard_year='2019')
    repo.update('b',filename='Specification-2020.pdf',standard_no='GB TEST',standard_year='2020')
    repo.save_revision(repo.get('b'),'Fuel specification version 2020.','local','version')
    IndexManager(repo.documents).sync()
    request=RetrievalRequest(query='Fuel',mode='lexical',scopes=[{'knowledge_base_id':'kb'}])
    output=service.retrieval.search(request)
    names={d['document_name'] for d in output.documents}
    assert names=={'Specification-2019.pdf','Specification-2020.pdf'}
    assert {d['standard_year'] for d in output.documents}=={repo.get('a').standard_year,repo.get('b').standard_year}
    seen=set();data={'status':'success','data':output.model_dump()}
    first=citations.observation(data,{}, {},'v2.find_document',seen)
    second=citations.observation(data,{}, {},'v2.find_document',seen)
    assert all(d['document_name'] and d['version_id'] for d in second['data']['documents'])
    for d in first['data']['documents']:
        assert service.invoke('v2.read_document',{'knowledge_base_id':'kb','document_id':d['document_id'],'version_id':d['version_id']}).status=='success'
    service.retrieval.access.authorize_documents=lambda kb,ids:['a']
    assert {d['document_id'] for d in service.retrieval.search(request).documents}=={'a'}


def test_packing_diversity_and_structure_atomicity(runtime):
    make,*_=runtime;runner=make(ScriptedModel())
    snippets=[{'evidence_id':str(i),'content':'x'*3000,'document_id':doc} for i,doc in enumerate(['a','a','b'])]
    refs={str(i):{} for i in range(3)};sources={f'S{i+1}':str(i) for i in range(3)}
    from app.agent_v2.state import ExecutionBudget
    runner.budget=ExecutionBudget(output_chars=8000)
    visible,_=runner.compact_observation({'status':'success','evidence':snippets,'data':{}},sources,refs,'v2.search_knowledge')
    assert [e['document_id'] for e in visible['evidence']]==['a','b']
    assert any(n.get('source_id')=='S2' for n in visible['navigation'])
    visible,_=runner.compact_observation({'status':'success','evidence':snippets[:1],
        'data':{'table_page':{'headers':['h'*20000]}}},sources,refs,'v2.read_document_table')
    assert not visible['evidence'] and visible['navigation']


def test_parallel_rereads_dedupe_and_approved_numeric_receipt(runtime):
    make,service,store,repo=runtime
    repo.save_revision(repo.get('a'),'Revenue 42 USD. Cost 42 USD.','local','numeric')
    service.configurations.update('v2.calculate',True,True,'tool.invoke')
    def rereads(messages):
        return AIMessage(content='',tool_calls=[call('read_document',{'knowledge_base_id':'kb','source_id':'S1'},name).tool_calls[0] for name in ['read1','read2']])
    def calculate(messages):
        evidence=json.loads(messages[-1].content)['evidence'][0]
        n=next(n for n in evidence['numeric_candidates'] if n['raw_text']=='42')
        return call('calculate',{'knowledge_base_id':'kb','operation':'add','operands':[{'source':'evidence','source_id':'S1','candidate_id':n['candidate_id']}]},'calc')
    runner=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),rereads,calculate))
    first=runner.run('revenue');assert first['status']=='waiting_approval'
    state=state_of(runner,first);assert len(state['sources'])==1
    approval=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    args=service.approvals.get(approval)['arguments']
    assert args['operands'][0]['start']==8 and args['operands'][0]['value']=='42'
    service.approvals.decide(approval,'admin',True)
    result=make(ScriptedModel(AIMessage(content='42 USD [S1]'))).run(thread_id=first['thread_id'],resume=True)
    assert result['result']['calculations'][0]['result']=='42' and result['result']['calculations'][0]['unit']=='USD'
    assert service.approvals.get(approval)['status']=='consumed'
    with pytest.raises(ValueError):make(ScriptedModel()).run(thread_id=first['thread_id'],resume=True)


def test_compacted_source_can_reread_before_calculate(runtime,monkeypatch):
    """9144 failure mechanism: hidden source must be re-read, never auto-authorized."""
    make,service,store,repo=runtime
    repo.save_revision(repo.get('a'),'Total revenue 100. Cost 20.','local','numeric')
    from app.agent_v2.context import TokenBudgetManager
    original=TokenBudgetManager.pack
    def pack(self,messages,tools,used):
        view,stats=original(self,messages,tools,used)
        # Deterministic capacity pressure on the original read only.
        if sum(isinstance(m,ToolMessage) for m in view)==1:
            view=[m.model_copy(update={'content':json.dumps({'status':'success','evidence':[],
                 'navigation':[{'source_id':'S1','knowledge_base_id':'kb','action':'read_document'}]})}) if isinstance(m,ToolMessage) else m for m in view]
        return view,stats
    monkeypatch.setattr(TokenBudgetManager,'pack',pack)
    def reread(messages):
        assert not json.loads(messages[-1].content)['evidence']
        return call('read_document',{'knowledge_base_id':'kb','source_id':'S1'},'reread')
    def finish(messages):
        assert json.loads(messages[-1].content)['evidence'][0]['source_id']=='S1'
        return AIMessage(content='Revenue 100 [S1]')
    result=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),reread,finish)).run('revenue')
    assert result['result']['citation_validation']=='passed'


def test_reread_approval_survives_process_restart(runtime,tmp_path):
    import subprocess,sys
    make,service,store,repo=runtime
    service.configurations.update('v2.read_document',True,True,'tool.invoke')
    runner=make(ScriptedModel(call(),call('read_document',{'knowledge_base_id':'kb','source_id':'S1'},'read')))
    first=runner.run('fuel');before=state_of(runner,first)['sources']
    approval=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    service.approvals.decide(approval,'admin',True)
    program='''
import json,sys
from pathlib import Path
from langchain_core.messages import AIMessage
from app.db import SourceRepository
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.tools import ToolService,HIGH
from app.tool_registry import ToolConfigStore,ToolCallStore,ToolApprovalStore
from app.agent_v2.checkpoint import ExecutionStore
from app.agent_v2.runtime import ReActRuntime
from tests.test_react_v2 import ScriptedModel
root=Path(sys.argv[1]);repo=SourceRepository(root/'db','kb');db=root/'control.db'
svc=ToolService(UnifiedRetrievalService({'kb':repo.documents},AccessPolicy(lambda kb:None,'user')),
 ToolConfigStore(db),ToolCallStore(db),lambda p:None,'user',approvals=ToolApprovalStore(db))
runner=ReActRuntime(ExecutionStore(root/'executions'),svc,ScriptedModel(AIMessage(content='Fact [S1]')),
 principal='user',agent_id='agent',scopes=[{'knowledge_base_id':'kb'}],allowed_tools=['v2.'+n for n in HIGH])
result=runner.run(thread_id=sys.argv[2],resume=True)
(root/'reread-child.json').write_text(json.dumps(result))
'''
    subprocess.run([sys.executable,'-c',program,str(tmp_path),first['thread_id']],check=True,timeout=30,capture_output=True)
    result=json.loads((tmp_path/'reread-child.json').read_text())
    assert result['result']['citation_validation']=='passed'
    assert state_of(runner,result)['sources']==before
    assert service.approvals.get(approval)['status']=='consumed'


def test_additive_fields_preserve_legacy_canonical_arguments():
    from app.retrieval_v2.tools import ReadInput,canonical_arguments
    from app.retrieval_v2.calculations import CalculationInput
    read=canonical_arguments(ReadInput(knowledge_base_id='kb',document_id='a'))
    assert 'source_id' not in read and 'number_start' not in read
    operand=canonical_arguments(CalculationInput(knowledge_base_id='kb',operation='add',operands=[{'value':'42','source':'user'}]))['operands'][0]
    assert 'candidate_id' not in operand and operand['value']=='42'


def test_observation_metadata_cursor_does_not_skip_candidates(runtime):
    make,*_=runtime;runner=make(ScriptedModel())
    documents=[dict(document_id=str(i),knowledge_base_id='kb',version_id='v',document_name='Title '+str(i)+'x'*250) for i in range(40)]
    start=0;seen=[]
    while start<len(documents):
        output={'status':'success','data':{'documents':documents,'document_start':start}}
        observed,_=runner.compact_observation(output,{}, {},'v2.find_document')
        admitted=observed['data']['documents'];assert admitted
        seen.extend(d['document_id'] for d in admitted)
        cursor=observed['data'].get('next_document_start')
        if cursor is None:break
        assert cursor>start;start=cursor
    assert seen==[str(i) for i in range(40)]


def test_registry_resolution_timeout_stops_scan(runtime,monkeypatch):
    make,service,store,repo=runtime;registry=make(ScriptedModel()).registry
    def timeout(*a,**kw):raise TimeoutError('resolution_deadline')
    monkeypatch.setattr(registry,'resolve',timeout)
    diag={};refs,_=registry.collect({'status':'success','trace_id':'t','data':{'results':[candidate(repo),candidate(repo,'b')]}},2,diag)
    assert not refs and diag['examined']==1 and diag['budget_exhausted']
    assert diag['registration_rejections']['resolution_timeout']==1


def test_numeric_entities_are_not_operands():
    from app.agent_v2.numbers import extract
    snippet={'evidence_id':'ev_x','content':'<table><tr><td>&#160; 42 USD</td></tr></table>'}
    values=extract(snippet,'run','S1')
    assert [n['raw_text'] for n in values]==['42']
