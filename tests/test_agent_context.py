import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
from app.agent_v2.context import TokenBudgetManager,profile
from app.agent_v2.state import ExecutionBudget
from app.agent_v2 import citations
from app.retrieval_v2.calculations import CalculationInput,calculate
from scripts.evaluate_agent_citations import select_questions
from tests.test_react_v2 import runtime,setup,ScriptedModel,call


def test_usage_actual_replaces_estimate_and_missing_usage_falls_back():
    manager=TokenBudgetManager(ExecutionBudget())
    msg=AIMessage(content='ok',response_metadata={'usage':{'prompt_tokens':123,'completion_tokens':7,'total_tokens':130}})
    assert manager.usage(msg,500)[0]['total_tokens']==130
    missing=manager.usage(AIMessage(content='ok'),500)
    assert missing[0]['prompt_tokens']==500 and missing[1] is None


def test_schema_reservation_and_boundary():
    manager=TokenBudgetManager(ExecutionBudget(total_tokens=10000))
    messages=[HumanMessage(content='query')];tools=[{'schema':'x'*3000}]
    measured=manager.measure(messages,tools,0)
    assert measured['tool_schema_tokens']>1000
    remaining=measured['estimated_input_tokens']+manager.budget.completion_tokens
    assert manager.fits(messages,tools,10000-remaining)
    assert not manager.fits(messages,tools,10001-remaining)
    assert manager.tokens('a'*1000)<1000


def test_compaction_pairs_checkpoint_unchanged_and_no_evidence_authority():
    messages=[HumanMessage(content='current question')]
    for i in range(4):
        messages.extend([call(ident=str(i)),ToolMessage(content=json.dumps({'status':'success','evidence':[
            {'source_id':f'S{i+1}','evidence_id':str(i),'content':'x'*6000,'document_id':'d'}],'data':{}}),tool_call_id=str(i),artifact={'original':'full'})])
    manager=TokenBudgetManager(ExecutionBudget(context_chars=14000))
    view,stats=manager.pack(messages,[],0)
    assert stats['context_compaction_count']>=2
    assert messages[2].content!=view[2].content and messages[2].artifact==view[2].artifact
    assert json.loads(view[2].content)['evidence']==[]
    assert json.loads(view[2].content)['requires_reread']
    assert [m.tool_call_id for m in view if isinstance(m,ToolMessage)]==['0','1','2','3']
    assert view[0].content=='current question'


def test_navigation_deduplicates_without_mutating_artifact():
    doc={'document_id':'d','knowledge_base_id':'kb','version_id':'v','title':'name','chunks':['secret']*1000}
    output={'status':'success','data':{'documents':[doc]},'evidence':[]}
    seen=set();a=citations.observation(output,{},[],'v2.find_document',seen)
    b=citations.observation(output,{},[],'v2.find_document',seen)
    assert a['data']['documents'][0]['title']=='name'
    assert 'title' not in b['data']['documents'][0]
    assert 'chunks' not in a['data']['documents'][0] and output['data']['documents'][0]['chunks']


def test_long_read_exact_locator_and_hash(runtime):
    make,service,store,repo=runtime
    text='Long original content. '*450
    repo.save_revision(repo.get('a'),text,'local','long test')
    def inspect(messages):
        out=json.loads(messages[-1].content)
        e=out['evidence'][0]
        assert len(e['content'])>2000
        assert e['locator']['end']-e['locator']['start']==len(e['content'])
        return AIMessage(content='Long content [S1]')
    result=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),inspect)).run('long')
    assert result['result']['citation_validation']=='passed'


def test_calculate_source_alias_approval_canonical_and_visibility(runtime):
    make,service,store,repo=runtime
    repo.save_revision(repo.get('a'),'Revenue 1,200; cost 200.','local','numbers')
    args={'knowledge_base_id':'kb','operation':'subtract','operands':[
        {'source':'evidence','source_id':'S1','value':'1,200'},
        {'source':'evidence','source_id':'S1','value':'200'}]}
    # The 200 inside 1,200 must not count as an occurrence.
    service.configurations.update('v2.calculate',True,True,'tool.invoke')
    first=make(ScriptedModel(call('read_document',{'knowledge_base_id':'kb','document_id':'a'}),call('calculate',args,'calc'))).run('compute')
    assert first['status']=='waiting_approval'
    ident=next(e['approval_id'] for e in first['events'] if e['type']=='approval_required')
    approved=service.approvals.get(ident)['arguments']
    assert approved['operands'][0]['evidence_id'].startswith('ev_') and approved['operands'][0]['source_id'] is None
    service.approvals.decide(ident,'admin',True)
    result=make(ScriptedModel(AIMessage(content='Difference 1000 [S1]'))).run(thread_id=first['thread_id'],resume=True)
    assert result['result']['calculations'][0]['result']=='1000'
    assert service.approvals.get(ident)['status']=='consumed'


@pytest.mark.parametrize('text,value,success', [('Amount −2.5','−2.5',True),('Amount 1,234','1,234',True),('Amount 12,34','12,34',False),('Amount 1,234','234',False)])
def test_exact_number_spelling(text,value,success):
    args=CalculationInput(knowledge_base_id='kb',operation='add',operands=[dict(source='user',value=value)])
    if success: assert calculate(args,lambda *_:text)['result']
    else:
        with pytest.raises(ValueError): calculate(args,lambda *_:text)


def test_strict_question_whitelist():
    rows=[{'id':'a'},{'id':'b'}]
    assert select_questions(rows,['b'])==[rows[1]]
    for ids in [[],['c'],['a','a']]:
        with pytest.raises(ValueError):select_questions(rows,ids)


def test_table_row_pages_preserve_headers_merged_cells_and_offsets():
    from app.retrieval_v2.table_page import table_page
    raw='<table><tr><th>Unit USD</th><th>Value</th></tr><tr><td rowspan="2">Group</td><td>12</td></tr><tr><td>34</td></tr><tr><td>X</td><td>56</td></tr></table>'
    from app.document_structure import table_cells
    cells=[c.model_dump() for c in table_cells(raw,'t',True)]
    table={'id':'t','raw':raw,'format':'table_html','cells':cells,'unit':'USD'}
    page=table_page(table,{'locator':{'start':10}},1,1,5000)
    assert page['next_start']==3 and page['table_page']['row_end']==3
    assert page['table_page']['headers'] and page['table_page']['unit']=='USD'
    for item in page['items']:
        loc=item['locator'];assert raw[loc['start']-10:loc['end']-10].startswith('<tr')
    with pytest.raises(ValueError):table_page(table,{'locator':{'start':10}},2,1,5000)


def test_profiles_are_distinct():
    assert profile('v2.search_knowledge')[1]['item_chars']==4000
    assert profile('v2.read_document')[1]['item_chars']==12000
    assert profile('v2.calculate')[1]['output_chars']<profile('v2.read_document')[1]['output_chars']


def test_source_alias_rejects_hidden_conflicting_and_revoked(runtime):
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call(),AIMessage(content='Fact [S1]')))
    result=runner.run('fuel')
    with store.saver() as saver:
        state=runner.build(saver).get_state({'configurable':{'thread_id':result['thread_id']}}).values
    args={'knowledge_base_id':'kb','operation':'add','operands':[{'source':'evidence','source_id':'S1','value':'42'}]}
    canonical,*_=runner.scoped_arguments('v2.calculate',args,state)
    assert canonical['operands'][0]['evidence_id']==state['sources']['S1']
    with pytest.raises(PermissionError):runner.scoped_arguments('v2.calculate',args,{**state,'visible_evidence':[]})
    bad=json.loads(json.dumps(args));bad['operands'][0]['evidence_id']='ev_other_run'
    with pytest.raises(PermissionError):runner.scoped_arguments('v2.calculate',bad,state)
    repo.delete(state['evidence'][state['sources']['S1']]['document_id'])
    with pytest.raises(PermissionError):runner.scoped_arguments('v2.calculate',args,state)


def test_table_read_exact_pages_and_no_raw_prefix(runtime):
    make,service,store,repo=runtime
    raw='| Item | USD |\n|---|---|\n'+''.join(f'| Company {i} | {i+100} |\n' for i in range(200))
    repo.save_revision(repo.get('a'),raw,'local','table')
    table=repo.documents.nodes('a','tables')[0]
    response=service.invoke('v2.read_document_table',dict(knowledge_base_id='kb',document_id='a',node_id=table['id'],max_chars=6000,limit=4))
    assert response.status=='success' and response.data['partial']
    assert response.data['table_page']['headers']
    runner=make(ScriptedModel())
    refs,snippets=runner.registry.collect(response.model_dump(),12,item_chars=12000)
    assert refs and snippets
    for e in snippets:
        assert e['content']==raw[e['locator']['start']:e['locator']['end']]
        assert not e['content_truncated']
        assert runner.registry.load(refs[e['evidence_id']]).content==e['content']


def test_usage_accumulation_across_model_calls(runtime):
    make,*_=runtime
    first=call();first.response_metadata={'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}
    last=AIMessage(content='Fact [S1]',response_metadata={'usage':{'input_tokens':200,'output_tokens':20,'total_tokens':220}})
    result=make(ScriptedModel(first,last)).run('fuel')
    assert result['usage']['total_tokens']==330
    assert result['metrics']['actual_prompt_tokens']==200
    assert result['metrics']['tool_schema_tokens']>0


def test_selected_evaluation_never_invokes_judge_by_default(runtime,tmp_path,monkeypatch):
    from scripts import evaluate_agent_citations as evaluation
    make,service,store,repo=runtime
    runner=make(ScriptedModel(call(),AIMessage(content='Fact [S1]')))
    monkeypatch.setattr(evaluation,'make_runtime',lambda *a,**kw:runner)
    monkeypatch.setattr(evaluation,'judge',lambda *a,**kw:pytest.fail('judge must not be called'))
    row={'id':'chosen','question':'fuel','target_document':'a','original_answer':'[]'}
    outcome=evaluation.evaluate(tmp_path,repo,None,row,'mineru',isolated=True)
    assert outcome['support_judgment']['status']=='disabled'
    assert evaluation.evaluate(tmp_path,repo,None,row,'mineru',isolated=True)==outcome


def test_outline_pagination_does_not_drop_equal_ordinals(runtime):
    make,service,store,repo=runtime
    text=''.join(f'# Section {i}\n\n| Name | Value |\n|---|---|\n| X | {i} |\n\n' for i in range(20))
    repo.save_revision(repo.get('a'),text,'local','outline')
    all_ids=[];after=-1
    while True:
        result=service.invoke('v2.read_document',dict(knowledge_base_id='kb',document_id='a',max_chars=20,limit=3,outline_after=after))
        assert result.status=='success'
        all_ids.extend(row['id'] for row in result.data['outline'])
        after=result.data['outline_next_after']
        if after is None:break
    assert len(all_ids)==len(set(all_ids))==40


def test_document_navigation_page_retains_candidates():
    documents=[{'document_id':str(i),'version_id':'v','knowledge_base_id':'kb'} for i in range(45)]
    output={'status':'success','data':{'documents':documents,'document_start':20}}
    observed=citations.observation(output,{},[],'v2.find_document')
    assert len(observed['data']['documents'])==20
    assert observed['data']['documents'][0]['document_id']=='20'
    assert observed['data']['next_document_start']==40
    assert len(output['data']['documents'])==45
