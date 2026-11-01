"""Real Repository/ToolNode tests with deliberately unreliable model citations."""
import json
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import unquote
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.utils import convert_to_openai_messages
from app.agent_v2.state import ExecutionBudget
from app.agent_v2 import citations
from app.agent_v2.api import AgentAPI
from tests.test_react_v2 import runtime, setup, ScriptedModel, call


def state_of(store, runner, thread):
    with store.saver() as saver:
        return runner.build(saver).get_state({'configurable':{'thread_id':thread}}).values


@pytest.mark.parametrize('draft', ['Fuel fact', 'Fuel [1]', 'Fuel [S99]',
    '{"claims":[{"text":"Fuel fact","source_ids":[]}]}'])
def test_missing_wrong_and_numeric_citations_repaired_once(runtime,draft):
    make,service,store,repo=runtime
    def repair(messages):
        payload=json.loads(messages[-1].content)
        assert payload['question']=='fuel' and payload['draft']==draft
        assert payload['evidence'][0]['source_id']=='S1'
        return AIMessage(content=json.dumps({'claims':[{'text':'Fuel evidence','source_ids':['S1']}]}))
    model=ScriptedModel(call(),AIMessage(content=draft),repair)
    runner=make(model);result=runner.run('fuel')
    final=result['result']
    assert final['citation_validation']=='passed' and final['repair_success']
    assert final['answer']=='Fuel evidence [1]' and final['answer_correctness']=='not_assessed'
    assert model.calls==3 and result['tool_calls']==1
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1
    assert final['claims'][0]['source_ids']==['S1']
    assert state_of(store,runner,result['thread_id'])['repair_attempted']


@pytest.mark.parametrize('repair', ['Still no reference','Bad [S99]', 'Fact [1]',
    '{"claims":[{"text":"Bad","source_ids":["S99"]}]}'])
def test_repair_fails_closed_without_arbitrary_attachment(runtime,repair):
    make,*_=runtime
    model=ScriptedModel(call(),AIMessage(content='Unsupported statement'),AIMessage(content=repair))
    result=make(model).run('fuel')['result']
    assert result['repair_attempted'] and not result['repair_success']
    assert not result['citations'] and not result['answer_displayable']
    assert model.calls==3 and 'Unsupported statement' not in result['answer']


@pytest.mark.parametrize('text', ['Fact [S1]', '{"claims":[{"text":"Fact","source_ids":["S1"]}]}'])
def test_valid_short_and_structured(runtime,text):
    make,*_=runtime
    result=make(ScriptedModel(call(),AIMessage(content=text))).run('fuel')['result']
    assert result['citation_validation']=='passed' and not result['repair_attempted']
    assert 'ev_' not in result['answer'] and '[1]' in result['answer']


@pytest.mark.parametrize('text',['Fact [S99]','Fact [1]','Complete unsupported answer'])
def test_repair_switch(runtime,text):
    make,*_=runtime
    model=ScriptedModel(call(),AIMessage(content=text))
    result=make(model,budget=ExecutionBudget(citation_repair=False)).run('fuel')['result']
    assert result['citation_validation']=='rejected' and not result['repair_attempted']
    assert model.calls==2


def test_no_evidence_and_cross_run_source(runtime):
    make,*_=runtime
    make(ScriptedModel(call(),AIMessage(content='Fact [S1]'))).run('fuel')
    model=ScriptedModel(AIMessage(content='Previous fact [S1]'))
    result=make(model).run('another question')['result']
    assert result['trusted_evidence_count']==0 and not result['repair_attempted']
    assert not result['citations'] and model.calls==1


def test_find_document_is_not_body_evidence(runtime):
    make,*_=runtime
    model=ScriptedModel(call('find_document'),AIMessage(content='Full answer [S1]'))
    result=make(model).run('fuel')
    assert result['metrics']['registered_evidence_count']==0
    assert not result['result']['repair_attempted']


@pytest.mark.parametrize('mutation',['delete','version','acl'])
def test_final_revocation_does_not_request_repair(runtime,mutation):
    make,service,store,repo=runtime
    def final(messages):
        if mutation=='delete': repo.delete('a')
        if mutation=='version': repo.save_revision(repo.get('a'),'replacement','local','test')
        if mutation=='acl': service.retrieval.access.authorize_documents=lambda kb,docs:[]
        return AIMessage(content='Fact [S1]')
    model=ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),final)
    result=make(model).run('fuel')['result']
    assert result['citation_validation']=='rejected' and not result['repair_attempted']
    assert result['validation_errors']==['evidence_stale_or_revoked'] and model.calls==2


def test_source_numbers_parallel_dedupe_incremental(runtime):
    make,service,store,repo=runtime
    reads=[call('read_document',{'knowledge_base_id':'kb','document_id':d},d).tool_calls[0] for d in ['a','b']]
    def inspect(messages):
        outputs=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        labels=[e['source_id'] for out in outputs for e in out['evidence']]
        assert labels==['S1','S2','S1']
        wire=[json.loads(m['content']) for m in convert_to_openai_messages(messages) if m['role']=='tool']
        assert all('_evidence' not in m and '_scope_checks' not in m for m in wire)
        return AIMessage(content='B [S2]. A [S1]. B [S2].')
    runner=make(ScriptedModel(AIMessage(content='',tool_calls=reads),
        call('read_document',{'knowledge_base_id':'kb','document_id':'a'},'again'),inspect))
    result=runner.run('both')
    assert result['result']['answer']=='B [1]. A [2]. B [1].'
    state=state_of(store,runner,result['thread_id'])
    assert list(state['sources'])==['S1','S2'] and len(state['evidence'])==2
    assert {r['document_id'] for r in state['evidence'].values()}=={'a','b'}
    first=next(m for m in state['messages'] if isinstance(m,ToolMessage))
    assert first.artifact['_evidence'] and len(first.content)<len(json.dumps(first.artifact))


def test_visibility_loss_is_rejected(runtime,monkeypatch):
    make,*_=runtime
    original=citations.observation
    def hide(*args):
        out=original(*args);out['evidence']=[];return out
    monkeypatch.setattr(citations,'observation',hide)
    result=make(ScriptedModel(call(),AIMessage(content='Fact [S1]'))).run('fuel')
    assert result['metrics']['registered_evidence_count']>0
    assert result['metrics']['model_visible_evidence_count']==0
    assert 'source_not_model_visible' in result['result']['validation_errors']
    assert not result['result']['repair_attempted']


def test_small_output_budget_keeps_receipt_but_no_hidden_citation(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),
        AIMessage(content='Fact [S1]')),budget=ExecutionBudget(output_chars=500,citation_repair=False))
    result=runner.run('fuel')
    assert result['metrics']['output_truncated']
    assert result['result']['citation_validation']=='rejected'
    with store.connect() as db:
        assert json.loads(db.execute('SELECT output FROM calls').fetchone()[0])['_evidence']


def test_short_sources_survive_approval_and_process_restart(runtime,tmp_path):
    make,service,store,repo=runtime
    service.configurations.update('v2.read_document',True,True,'tool.invoke')
    runner=make(ScriptedModel(call(),call('read_document',{'knowledge_base_id':'kb','document_id':'a'},'read')))
    first=runner.run('fuel');before=state_of(store,runner,first['thread_id'])['sources']
    ident=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    service.approvals.decide(ident,'admin',True)
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
(root/'citation-child.json').write_text(json.dumps(result))
'''
    subprocess.run([sys.executable,'-c',program,str(tmp_path),first['thread_id']],check=True,timeout=30,capture_output=True)
    result=json.loads((tmp_path/'citation-child.json').read_text())
    assert result['result']['citation_validation']=='passed'
    after=state_of(store,runner,first['thread_id'])['sources']
    assert all(after[label]==ident for label,ident in before.items())
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1


def test_metadata_links_and_validation_separate(runtime):
    make,service,store,repo=runtime
    result=make(ScriptedModel(call(),AIMessage(content='Unsupported meaning [S1]'))).run('fuel')
    api=AgentAPI.__new__(AgentAPI)
    response=api.legacy_response(result,SimpleNamespace(agent_id='agent',name='Agent'))
    assert response.citation_validation=='passed' and response.answer_correctness=='not_assessed'
    c=response.citation_metadata[0];e=response.evidences[0]
    assert c['number']==1 and c['evidence_id']==e.evidence_id
    assert c['excerpt']==e.content and c['url']==e.pdf_url
    assert c['version_id'] in unquote(c['url']) and c['url'].startswith('/api/v2/knowledge-bases/kb/documents/')


@pytest.mark.parametrize('draft',['Fact [S1] 第999页','Fact [S1] https://evil.test',
    '{"claims":[{"text":"Fact [S1]","source_ids":["S1"]}]}'])
def test_invalid_locations_urls_or_nested_claims(runtime,draft):
    make,*_=runtime
    result=make(ScriptedModel(call(),AIMessage(content=draft)),budget=ExecutionBudget(citation_repair=False)).run('fuel')['result']
    assert result['citation_validation']=='rejected' and not result['citations']


def test_calculation_retains_internal_id(runtime):
    make,*_=runtime
    def calc(messages):
        e=json.loads(messages[-1].content)['evidence'][0]
        assert e['source_id']=='S1' and e['evidence_id'].startswith('ev_')
        return call('calculate',dict(knowledge_base_id='kb',operation='add',operands=[
            dict(value='42',source='evidence',evidence_id=e['evidence_id'])]),'calc')
    result=make(ScriptedModel(call('read_document_table',{'knowledge_base_id':'kb','document_id':'a'}),calc,
        AIMessage(content='Value is 42 [S1]'))).run('table')['result']
    assert result['calculations'][0]['result']=='42' and result['citation_validation']=='passed'


@pytest.mark.parametrize('budget',[{'total_tokens':1000},{'context_chars':1000}])
def test_model_budget_prevents_request(runtime,budget):
    make,*_=runtime
    model=ScriptedModel(call())
    result=make(model,budget=ExecutionBudget(**budget)).run('fuel'*300)
    assert model.calls==0 and not result['result']['citations']


def test_registration_diagnostics(runtime):
    make,*_=runtime
    registry=make(ScriptedModel()).registry;metrics={}
    refs,_=registry.collect({'status':'success','trace_id':'t','data':{'results':[
        {'verification_status':'unverified_external'},{'locator':{}}]}},12,metrics)
    assert not refs and metrics['tool_candidate_count']==2
    assert metrics['registration_rejections']=={'unverified_candidate':1,'missing_identity':1}


def test_old_checkpoint_validate_and_budget_defaults(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call(),AIMessage(content='Fact [S1]')))
    result=runner.run('fuel');state=state_of(store,runner,result['thread_id'])
    for field in ['sources','visible_evidence','repair_attempted']: state.pop(field,None)
    for message in state['messages']:
        if isinstance(message,ToolMessage):
            message.content=json.dumps(message.artifact);message.artifact=None
    ident=next(iter(state['evidence']))
    state['messages'][-1]=AIMessage(content='Legacy fact ['+ident+']')
    migrated=runner.validate(state)
    assert migrated['final_answer']['answer']=='Legacy fact [1]'
    assert migrated['sources']['S1']==ident
    old=runner.budget.model_dump()
    for field in ['citation_repair','total_tokens','completion_tokens']: old.pop(field)
    assert ExecutionBudget.model_validate(old)==runner.budget


def test_repair_context_budget_no_provider_or_tool_replay(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call(),AIMessage(content='Fact [S1]')))
    result=runner.run('fuel');state=state_of(store,runner,result['thread_id'])
    state.update(draft='x'*2000,repair_attempted=True)
    runner.budget=runner.budget.model_copy(update={'context_chars':1000})
    model=ScriptedModel();runner.model=model
    repaired=runner.repair(state)
    assert repaired['stop_reason']=='citation_repair_failed' and model.calls==0
    assert len(service.audit.recent('kb','v2.search_knowledge'))==1


def test_repair_receipt_reuse_and_uncertain_execution(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call(),AIMessage(content='Fact [S1]')))
    result=runner.run('fuel');state=state_of(store,runner,result['thread_id'])
    state.update(draft='No citation',repair_attempted=True)
    model=ScriptedModel(AIMessage(content='Fact [S1]'));runner.model=model
    first=runner.repair(state);second=runner.repair(state)
    assert first['messages'][0].content==second['messages'][0].content and model.calls==1
    with store.connect() as db:
        db.execute("UPDATE calls SET status='running',output=NULL WHERE call='__citation_repair__'")
    third=runner.repair(state)
    assert third['stop_reason']=='citation_repair_failed' and model.calls==1


def test_repair_token_budget_applies(runtime):
    make,*_=runtime
    def costly(messages):
        return AIMessage(content='No citation',response_metadata={'usage':{
            'total_tokens':119999,'prompt_tokens':119990,'completion_tokens':9}})
    model=ScriptedModel(call(),costly)
    result=make(model).run('fuel')['result']
    assert result['repair_attempted'] and not result['repair_success'] and model.calls==2


def test_failed_draft_is_not_conversation_history(runtime):
    make,*_=runtime
    make(ScriptedModel(call(),AIMessage(content='UNVERIFIED_DRAFT'),AIMessage(content='Fact [S1]'))).run('fuel')
    def inspect(messages):
        assert all('UNVERIFIED_DRAFT' not in str(m.content) for m in messages)
        return AIMessage(content='你好')
    assert make(ScriptedModel(inspect)).run('你好')['result']['citation_validation']=='passed'


def test_evidence_identity_does_not_merge_ranges_versions(runtime):
    make,service,store,repo=runtime
    registry=make(ScriptedModel()).registry
    def collect(start,end):
        version=repo.documents.version('a').id
        return registry.collect({'status':'success','trace_id':'t','data':{'results':[
            dict(knowledge_base_id='kb',document_id='a',version_id=version,locator=dict(start=start,end=end))]}},12)[0]
    a=collect(0,10);b=collect(0,20)
    assert a and b and set(a).isdisjoint(b)
    repo.save_revision(repo.get('a'),'New text starts here with different content','local','test')
    c=collect(0,10)
    assert c and set(a).isdisjoint(c)
    assert len(citations.assign_sources({**a,**b,**c},{}))==3


@pytest.mark.parametrize('style',['chat_completions','responses'])
def test_artifact_never_reaches_provider(runtime,monkeypatch,style):
    from app.clients.llm import LLMClient
    llm=LLMClient('https://unused','fake','test',style);seen=[]
    def post(path,body):
        seen.append(body)
        if style=='responses': return {'output':[{'content':[{'type':'output_text','text':'answer'}]}]}
        return {'choices':[{'message':{'content':'answer'}}]}
    monkeypatch.setattr(llm,'_post',post)
    llm.react([AIMessage(content='',tool_calls=[dict(id='c',name='t',args={})]),
        ToolMessage(content='{"status":"success"}',tool_call_id='c',artifact={'secret_internal':'NEVER_WIRE'})],[])
    assert 'NEVER_WIRE' not in json.dumps(seen) and 'artifact' not in json.dumps(seen)
