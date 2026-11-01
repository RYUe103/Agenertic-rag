"""Chinese evaluation: prepare/retrieval/agent/judge/report, one isolated shared corpus.

Agent receives only question, random conversation ID and KB scope. Offline analysis
and judge can inspect annotations afterwards. Started uncertain calls are never replayed.
"""
import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from app.agent_v2.checkpoint import ExecutionStore
from app.agent_v2.runtime import ReActRuntime
from app.agent_v2.state import ExecutionBudget
from app.clients.llm import LLMClient
from app.config import Settings
from app.db import SourceRepository
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.models import RetrievalRequest
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.tools import HIGH, ToolService
from app.search.index_manager import IndexManager
from app.tool_registry import ToolApprovalStore, ToolCallStore, ToolConfigStore
from scripts.chinese_eval.common import sha,write,read_rows,verify_pack,lock
from scripts.chinese_eval.scoring import VERSION,deterministic,diagnose,review
from scripts.chinese_eval.trace import checkpoint_trace,relevant,retrieval_metrics,rank_metrics

KB='chinese-rgb-evaluation'
BUDGET=ExecutionBudget(tool_calls=8,retrieval_rounds=4,iterations=12,tool_seconds=60,total_seconds=300,context_chars=80000,output_chars=24000)


def code_identity():
    files=[*Path('app').rglob('*.py'),*Path('scripts/chinese_eval').glob('*.py'),Path('scripts/evaluate_chinese_agent.py'),Path('scripts/build_chinese_eval.py'),Path('pyproject.toml')]
    return dict(git_sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        tree_sha256=sha(json.dumps({str(p):sha(p.read_bytes()) for p in sorted(files)},sort_keys=True)))


def configuration(settings):
    # Never persist the key or a possibly credential-bearing URL.
    return dict(model=settings.llm_model,api_style=settings.llm_api_style,
                endpoint_sha256=sha(settings.llm_base_url),budget=BUDGET.model_dump(),
                provider_defaults='LLMClient unchanged; temperature/top_p not overridden')


def prepare(directory,pack,settings):
    manifest=verify_pack(pack)
    directory=directory.resolve()
    if directory in {Path('data').resolve(), Path('.').resolve()}:
        raise ValueError('isolated_output_directory_required')
    if (directory/'manifest.json').exists():
        value=json.loads((directory/'manifest.json').read_text())
        if value.get('pack_sha256')!=sha((pack/'manifest.json').read_bytes()):
            raise ValueError('pack_changed')
        return value,SourceRepository(directory/'sources.db',KB)
    directory.mkdir(parents=True,exist_ok=True)
    if (directory/'sources.db').exists(): raise ValueError('partial_prepare_use_new_directory')
    repo=SourceRepository(directory/'sources.db',KB)
    corpus=[]
    for item in manifest['corpus']:
        text=(pack/item['filename']).read_bytes().decode('utf-8');local=directory/'corpus'/Path(item['filename']).name
        local.parent.mkdir(exist_ok=True);local.write_text(text)
        source=repo.create(dict(source_id=item['document_id'],filename=local.name,normalized_filename=local.name,
            file_hash=sha(text),local_pdf_path='',local_source_path=str(local),local_md_path=str(local),
            document_type='txt',mime_type='text/plain',parse_status='success'))
        repo.save_revision(source,text,'rgb_frozen_text','Isolated public evaluation corpus')
        corpus.append(dict(item,version_id=repo.documents.version(item['document_id']).id))
    manager=IndexManager(repo.documents);built=manager.sync();generation=manager.current()
    value=dict(format_version=2,execution_kind='frozen',code=code_identity(),configuration=configuration(settings),
        scorer_version=VERSION,pack_sha256=sha((pack/'manifest.json').read_bytes()),corpus=corpus,
        questions=read_rows(pack/'questions.jsonl'),chunks=built['chunks'],index_generation=generation['id'],
        index_manifest_sha256=sha(generation['manifest']),knowledge_base_id=KB,vector='unconfigured; no Dify or vector acceptance')
    write(directory/'manifest.json',value)
    return value,repo


def verify_environment(manifest,repo,settings,for_run=False):
    for item in manifest['corpus']:
        version=repo.documents.version(item['document_id'])
        if version.id!=item['version_id'] or sha(version.markdown)!=item.get('sha256',item.get('text_sha256')):
            raise ValueError('repository_changed')
    current=IndexManager(repo.documents).current()
    if manifest.get('index_generation') and current['id']!=manifest['index_generation']:
        raise ValueError('index_changed')
    if manifest.get('index_manifest_sha256') and sha(current['manifest'])!=manifest['index_manifest_sha256']:
        raise ValueError('index_manifest_changed')
    if for_run:
        if manifest.get('execution_kind')!='frozen': raise ValueError('pilot_is_offline_only')
        # Git documentation commits need not invalidate identical executable content.
        if manifest['code']['tree_sha256']!=code_identity()['tree_sha256']: raise ValueError('code_changed_new_run_required')
        if manifest['configuration']!=configuration(settings): raise ValueError('configuration_changed')


def make_service(repo):
    def access(kb):
        if kb!=KB: raise PermissionError('unknown_evaluation_kb')
    return UnifiedRetrievalService({KB:repo.documents},AccessPolicy(access,'evaluation-user'))


def make_runtime(directory,repo,settings,execution_dir):
    db=execution_dir/'control.db';execution_dir.mkdir(parents=True,exist_ok=True)
    service=ToolService(make_service(repo),ToolConfigStore(db),ToolCallStore(db),lambda p:None,
                        'evaluation-user','chinese-evaluation',ToolApprovalStore(db))
    model=LLMClient(settings.llm_base_url,settings.llm_api_key,settings.llm_model,settings.llm_api_style,45)
    return ReActRuntime(ExecutionStore(execution_dir),service,model,principal='evaluation-user',agent_id='evaluation',
        scopes=[{'knowledge_base_id':KB}],allowed_tools=['v2.'+n for n in HIGH],budget=BUDGET)


def selected_questions(manifest,limit,ids=None):
    rows=manifest['questions']
    if limit<0: raise ValueError('negative_limit')
    if ids:
        lookup={r['question_id']:r for r in rows}
        if len(ids)!=len(set(ids)) or set(ids)-lookup.keys(): raise ValueError('invalid_question_selection')
        return [lookup[i] for i in ids]
    return rows[:limit] if limit else rows


def freeze_selection(directory,manifest,questions):
    selection=[q['question_id'] for q in questions];path=directory/'selection.json'
    if path.exists() and json.loads(path.read_text())!=selection: raise ValueError('selection_changed_use_new_output')
    if not path.exists():write(path,selection)


def run_one(directory,repo,settings,question):
    qid=question['question_id'];saved=directory/'cases'/(qid+'.json');journal=directory/'attempts'/(qid+'.json')
    with lock(directory/'locks'/(qid+'.lock')):
        if saved.exists(): return json.loads(saved.read_text())
        if journal.exists(): raise RuntimeError('uncertain_started_execution_no_automatic_replay:'+qid)
        execution=directory/'executions'/qid
        if (execution/'executions.db').exists(): raise RuntimeError('orphan_execution_no_replay')
        conversation=uuid.uuid4().hex
        write(journal,dict(status='started',conversation=conversation,question_sha256=sha(question['question']),code=code_identity()))
        # The ONLY question-dependent data supplied to the runtime are these words.
        runner=make_runtime(directory,repo,settings,execution)
        try:
            result=runner.run(question['question'],conversation=conversation)
            artifact=dict(question_id=qid,result=result,trace=checkpoint_trace(execution,result),generation_status='completed')
        except Exception as exc:
            artifact=dict(question_id=qid,result=None,generation_status='failed',error_type=type(exc).__name__)
        write(saved,artifact);write(journal,dict(status='completed',artifact_sha256=sha(saved.read_bytes())))
        return artifact


def verified_excerpts(repo,artifact):
    """Read cited evidence again through the runtime registry, not judge-invented text."""
    from app.agent_v2.evidence import EvidenceRegistry
    registry=EvidenceRegistry(SimpleNamespace(retrieval=make_service(repo)),[{'knowledge_base_id':KB}])
    final=(artifact.get('result') or {}).get('result',{});trace=artifact.get('trace',{})
    refs=trace.get('evidence',{});visible=set(trace.get('visible_evidence',[]));cited={};errors=[]
    for metadata in final.get('citation_metadata',[]):
        ident=metadata['evidence_id'];label=metadata['source_id']
        if ident not in refs or ident not in visible:
            errors.append('not_registered_or_visible:'+label);continue
        try:
            hit=registry.load(refs[ident])
            if hit.content!=final['citations'][ident]['content']: raise ValueError('cited_content_changed')
            cited[label]=hit.content
        except (KeyError,ValueError,PermissionError,TimeoutError):errors.append('repository_recheck_failed:'+label)
    return cited,errors


def judge_payload(repo,question,artifact):
    final=artifact['result']['result'];cited,errors=verified_excerpts(repo,artifact)
    gold={}
    # Bounded group-fair evidence, with explicit truncation. Answers remain complete.
    grouped={}
    for item in question['gold_evidence']:grouped.setdefault(item.get('group',0),[]).append(item)
    count=0
    for group,items in sorted(grouped.items()):
        for item in items[:2]:
            text=repo.documents.version(item['document_id']).markdown[item['start']:item['end']]
            if sha(text)!=item['text_sha256']:raise ValueError('gold_range_mismatch')
            gold[str(group)+':'+str(count)]=text[:6000];count+=1
    payload=dict(question=question['question'],reference_answer=question['reference_answer'],answerable=question['answerable'],
        fake_answer=question.get('fake_answer'),agent_answer=final.get('answer',''),claims=final.get('claims',[]),
        cited_excerpts=cited,gold_excerpts=gold,source_recheck_errors=errors,
        gold_selection='first two passages per original group, each at most 6000 chars; incomplete relevance annotation',
        task_type=question['task_type'])
    payload['allowed_answer_quote_sources']=['agent_answer','reference_answer',*['gold:'+key for key in gold]]
    return payload


def evaluate_judge(directory,repo,question,artifact,settings,enable=False):
    answer=((artifact.get('result') or {}).get('result') or {}).get('answer','')
    baseline=deterministic(question,answer)
    qid=question['question_id'];path=directory/'judgments'/(qid+'.json')
    if path.exists():
        record=json.loads(path.read_text())
        if record.get('artifact_sha256')!=sha(json.dumps(artifact,sort_keys=True,ensure_ascii=False)):
            raise ValueError('judged_artifact_changed')
        return record['assessment']
    if not enable or not artifact.get('result'):return baseline
    with lock(directory/'locks'/('judge-'+qid+'.lock')):
        journal=directory/'attempts'/('judge-'+qid+'.json')
        if journal.exists(): raise RuntimeError('uncertain_judge_no_replay')
        payload=judge_payload(repo,question,artifact)
        write(journal,dict(status='started',payload_sha256=sha(json.dumps(payload,sort_keys=True,ensure_ascii=False))))
        model=LLMClient(settings.llm_base_url,settings.llm_api_key,settings.llm_model,settings.llm_api_style,60)
        record=review(payload,model)
        record.update(artifact_sha256=sha(json.dumps(artifact,sort_keys=True,ensure_ascii=False)),model=settings.llm_model,
                      bias='Post-hoc independent call, same model as Agent by default; correlated errors possible',
                      scorer_sha256=sha(Path('scripts/chinese_eval/scoring.py').read_bytes()),payload=payload)
        write(path,record);write(journal,dict(status='completed',result_sha256=sha(path.read_bytes())))
        return record['assessment']


def case_report(repo,question,artifact,assessment):
    result=artifact.get('result') or {};final=result.get('result',{});trace=artifact.get('trace',{})
    hits=list(final.get('citations',{}).values());visible=[]
    from app.agent_v2.evidence import EvidenceRegistry
    registry=EvidenceRegistry(SimpleNamespace(retrieval=make_service(repo)),[{'knowledge_base_id':KB}])
    for ident in trace.get('visible_evidence',[]):
        try: visible.append(registry.load(trace['evidence'][ident]).model_dump())
        except (KeyError,ValueError,PermissionError,TimeoutError):continue
    cited,errors=verified_excerpts(repo,artifact)
    met=result.get('metrics',{});usage=result.get('usage',{})
    providers=[e for e in result.get('events',[]) if e['type']=='model_metrics']
    actual=all(e.get('usage_source')=='provider' for e in providers) if providers else False
    row=dict(question_id=question['question_id'],question=question['question'],reference_answer=question['reference_answer'],
        task_type=question['task_type'],answerable=question['answerable'],agent_answer=final.get('answer',''),
        answer_grade=assessment['answer_grade'],assessment=assessment,citation_supported=assessment.get('citation_supported'),
        citation_validation=final.get('citation_validation'),citation_recheck_errors=errors,
        evaluation_failure=assessment.get('reason') if assessment.get('method')=='judge_failed' else None,
        citation_layers=dict(id_valid=not final.get('validation_errors') if final else None,
            authorized_repository_rechecked=not errors if final.get('citations') else None,
            actually_visible=not errors if final.get('citations') else None,
            semantic_support=assessment.get('citation_supported')),
        citation_excerpts=cited,claims=final.get('claims',[]),
        gold_document_hit=any(h['document_id'] in question['gold_document_ids'] for h in hits),
        cited_documents=sorted({h['document_id'] for h in hits}),
        visible_gold_passage_count=len(set().union(*(relevant(h,question) for h in visible))) if visible else 0,
        final_visible_evidence_count=len(visible),retrieval=retrieval_metrics(trace,question),trace=trace,
        tool_sequence=[t['name'] for t in trace.get('tools',[])],tool_calls=result.get('tool_calls',0),
        model_calls=len(providers),stop_reason=final.get('stop_reason') or trace.get('stop_reason') or artifact.get('generation_status'),
        usage=usage,usage_source='provider' if actual else 'estimated_or_unavailable',
        actual_total_tokens=usage.get('total_tokens') if actual else None,
        actual_prompt_tokens=usage.get('prompt_tokens') if actual else None,
        actual_completion_tokens=usage.get('completion_tokens') if actual else None,
        elapsed_seconds=result.get('elapsed_seconds'),context_compaction_count=met.get('context_compaction_count',0),
        tool_output_metrics=met.get('tool_outputs',[]),unnecessary_calls='Only exact request repetition measured; necessity otherwise not_assessed')
    row['failure_reasons']=diagnose(row)
    return row


def aggregate(rows):
    scored=[r for r in rows if r['answer_grade']!='not_assessed'];cost=[r['actual_total_tokens'] for r in rows if r.get('actual_total_tokens') is not None]
    seconds=[r['elapsed_seconds'] for r in rows if r.get('elapsed_seconds') is not None]
    first=[r['retrieval']['first_search'] for r in rows if r.get('retrieval',{}).get('first_search')]
    return dict(completed_questions=len(rows),answer_grades=dict(Counter(r['answer_grade'] for r in rows)),
        assessed_questions=len(scored),correct_rate_assessed=sum(r['answer_grade']=='correct' for r in scored)/len(scored) if scored else None,
        correct_fraction_all=sum(r['answer_grade']=='correct' for r in rows)/len(rows) if rows else None,
        citation_support=dict(Counter(str(r.get('citation_supported')) for r in rows)),
        citation_validation=dict(Counter(r.get('citation_validation') for r in rows)),
        final_gold_document_hits=sum(r.get('gold_document_hit',False) for r in rows),
        first_search_metrics={k:statistics.mean([r[k] for r in first if r.get(k) is not None]) for k in ['mrr','document_hit_at_5','document_hit_at_10','document_recall_at_5','document_recall_at_10','passage_hit_at_5','passage_hit_at_10','passage_recall_at_5','passage_recall_at_10'] if any(r.get(k) is not None for r in first)},
        total_actual_tokens=sum(cost),avg_actual_tokens=statistics.mean(cost) if cost else None,
        actual_usage_questions=len(cost),avg_seconds=statistics.mean(seconds) if seconds else None,
        tool_calls=sum(r.get('tool_calls',0) for r in rows),model_calls=sum(r.get('model_calls',0) for r in rows),
        context_compactions=sum(r.get('context_compaction_count',0) for r in rows),
        failure_reasons=dict(Counter(x for r in rows for x in r.get('failure_reasons',[]))),
        evaluation_failures=sum(bool(r.get('evaluation_failure')) for r in rows))


def report(directory,repo,manifest,questions):
    rows=[]
    for q in questions:
        path=directory/'cases'/(q['question_id']+'.json')
        if not path.exists():continue
        artifact=json.loads(path.read_text())
        # No Settings/model construction/network in report mode.
        assessment=evaluate_judge(directory,repo,q,artifact,None,False)
        rows.append(case_report(repo,q,artifact,assessment))
    value=dict(manifest={k:v for k,v in manifest.items() if k not in {'questions','corpus'}},corpus_documents=len(manifest['corpus']),
               scorer_version=VERSION,summary=aggregate(rows),cases=rows,
               limits=['Exact positive-passage labels, not exact answer span recall.',
                       'Pooled corpus protocol differs from RGB context-only metrics.',
                       'Technical citations and lexical overlap never imply semantic correctness.',
                       'No vector backend, no production data mutation.',
                       'wrong_document/distractor_misled/hallucination require attributable semantic review; otherwise unknown.'])
    judges=[json.loads(p.read_text()) for p in (directory/'judgments').glob('*.json')] if (directory/'judgments').exists() else []
    value['judge_usage']={k:sum(j['assessment'].get('usage',{}).get(k,0) for j in judges) for k in ('prompt_tokens','completion_tokens','total_tokens')}
    value['judge_calls']=len(judges)
    write(directory/'report.json',value)
    lines=['# 中文 Agent 逐题报告','',json.dumps(value['summary'],ensure_ascii=False,indent=2),'']
    for r in rows:
        lines += [f"## {r['question_id']}",'',r['question'],'',f"参考：{json.dumps(r['reference_answer'],ensure_ascii=False)}",'',r['agent_answer'],'',
                  f"评分：{r['answer_grade']}；原因：{r['assessment']['reason']}；引用支持：{r['citation_supported']}",'',
                  f"工具：{' → '.join(r['tool_sequence'])}；停止：{r['stop_reason']}；失败：{r['failure_reasons']}",'',
                  f"检索命中：{r['retrieval'].get('any_gold_passage_retrieved')}；最终可见正确段落：{r['visible_gold_passage_count']}",'']
    (directory/'report.md').write_text('\n'.join(lines))
    return value


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',choices=['rgb'],default='rgb')
    p.add_argument('--mode',choices=['prepare','retrieval','agent','judge','report'],default='agent')
    p.add_argument('--pack',type=Path,default=Path('benchmarks/chinese_rgb50'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--limit',type=int,default=5)
    p.add_argument('--question-ids',nargs='+')
    p.add_argument('--model-judge',action='store_true',help='Explicit extra post-hoc semantic judge calls')
    a=p.parse_args();directory=a.output.resolve()
    with lock(directory/'.evaluation.lock'):
        if a.mode in {'judge','report'}:
            manifest=json.loads((directory/'manifest.json').read_text());repo=SourceRepository(directory/'sources.db',KB)
        else:
            settings=Settings();manifest,repo=prepare(directory,a.pack,settings)
        questions=selected_questions(manifest,a.limit,a.question_ids)
        if a.mode=='prepare':print(json.dumps(dict(questions=len(manifest['questions']),files=len(manifest['corpus']))));return
        verify_environment(manifest,repo,None if a.mode=='report' else Settings(),for_run=a.mode in {'agent','retrieval'})
        if a.mode=='agent':
            settings=Settings()
            if not settings.llm_api_key:raise ValueError('real_llm_key_required')
            freeze_selection(directory,manifest,questions)
            for q in questions:
                try:
                    run_one(directory,repo,settings,q)
                except RuntimeError as exc:
                    if not str(exc).startswith(('uncertain_started_execution','orphan_execution')): raise
                    print(json.dumps(dict(question_id=q['question_id'],status='blocked_uncertain_no_replay')),flush=True)
                    continue
                print(json.dumps(dict(completed=q['question_id'])),flush=True)
        elif a.mode=='retrieval':
            rows=[]
            for q in questions:
                request=RetrievalRequest(query=q['question'],scopes=[{'knowledge_base_id':KB}],mode='lexical',top_k=10)
                output=make_service(repo).search(request).model_dump()
                rows.append(dict(question_id=q['question_id'],query=q['question'],metrics=rank_metrics(output['results'],q),response=output))
            write(directory/'retrieval.json',dict(mode='retrieval_only_not_answer_accuracy',cases=rows));return
        elif a.mode=='judge':
            settings=Settings()
            for q in questions:
                path=directory/'cases'/(q['question_id']+'.json')
                if path.exists():
                    evaluate_judge(directory,repo,q,json.loads(path.read_text()),settings,a.model_judge)
                    print(json.dumps(dict(judged=q['question_id'],model_enabled=a.model_judge)),flush=True)
        value=report(directory,repo,manifest,questions)
        print(json.dumps(value['summary'],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
