"""Small REAL model acceptance on an isolated copy of existing source Markdown.

No corpus upload, production migration or remote mutation. Report excludes content.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from app.config import Settings
from app.clients.llm import LLMClient
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.tools import ToolService,HIGH
from app.tool_registry import ToolConfigStore,ToolCallStore,ToolApprovalStore
from app.agent_v2.runtime import ReActRuntime
from app.agent_v2.checkpoint import ExecutionStore
from app.agent_v2.state import ExecutionBudget


def sha(value): return hashlib.sha256(value.encode() if isinstance(value,str) else value).hexdigest()


def runtime(directory,approval=False):
    settings=Settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        raise ValueError('Real LLM is not configured')
    repo=SourceRepository(directory/'sources.db','smoke')
    service=UnifiedRetrievalService({'smoke':repo.documents},AccessPolicy(lambda kb:None,'smoke-user'))
    db=directory/'control.db';configs=ToolConfigStore(db);approvals=ToolApprovalStore(db)
    if approval:
        for name in HIGH: configs.update('v2.'+name,True,True,'tool.invoke')
    tool_service=ToolService(service,configs,ToolCallStore(db),lambda p:None,'smoke-user','real-llm-smoke',approvals)
    model=LLMClient(settings.llm_base_url,settings.llm_api_key,settings.llm_model,settings.llm_api_style,45)
    return ReActRuntime(ExecutionStore(directory/'execution'),tool_service,model,principal='smoke-user',
        agent_id='smoke',scopes=[{'knowledge_base_id':'smoke','document_ids':['existing-source']}],
        allowed_tools=['v2.'+n for n in HIGH],budget=ExecutionBudget(tool_calls=8,retrieval_rounds=4,
            iterations=12,tool_seconds=60,total_seconds=300,context_chars=80000,output_chars=24000))


def redacted(result):
    final=result.get('result') or {}
    return {k:result[k] for k in ('status','tool_calls','retrieval_rounds','iterations','elapsed_seconds','usage')} | {
        'sequence':[{k:e[k] for k in ('type','tool_call_id','tool_name','status') if k in e}
                    for e in result['events'] if e['type'] in {'tool_started','tool_completed','tool_failed','resumed','approval_required'}],
        'citation_validation':final.get('citation_validation'),'validation_errors':final.get('validation_errors'),
        'answer_sha256':sha(final.get('answer','')),'answer_correctness':'not_assessed',
        'stop_reason':final.get('stop_reason'),
        'evidence':[{'document_sha256':sha(e['document_id']),'version_sha256':sha(e['version_id']),
                    'source_adapter':e['source_adapter'],'locator':e['locator'],'verification_status':e['verification_status']}
                    for e in final.get('citations',{}).values()],
        'calculation_count':len(final.get('calculations',[]))}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--resume-thread');p.add_argument('--targeted',action='store_true');args=p.parse_args();directory=args.output
    if args.resume_thread:
        result=runtime(directory).run(thread_id=args.resume_thread,resume=True)
        (directory/'resume-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        return
    if args.targeted:
        repo=SourceRepository(directory/'sources.db','smoke')
        configs=ToolConfigStore(directory/'control.db')
        for name in HIGH: configs.update('v2.'+name,True,False,'tool.invoke')
        section=repo.documents.nodes('existing-source','sections')[0]['id']
        report=json.loads((directory/'report.json').read_text())
        cases={'explicit_section':f'请读取 existing-source 的章节 {section}，根据原文概括并使用工具返回的 evidence_id 引用。',
               'structured_table_calculation':'Read structured tables in existing-source using read_document_table with max_chars=100000 and limit=1. Read further tables if necessary. Compute the change between two comparable annual values with calculate. Cite the exact evidence_id from tool output; do not guess units or applicability.'}
        for name,question in cases.items():
            result=runtime(directory).run(question,conversation=name)
            (directory/(name+'-private-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
            report['cases'][name]=redacted(result)
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(name,result['status'],flush=True)
        return
    directory.mkdir(parents=True,exist_ok=True)
    if (directory/'sources.db').exists(): raise ValueError('Fresh output directory required')
    existing=Path('data/dify-formal-closure-accepted-20261101/submitted.md')
    payload=existing.read_bytes();copy=directory/'source.md';copy.write_bytes(payload)
    repo=SourceRepository(directory/'sources.db','smoke')
    source=repo.create(dict(source_id='existing-source',filename='existing-source.md',normalized_filename='existing-source',
        file_hash=sha(payload),local_pdf_path=str(copy.resolve()),local_md_path=str(copy.resolve()),parse_status='success'))
    repo.save_revision(source,payload.decode(),'mineru','Isolated copy of existing real submitted Markdown')
    IndexManager(repo.documents).sync()
    sections=repo.documents.nodes('existing-source','sections');tables=repo.documents.nodes('existing-source','tables')
    report={'model':Settings().llm_model,'source_sha256':sha(payload),'source_bytes':len(payload),
        'sections':len(sections),'tables':len(tables),'scope':'isolated existing real Markdown; real LLM; no production writes',
        'vector':'unconfigured in isolated service; exercises genuine local degradation, not a real Dify outage',
        'cases':{}}
    metadata=Path('data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl')
    qa=next(json.loads(line) for line in metadata.read_text().splitlines() if json.loads(line)['id']=='tatqa_test_9063')
    cases={'simple':qa['question'],
        'section':'授权文档 existing-source。请阅读文档，使用其中真实的章节 ID 读取相关章节，说明数值适用年份并引用。证据没有年份时明确说明。',
        'table':'授权文档 existing-source。请用 read_document_table 读取结构化表格，选取同一行、单位一致的两个年度数值，计算百分比变化并引用原表。无法确认表头或口径时说明不足。',
        'user_calculation':'2018 年收入 125.6，2019 年收入 132.4，增长率是多少？这两个数字口径相同。',
        'degraded':'Search with hybrid for operating income in document existing-source. If vector is unavailable, continue with verifiable local evidence and state the limitation.'}
    for name,question in cases.items():
        try:
            result=runtime(directory).run(question,conversation=name)
            (directory/(name+'-private-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
            report['cases'][name]=redacted(result)
            if name=='user_calculation':
                report['cases'][name]['answer_correctness']='numeric_rule_passed' if '5.414012738853503' in result['result']['answer'] else 'numeric_rule_not_passed'
        except Exception as exc: report['cases'][name]={'status':'failed','error_type':type(exc).__name__}
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(name,report['cases'][name]['status'],flush=True)
    first=runtime(directory,approval=True).run(qa['question'],conversation='approval-restart')
    phases=[redacted(first)];current=first
    for _ in range(8):
        if current['status']!='waiting_approval': break
        ident=next(e['approval_id'] for e in reversed(current['events']) if e['type']=='approval_required')
        ToolApprovalStore(directory/'control.db').decide(ident,'smoke-admin',True)
        # A new Python interpreter loads the official checkpoint and pending call.
        subprocess.run([sys.executable,'-m','scripts.smoke_react_v2','--output',str(directory),
                        '--resume-thread',current['thread_id']],check=True,timeout=360)
        current=json.loads((directory/'resume-result.json').read_text());phases.append(redacted(current))
    report['cases']['approval_process_restart']={'phases':phases,'final_status':current['status']}
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('report',str(directory/'report.json'),flush=True)


if __name__=='__main__': main()
