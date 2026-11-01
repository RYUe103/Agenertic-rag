"""Deterministic evaluator tests; tiny synthetic fixtures never count as public questions."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage
from scripts.build_chinese_eval import build,passages,FILES
from scripts.chinese_eval.common import sha,write,verify_pack,read_rows,lock
from scripts.chinese_eval.scoring import deterministic,answer_groups,validate_judge,diagnose
from scripts.chinese_eval.trace import rank_metrics,checkpoint_trace,retrieval_metrics
from scripts import evaluate_chinese_agent as ev
from tests.test_react_v2 import runtime,setup,ScriptedModel,call,answer


@pytest.fixture
def tiny_pack(tmp_path):
    raw=tmp_path/'raw';raw.mkdir()
    for name in FILES:
        row=dict(id=0,query='真实来源关系的单元测试问题',answer=[['甲','A'],'乙'],positive=[['甲证据'],['乙证据']] if name=='zh_int.json' else ['甲证据','乙证据'],negative=['干扰材料'])
        if name=='zh_fact.json':row.update(fakeanswer='丙',positive_wrong=['错误材料'])
        (raw/name).write_text(json.dumps(row,ensure_ascii=False)+'\n')
    (raw/'LICENSE.txt').write_text('fixture-license')
    pack=tmp_path/'pack';build(pack,raw,source_revision='fixture')
    return pack


def test_nested_passages_preserve_group_paths():
    assert list(passages([['a','b'],['c']]))==[((0,0),'a'),((0,1),'b'),((1,0),'c')]
    with pytest.raises(ValueError):list(passages({'bad':'shape'}))


def test_pack_independent_files_mapping_and_labels(tiny_pack):
    manifest=verify_pack(tiny_pack);questions=read_rows(tiny_pack/'questions.jsonl')
    assert manifest['document_count']==4
    integration=questions[1]
    assert {e['group'] for e in integration['gold_evidence']}=={0,1}
    assert len(integration['gold_document_ids'])==2
    assert questions[2]['document_roles']['positive_wrong']
    for item in manifest['corpus']:
        content=(tiny_pack/item['filename']).read_text()
        assert content in {'甲证据','乙证据','干扰材料','错误材料'}
        assert 'reference_answer' not in content and 'rgb_zh_' not in content
    for item in json.loads((tiny_pack/'passage_mapping.json').read_text()):
        doc=next(x for x in manifest['corpus'] if x['document_id']==item['document_id'])
        text=(tiny_pack/doc['filename']).read_text()[item['start']:item['end']]
        assert sha(text)==item['text_sha256']


def test_actual_frozen_fifty_pack_is_portable():
    pack=Path('benchmarks/chinese_rgb50');m=verify_pack(pack)
    assert m['question_count']==50 and m['document_count']>50
    assert len(read_rows(pack/'questions.jsonl'))==50
    assert (pack/'LICENSE.txt').exists()
    assert sum(m['task_counts'].values())==50


def test_pack_deterministic_and_immutable(tiny_pack,tmp_path):
    second=tmp_path/'second';build(second,tmp_path/'raw',source_revision='fixture')
    assert (tiny_pack/'manifest.json').read_bytes()==(second/'manifest.json').read_bytes()
    with pytest.raises(ValueError,match='immutable'):build(tiny_pack,tmp_path/'raw')
    doc=next((tiny_pack/'files').glob('*'));doc.write_text('changed')
    with pytest.raises(ValueError,match='identity'):verify_pack(tiny_pack)


@pytest.mark.parametrize('answer,expected',[
    ('70','correct'),('170','not_assessed'),('不是70，而是170','not_assessed'),
    ('70 [1]','correct'),('70，且发生在1999年','not_assessed'),('', 'unanswered')])
def test_no_substring_answer_accuracy(answer,expected):
    q=dict(reference_answer='70',answerable=True)
    assert deterministic(q,answer)['answer_grade']==expected


def test_alias_groups_and_refusal_uncertainty():
    q=dict(reference_answer=[['4次','四次']],answerable=True)
    assert deterministic(q,'四次')['answer_grade']=='correct'
    assert answer_groups([['4次','四次'],'中国'])==[['4次','四次'],['中国']]
    assert deterministic(dict(reference_answer=['中国','日本'],answerable=True),'中国和日本')['answer_grade']=='not_assessed'
    assert deterministic(dict(reference_answer='无法确定',answerable=False),'无法确定')['answer_grade']=='not_assessed'


@pytest.fixture
def judgment():
    payload=dict(agent_answer='答案甲',reference_answer=['甲'],gold_excerpts={'a':'甲的证据'},
                 claims=[{'text':'答案甲','source_ids':['S1']}],cited_excerpts={'S1':'甲的证据'})
    value=dict(answer_grade='correct',reason='比对答案与参考',answer_witnesses=[{'source':'agent_answer','quote':'答案甲'},{'source':'reference_answer','quote':'甲'}],
               claims=[{'index':0,'verdict':'supported','reason':'原文支持','quotes':[{'source_id':'S1','quote':'甲的证据'}]}])
    return payload,value


def test_judge_support_is_separate_from_answer(judgment):
    p,v=judgment
    assert validate_judge(v,p)['citation_supported'] is True
    v['claims'][0].update(verdict='unsupported',quotes=[])
    result=validate_judge(v,p)
    assert result['answer_grade']=='correct' and result['citation_supported'] is False


@pytest.mark.parametrize('change',['forged_quote','foreign_source','missing_claim','missing_witness','wrong_grade'])
def test_judge_untrusted_outputs_rejected(judgment,change):
    p,v=judgment
    if change=='forged_quote':v['claims'][0]['quotes'][0]['quote']='不在原文'
    if change=='foreign_source':v['claims'][0]['quotes'][0]['source_id']='S2'
    if change=='missing_claim':v['claims']=[]
    if change=='missing_witness':v['answer_witnesses']=[]
    if change=='wrong_grade':v['answer_grade']='great'
    with pytest.raises(ValueError):validate_judge(v,p)


def test_document_hit_is_not_passage_hit_or_answer_accuracy():
    q={'gold_document_ids':['a'],'gold_evidence':[{'document_id':'a','start':10,'end':20,'evidence_id':'p'}]}
    hit=dict(document_id='a',locator={'start':0,'end':5})
    m=rank_metrics([hit],q)
    assert m['document_hit_at_5'] and not m['passage_hit_at_5']
    assert 'answer_grade' not in m


def test_failure_classification_requires_trace_evidence():
    row=dict(answer_grade='incorrect',trace={},retrieval={},visible_gold_passage_count=0)
    assert diagnose(row)==['unknown']
    row.update(trace={'search_count':1},retrieval={'any_gold_passage_retrieved':False})
    assert 'retrieval_miss' in diagnose(row)
    row.update(retrieval={'any_gold_passage_retrieved':True})
    assert 'evidence_not_visible' in diagnose(row)


def test_actual_checkpoint_tool_trace(runtime):
    make,_,store,_=runtime
    result=make(ScriptedModel(call(),answer)).run('fuel')
    trace=checkpoint_trace(store.directory,result)
    assert trace['available'] and trace['visible_evidence']
    assert trace['tools'][0]['arguments']['query']=='fuel'
    assert trace['tools'][0]['results'] and trace['tools'][0]['registered_evidence']
    assert trace['search_count']==1


def test_run_receives_only_question_and_random_conversation(tmp_path,monkeypatch):
    observed=[]
    class Fake:
        def run(self,question,**kwargs):
            observed.append((question,kwargs));return {'thread_id':'unused','result':{},'events':[]}
    monkeypatch.setattr(ev,'make_runtime',lambda *args:Fake())
    q=dict(question_id='q',question='问题',reference_answer='秘密答案',gold_document_ids=['secret'])
    first=ev.run_one(tmp_path,None,None,q);second=ev.run_one(tmp_path,None,None,q)
    assert first==second and len(observed)==1
    assert observed[0][0]=='问题' and set(observed[0][1])=={'conversation'}
    assert observed[0][1]['conversation']!='q'


def test_interruption_never_replays_and_next_question_can_continue(tmp_path,monkeypatch):
    write(tmp_path/'attempts'/'q.json',{'status':'started'})
    monkeypatch.setattr(ev,'make_runtime',lambda *args:pytest.fail('must not run'))
    with pytest.raises(RuntimeError,match='no_automatic_replay'):
        ev.run_one(tmp_path,None,None,{'question_id':'q','question':'x'})


def test_selection_and_lock_protect_repeat_execution(tmp_path):
    ev.freeze_selection(tmp_path,{},[{'question_id':'a'}])
    with pytest.raises(ValueError,match='selection_changed'):ev.freeze_selection(tmp_path,{},[{'question_id':'b'}])
    with lock(tmp_path/'busy'):
        with pytest.raises(BlockingIOError):
            with lock(tmp_path/'busy'): pass


def test_prepare_hashes_versions_and_changed_configuration(tiny_pack,tmp_path):
    settings=SimpleNamespace(llm_model='fake',llm_api_style='chat_completions',llm_base_url='http://example.invalid')
    manifest,repo=ev.prepare(tmp_path/'run',tiny_pack,settings)
    ev.verify_environment(manifest,repo,settings,True)
    assert len(repo.list())==4 if hasattr(repo,'list') else len(manifest['corpus'])==4
    changed=SimpleNamespace(**{**settings.__dict__,'llm_model':'other'})
    with pytest.raises(ValueError,match='configuration_changed'):ev.verify_environment(manifest,repo,changed,True)
    source=repo.get(manifest['corpus'][0]['document_id']);repo.save_revision(source,'changed','test','test')
    with pytest.raises(ValueError,match='repository_changed'):ev.verify_environment(manifest,repo,settings,False)


def test_unassessed_and_missing_usage_not_counted_as_accuracy_or_zero_cost():
    r=dict(answer_grade='not_assessed',actual_total_tokens=None,elapsed_seconds=None,retrieval={})
    m=ev.aggregate([r])
    assert m['correct_rate_assessed'] is None and m['actual_usage_questions']==0
    assert m['avg_actual_tokens'] is None


def test_report_no_model_calls(tmp_path,monkeypatch):
    q=dict(question_id='q',question='问题',reference_answer='甲',answerable=True,task_type='fact',gold_document_ids=[],gold_evidence=[])
    artifact=dict(question_id='q',result=None,trace={'tools':[]},generation_status='failed')
    write(tmp_path/'cases'/'q.json',artifact)
    monkeypatch.setattr(ev,'LLMClient',lambda *args:pytest.fail('offline report invoked model'))
    monkeypatch.setattr(ev,'verified_excerpts',lambda *args:({},[]))
    monkeypatch.setattr(ev,'make_service',lambda *args:None)
    result=ev.report(tmp_path,None,{'corpus':[]},[q])
    assert result['summary']['completed_questions']==1
    assert (tmp_path/'report.md').exists()


def test_original_crlf_bytes_are_preserved(tmp_path):
    raw=tmp_path/'raw';raw.mkdir()
    text='首行\r\n次行\r结尾'
    for name in FILES:
        (raw/name).write_text(json.dumps(dict(id=1,query='问题',answer=['首行'],positive=[text],negative=[]),ensure_ascii=False)+'\n')
    (raw/'LICENSE.txt').write_text('test')
    pack=tmp_path/'pack';build(pack,raw)
    settings=SimpleNamespace(llm_model='fake',llm_api_style='chat_completions',llm_base_url='http://example.invalid')
    manifest,repo=ev.prepare(tmp_path/'run',pack,settings)
    assert repo.documents.version(manifest['corpus'][0]['document_id']).markdown==text
    ev.verify_environment(manifest,repo,settings,True)


def test_pilot_snapshot_origin_ranges():
    pack=Path('benchmarks/chinese_rgb_pilot');m=verify_pack(pack)
    corpus={c['document_id']:(pack/c['filename']).read_bytes().decode() for c in m['corpus']}
    mapping=json.loads((pack/'passage_mapping.json').read_text())
    assert len(mapping)>50
    for origin in mapping:
        assert sha(corpus[origin['document_id']][origin['start']:origin['end']])==origin['text_sha256']
    assert any(x['legacy_list_serialization'] for x in mapping)


def test_unsupported_witness_failure_does_not_become_agent_wrong_answer():
    r=dict(answer_grade='not_assessed',actual_total_tokens=10,elapsed_seconds=1,retrieval={},evaluation_failure='bad_quote')
    summary=ev.aggregate([r])
    assert summary['evaluation_failures']==1 and summary['assessed_questions']==0
    assert summary['failure_reasons']=={}
