"""Deterministic model decisions, real LangGraph/SQLite/Tantivy/Repository tools."""
import json
import sqlite3
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from app.agent_v2.runtime import ReActRuntime
from app.agent_v2.checkpoint import ExecutionStore
from app.agent_v2.state import ExecutionBudget
from app.retrieval_v2.tools import ToolService, HIGH
from app.tool_registry import ToolConfigStore, ToolCallStore, ToolApprovalStore
from tests.test_retrieval_v2 import setup


class ScriptedModel:
    def __init__(self,*steps): self.steps=list(steps);self.calls=0
    def react(self,messages,tools,**kwargs):
        step=self.steps[self.calls];self.calls+=1
        return step(messages) if callable(step) else step


def call(name='search_knowledge',args=None,ident='call1'):
    return AIMessage(content='',tool_calls=[dict(id=ident,name='v2__'+name,args=args or
        {'query':'fuel','mode':'lexical','scopes':[{'knowledge_base_id':'kb'}]})])


def answer(messages):
    # Retain legacy ID-copying coverage against trusted artifacts, not wire payloads.
    outputs=[m.artifact or json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
    ids=[k for o in outputs for k in o.get('_evidence',{})]
    return AIMessage(content='Fuel evidence ['+ids[0]+']' if ids else '证据不足。')


@pytest.fixture
def runtime(setup,tmp_path):
    repo,retrieval=setup
    db=tmp_path/'control.db'
    service=ToolService(retrieval,ToolConfigStore(db),ToolCallStore(db),lambda p:None,'user',
                        approvals=ToolApprovalStore(db))
    store=ExecutionStore(tmp_path/'executions')
    def make(model,**kwargs):
        return ReActRuntime(store,service,model,principal=kwargs.pop('principal','user'),
            agent_id=kwargs.pop('agent_id','agent'),scopes=[{'knowledge_base_id':'kb'}],
            allowed_tools=['v2.'+n for n in HIGH],**kwargs)
    return make,service,store,repo


def test_search_answer(runtime):
    make,service,store,repo=runtime
    result=make(ScriptedModel(call(),answer)).run('fuel')
    assert result['status']=='completed'
    assert result['tool_calls']==1
    assert result['result']['citation_validation']=='passed',result


def test_restart_approval(runtime):
    make,service,store,repo=runtime
    service.configurations.update('v2.search_knowledge',True,True,'tool.invoke')
    first=make(ScriptedModel(call())).run('fuel')
    assert first['status']=='waiting_approval',first
    ident=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    service.approvals.decide(ident,'admin',True)
    model=ScriptedModel(answer)
    second=make(model).run(thread_id=first['thread_id'],resume=True)
    assert second['result']['citation_validation']=='passed',second
    assert model.calls==1
    assert service.approvals.get(ident)['status']=='consumed'
    with pytest.raises(ValueError): make(model).run(thread_id=first['thread_id'],resume=True)


@pytest.mark.parametrize('name',['read_document','read_document_section','read_document_table','expand_evidence','find_exact_text'])
def test_search_then_read(runtime,name):
    make,service,store,repo=runtime
    doc=repo.documents;args={'knowledge_base_id':'kb','document_id':'a'}
    if name=='read_document_section': args['section_id']=doc.nodes('a','sections')[0]['id']
    if name=='expand_evidence': args['node_id']=doc.nodes('a','chunks')[0]['id']
    if name=='find_exact_text': args={'query':'U-10Zr','scopes':[{'knowledge_base_id':'kb'}],'mode':'exact'}
    model=ScriptedModel(call(),call(name,args,'call2'),answer)
    result=make(model).run('fuel and data')
    assert result['tool_calls']==2 and result['result']['citation_validation']=='passed',result
    with store.connect() as db:
        outputs=[json.loads(r[0]) for r in db.execute('SELECT output FROM calls')]
    assert all(o['status']=='success' for o in outputs),outputs
    assert outputs[-1]['_evidence'],outputs


def test_parallel_read(runtime):
    make,service,store,repo=runtime
    msg=AIMessage(content='',tool_calls=[call('read_document',{'knowledge_base_id':'kb','document_id':d},d).tool_calls[0] for d in ['a','b']])
    result=make(ScriptedModel(msg,answer)).run('read')
    assert result['tool_calls']==2 and result['result']['citation_validation']=='passed'
    assert len({e['tool_call_id'] for e in result['events'] if e['type']=='tool_completed'})==2


@pytest.mark.parametrize('first',[
    call(args={'query':'不存在的zzzz','scopes':[{'knowledge_base_id':'kb'}],'mode':'lexical'}),
    call(args={'unknown':1}),
    call('unregistered',{'x':1}),
    call('search_vector',{'query':'fuel','scopes':[{'knowledge_base_id':'kb'}]})])
def test_empty_or_error_requery(runtime,first):
    make,*_=runtime
    result=make(ScriptedModel(first,call(ident='call2'),answer)).run('fuel')
    assert result['tool_calls']==2 and result['result']['citation_validation']=='passed'


@pytest.mark.parametrize('budget,expected',[
    ({'iterations':1},'iteration_budget'),({'tool_calls':1},'tool_budget'),
    ({'retrieval_rounds':1},'answer'),({'context_chars':1000},'context_budget')])
def test_budgets(runtime,budget,expected):
    make,*_=runtime
    result=make(ScriptedModel(call(),call(ident='call2'),answer),budget=ExecutionBudget(**budget)).run('fuel')
    assert result['result']['stop_reason']==expected
    assert result['tool_calls']<=2


def test_tool_timeout(runtime,monkeypatch):
    make,service,*_=runtime
    def slow(*args,**kwargs): time.sleep(.1);raise RuntimeError('private upstream detail')
    monkeypatch.setattr(service,'invoke',slow)
    result=make(ScriptedModel(call(),answer),budget=ExecutionBudget(tool_seconds=.02)).run('fuel')
    assert any(e['type']=='tool_failed' for e in result['events'])
    assert 'private upstream' not in str(result)


def test_cancel_during_model(runtime):
    make,service,store,repo=runtime
    runtime_obj=None
    def model(messages):
        store.cancel(runtime_obj.thread_id)
        return call()
    runtime_obj=make(ScriptedModel(model,answer))
    result=runtime_obj.run('fuel')
    assert result['status']=='cancelled'
    assert not service.audit.recent('kb','v2.search_knowledge')


def test_cancel_before_direct_answer(runtime):
    make,service,store,repo=runtime
    runner=None
    def model(messages):
        store.cancel(runner.thread_id)
        return AIMessage(content='must not display')
    runner=make(ScriptedModel(model))
    result=runner.run('hello')
    assert result['status']=='cancelled' and 'must not display' not in str(result)


@pytest.mark.parametrize('principal,agent',[('other','agent'),('user','other')])
def test_thread_ownership(runtime,principal,agent):
    make,*_=runtime
    first=make(ScriptedModel(AIMessage(content='你好'))).run('你好')
    with pytest.raises(PermissionError):
        make(ScriptedModel(),principal=principal,agent_id=agent).run(thread_id=first['thread_id'],resume=True)


def test_thread_lock_cross_instance(runtime):
    make,service,store,repo=runtime
    row=store.thread('user','agent',[],'lock-test')
    with store.lock(row['id']):
        with pytest.raises(RuntimeError,match='thread_busy'):
            with ExecutionStore(store.directory).lock(row['id']): pass


def test_multiturn_and_archive(runtime):
    make,service,store,repo=runtime
    first=make(ScriptedModel(call(),answer)).run('fuel')
    def second_model(messages):
        assert any(m.content=='fuel' for m in messages)
        assert not any(isinstance(m,ToolMessage) for m in messages)
        return AIMessage(content='你好')
    second=make(ScriptedModel(second_model)).run('你好')
    assert first['thread_id']==second['thread_id'] and first['run_id']!=second['run_id']
    assert not second['result']['citations']
    store.archive(first['thread_id'])
    with pytest.raises(ValueError): make(ScriptedModel()).run('hello')


@pytest.mark.parametrize('mutation',['reject','expire','disable','revoke','version','arguments'])
def test_approval_revalidation(runtime,mutation):
    make,service,store,repo=runtime
    service.configurations.update('v2.read_document',True,True,'tool.invoke')
    args={'knowledge_base_id':'kb','document_id':'a'}
    first=make(ScriptedModel(call('read_document',args))).run('fuel')
    ident=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    service.approvals.decide(ident,'admin',mutation!='reject')
    if mutation=='expire':
        with sqlite3.connect(service.approvals.path) as db: db.execute("UPDATE tool_approvals SET expires_at='2000-01-01'")
    if mutation=='disable': service.configurations.update('v2.read_document',False,True,'tool.invoke')
    if mutation=='revoke':
        service.retrieval.access.authorize_kb=lambda kb:(_ for _ in ()).throw(PermissionError('revoked'))
    if mutation=='version': repo.save_revision(repo.get('a'),'changed','local','reparse')
    if mutation=='arguments':
        with sqlite3.connect(service.approvals.path) as db: db.execute("UPDATE tool_approvals SET arguments='{}'")
    if mutation=='revoke':
        with pytest.raises(PermissionError): make(ScriptedModel()).run(thread_id=first['thread_id'],resume=True)
    else:
        result=make(ScriptedModel(answer)).run(thread_id=first['thread_id'],resume=True)
        assert not result['result']['citations']
        assert any(e['type']=='tool_failed' for e in result['events'])
    assert not service.audit.recent('kb','v2.read_document')


def test_prior_completed_step_not_replayed(runtime):
    make,service,store,repo=runtime
    service.configurations.update('v2.read_document',True,True,'tool.invoke')
    first=make(ScriptedModel(call(),call('read_document',{'knowledge_base_id':'kb','document_id':'a'},'call2'))).run('fuel')
    ident=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1
    service.approvals.decide(ident,'admin',True)
    result=make(ScriptedModel(answer)).run(thread_id=first['thread_id'],resume=True)
    assert result['tool_calls']==2 and result['result']['citation_validation']=='passed'
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1


@pytest.mark.parametrize('mutation',['delete','version','acl','page','fake_id','locator'])
def test_final_evidence_revalidation(runtime,mutation):
    make,service,store,repo=runtime
    def final(messages):
        out=answer(messages)
        if mutation=='delete': repo.delete('a')
        if mutation=='version': repo.save_revision(repo.get('a'),'replaced','local','reparse')
        if mutation=='acl': service.retrieval.access.authorize_documents=lambda kb,docs:[]
        if mutation=='page': out.content+=' 第 999 页'
        if mutation=='fake_id': out.content='Fact [ev_forged]'
        if mutation=='locator':
            with repo.connect() as db:
                db.execute("UPDATE retrieval_chunks SET payload=json_set(payload,'$.locator.page_numbers',json('[999]'))")
        return out
    result=make(ScriptedModel(call(),final)).run('fuel')
    assert result['result']['citation_validation']=='rejected'
    assert not result['result']['citations']


def test_explicit_historical_only(runtime):
    make,service,store,repo=runtime
    old=repo.documents.version('a').id
    repo.save_revision(repo.get('a'),'replacement','local','reparse')
    args={'knowledge_base_id':'kb','document_id':'a','version_id':old}
    result=make(ScriptedModel(call('read_document',args),answer)).run('fuel')
    assert not result['result']['citations']
    runtime_obj=make(ScriptedModel(call('read_document',args),answer))
    runtime_obj.scopes=[{'knowledge_base_id':'kb','document_ids':['a'],'versions':{'a':old}}]
    from app.agent_v2.evidence import EvidenceRegistry
    runtime_obj.registry=EvidenceRegistry(service,runtime_obj.scopes)
    result=runtime_obj.run('Read explicit historical version')
    assert all(e['verification_status']=='verified_historical' for e in result['result']['citations'].values())
    assert result['result']['citations']


def test_low_tool_denied_without_advanced(runtime):
    make,service,*_=runtime
    service.authorize_tool=lambda p:(_ for _ in ()).throw(PermissionError('advanced_denied')) if p=='tool.advanced' else None
    runner=make(ScriptedModel(call('search_bm25'),answer));runner.allowed.add('v2.search_bm25');runner.names['v2__search_bm25']='v2.search_bm25'
    result=runner.run('fuel')
    assert any(e['type']=='tool_failed' for e in result['events'])
    assert not result['result']['citations']


def test_duplicate_call_id_and_uncertain_receipt(runtime):
    make,service,store,repo=runtime
    result=make(ScriptedModel(call(),call(),answer)).run('fuel')
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1
    assert result['result']['citation_validation']=='passed'
    state={'thread_id':'t','run_id':'r'};c={'id':'c'}
    assert store.claim(state,c,'binding') is None
    with pytest.raises(RuntimeError,match='uncertain'): store.claim(state,c,'binding')
    with pytest.raises(ValueError,match='reused'): store.claim(state,c,'different')


def test_calculate_user_data(runtime):
    make,*_=runtime
    question='125.6 132.4'
    args=dict(knowledge_base_id='kb',operation='percentage_change',operands=[
        dict(value='125.6',source='user',start=0,end=5),dict(value='132.4',source='user',start=6,end=11)])
    result=make(ScriptedModel(call('calculate',args),AIMessage(content='anything'))).run(question)
    assert '5.414012738853503184713375796178344' in result['result']['answer']
    assert result['result']['calculations'][0]['operands'][0]['value']=='125.6'


def test_checkpoint_across_python_process(runtime,tmp_path):
    make,service,store,repo=runtime
    service.configurations.update('v2.read_document',True,True,'tool.invoke')
    first=make(ScriptedModel(call(),call('read_document',{'knowledge_base_id':'kb','document_id':'a'},'call2'))).run('fuel')
    ident=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    service.approvals.decide(ident,'admin',True)
    program='''
import json,sys
from pathlib import Path
from app.db import SourceRepository
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.tools import ToolService,HIGH
from app.tool_registry import ToolConfigStore,ToolCallStore,ToolApprovalStore
from app.agent_v2.checkpoint import ExecutionStore
from app.agent_v2.runtime import ReActRuntime
from tests.test_react_v2 import ScriptedModel,answer
root=Path(sys.argv[1]);repo=SourceRepository(root/'db','kb');db=root/'control.db'
svc=ToolService(UnifiedRetrievalService({'kb':repo.documents},AccessPolicy(lambda kb:None,'user')),
 ToolConfigStore(db),ToolCallStore(db),lambda p:None,'user',approvals=ToolApprovalStore(db))
runner=ReActRuntime(ExecutionStore(root/'executions'),svc,ScriptedModel(answer),principal='user',agent_id='agent',
 scopes=[{'knowledge_base_id':'kb'}],allowed_tools=['v2.'+n for n in HIGH])
result=runner.run(thread_id=sys.argv[2],resume=True)
(root/'child-result.json').write_text(json.dumps(result))
'''
    subprocess.run([sys.executable,'-c',program,str(tmp_path),first['thread_id']],check=True,timeout=30,capture_output=True)
    result=json.loads((tmp_path/'child-result.json').read_text())
    assert result['result']['citation_validation']=='passed'
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1


def test_corrupt_checkpoint_does_not_restart_question(runtime):
    make,service,store,repo=runtime
    make(ScriptedModel(AIMessage(content='hi'))).run('hi')
    (store.directory/'checkpoints.db').write_bytes(b'corrupt')
    model=ScriptedModel(AIMessage(content='must not run'))
    with pytest.raises(sqlite3.DatabaseError): make(model).run('fuel')
    assert model.calls==0


def test_parallel_approvals_resume_one_at_a_time(runtime):
    make,service,store,repo=runtime
    service.configurations.update('v2.read_document',True,True,'tool.invoke')
    calls=[call('read_document',{'knowledge_base_id':'kb','document_id':d},d).tool_calls[0] for d in ['a','b']]
    result=make(ScriptedModel(AIMessage(content='',tool_calls=calls))).run('read both')
    for count in range(2):
        assert result['status']=='waiting_approval'
        ident=next(e['approval_id'] for e in reversed(result['events']) if e['type']=='approval_required')
        service.approvals.decide(ident,'admin',True)
        result=make(ScriptedModel(answer)).run(thread_id=result['thread_id'],resume=True)
    assert result['status']=='completed' and result['tool_calls']==2
    assert len(service.audit.recent('kb','v2.read_document'))==2


def test_document_scope_and_cross_kb(runtime):
    make,service,store,repo=runtime
    for args in [{'knowledge_base_id':'other','document_id':'a'}, {'knowledge_base_id':'kb','document_id':'b'}]:
        runner=make(ScriptedModel(call('read_document',args),answer))
        runner.scopes=[{'knowledge_base_id':'kb','document_ids':['a'],'versions':{}}]
        result=runner.run('fuel',conversation=args['knowledge_base_id'])
        assert not result['result']['citations']
    assert not service.audit.recent('kb','v2.read_document')


def test_table_read_then_evidence_calculation(runtime):
    make,service,store,repo=runtime
    def calculate_call(messages):
        output=json.loads(messages[-1].content)
        evidence=output['evidence'][0];content=evidence['content'];start=content.index('42')
        args=dict(knowledge_base_id='kb',operation='add',operands=[dict(value='42',source='evidence',
            evidence_id=evidence['evidence_id'],start=start,end=start+2)])
        return call('calculate',args,'calc')
    result=make(ScriptedModel(call('read_document_table',{'knowledge_base_id':'kb','document_id':'a'}),calculate_call,answer)).run('Read the table value')
    assert result['result']['calculations'][0]['result']=='42'
    assert result['result']['citations']


def test_malformed_model_call(runtime):
    make,*_=runtime
    message=AIMessage(content='',invalid_tool_calls=[dict(name='v2__search_knowledge',args='{',id='bad',error='bad json')])
    result=make(ScriptedModel(message)).run('fuel')
    assert result['result']['stop_reason']=='malformed_model_call' and result['tool_calls']==0


def test_unknown_external_evidence_never_registered(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel())
    refs,_=runner.registry.collect({'status':'success','trace_id':'t','data':{'results':[{
        'knowledge_base_id':'kb','document_id':'a','version_id':repo.documents.version('a').id,
        'verification_status':'unverified_external','locator':{'start':0,'end':20}}]}},12)
    assert not refs


def test_explicit_current_version_is_not_history(runtime):
    make,service,store,repo=runtime
    result=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a',
        'version_id':repo.documents.version('a').id}),answer)).run('fuel')
    assert result['result']['citation_validation']=='passed'


def test_scope_rechecked_between_turns(runtime):
    make,service,store,repo=runtime
    make(ScriptedModel(call(),answer)).run('fuel')
    repo.delete('a')
    def model(messages):
        assert not any(isinstance(m,AIMessage) and 'Fuel evidence' in m.content for m in messages)
        return AIMessage(content='你好')
    assert make(ScriptedModel(model)).run('你好')['result']['citation_validation']=='passed'


def test_final_calculation_cannot_bypass_citations(runtime):
    make,service,store,repo=runtime
    def calc(messages):
        item=json.loads(messages[-1].content)['evidence'][0]
        return call('calculate',dict(knowledge_base_id='kb',operation='add',operands=[
            dict(value='42',source='evidence',evidence_id=item['evidence_id'])]),'calc')
    result=make(ScriptedModel(call('read_document_table',{'knowledge_base_id':'kb','document_id':'a'}),calc,
                             AIMessage(content='Unsupported answer without citation'))).run('table')
    assert result['result']['calculations']
    assert result['result']['citation_validation']=='rejected'


def test_structured_navigation(runtime):
    make,service,store,repo=runtime
    result=service.invoke('v2.read_document',{'knowledge_base_id':'kb','document_id':'a'})
    assert {'sections','tables'}=={n['kind'] for n in result.data['outline']}
    assert all(repo.documents.node('a',n['id']) for n in result.data['outline'])


def test_model_unavailable_and_output_budget(runtime):
    make,*_=runtime
    result=make(ScriptedModel(None)).run('fuel')
    assert result['status']=='failed' and result['result']['stop_reason']=='model_unavailable'
    result=make(ScriptedModel(AIMessage(content='x'*600)),budget=ExecutionBudget(output_chars=500)).run('fuel',conversation='large')
    assert result['result']['stop_reason']=='answer_output_budget'


def test_approval_required_without_decision_cannot_resume(runtime):
    make,service,*_=runtime
    service.configurations.update('v2.search_knowledge',True,True,'tool.invoke')
    result=make(ScriptedModel(call())).run('fuel')
    with pytest.raises(ValueError,match='not_decided'):
        make(ScriptedModel()).run(thread_id=result['thread_id'],resume=True)


def test_tool_permission_changes_during_execution(runtime,monkeypatch):
    make,service,*_=runtime
    original=service.invoke
    def invoke(*args,**kwargs):
        result=original(*args,**kwargs)
        service.configurations.update('v2.search_knowledge',False,False,'tool.invoke')
        return result
    monkeypatch.setattr(service,'invoke',invoke)
    result=make(ScriptedModel(call(),answer)).run('fuel')
    assert not result['result']['citations']
    assert any(e['type']=='tool_failed' for e in result['events'])


def test_read_receipt_revocation_blocks_replay(runtime):
    make,service,store,repo=runtime
    model=ScriptedModel(call(),answer)
    runner=make(model);result=runner.run('fuel')
    with store.connect() as db:
        output=json.loads(db.execute('SELECT output FROM calls WHERE run=?',(result['run_id'],)).fetchone()[0])
    repo.delete('a')
    with pytest.raises(PermissionError): runner.verify_scope_checks(output['_scope_checks'])


def test_large_table_returns_bounded_page(runtime):
    make,service,store,repo=runtime
    text='| Year | Value |\n|---|---|\n|2018|125.6|\n|2019|132.4|\n\n'
    repo.save_revision(repo.get('a'),text*10,'local','table pagination')
    result=service.invoke('v2.read_document_table',dict(knowledge_base_id='kb',document_id='a',max_chars=5000))
    assert result.status=='success' and result.data['items'] and result.data['partial']
    after=result.data['next_after']
    next_result=service.invoke('v2.read_document_table',dict(knowledge_base_id='kb',document_id='a',max_chars=5000,after=after))
    assert next_result.status=='success'
    assert result.data['items'][0]['id']!=next_result.data['items'][0]['id']


def test_resume_rejects_changed_execution_budget(runtime):
    make,service,*_=runtime
    service.configurations.update('v2.search_knowledge',True,True,'tool.invoke')
    result=make(ScriptedModel(call())).run('fuel')
    with pytest.raises(PermissionError,match='execution_budget_changed'):
        make(ScriptedModel(),budget=ExecutionBudget(tool_calls=1)).run(thread_id=result['thread_id'],resume=True)
