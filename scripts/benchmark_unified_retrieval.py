"""Phase 3 frozen-corpus acceptance. No Dify uploads or synthetic vector quality claims."""
import argparse
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.search.service import LocalSearchService
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.models import RetrievalRequest
from app.retrieval_v2.service import UnifiedRetrievalService
from scripts.benchmark_search_hardening import real, synthetic


def percentile(values):
    return dict(p50_ms=statistics.median(values),p95_ms=sorted(values)[int((len(values)-1)*.95)])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=['real','synthetic'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    if (output/'sources.db').exists():
        raise RuntimeError('Use a fresh output directory; benchmark is immutable')
    repo=SourceRepository(output/'sources.db','benchmark')
    baseline_path=Path('docs/LOCAL_SEARCH_BENCHMARK_2026-10-31.json')
    baseline=json.loads(baseline_path.read_text())
    started=time.monotonic()
    records=real(repo,output,baseline,Path('data')) if args.kind=='real' else synthetic(repo,output,2001)
    built=IndexManager(repo.documents).sync()
    print(json.dumps({'kind':args.kind,'prepared_seconds':time.monotonic()-started,'chunks':built['chunks']}),flush=True)
    legacy=LocalSearchService(repo.documents,lambda k,d:None,'benchmark')
    service=UnifiedRetrievalService({'benchmark':repo.documents},AccessPolicy(lambda k:None,'benchmark'))
    questions={r['id']:r for r in map(json.loads,Path('data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl').read_text().splitlines())}
    sample=[questions[g['question_id']]['question'] for g in baseline['quality']['judgments']]
    if args.kind=='synthetic':
        sample=(['电缆','GB/T 31840.3-2025','U-10Zr','U-Mo','99.95%','12345.67元',
                 '项目验收','cash flow','26/35kV','3×500mm²']*10)
    modes={'phase2_local':[],'v2_lexical_unlimited_document_cap':[],'v2_lexical_diverse':[]}
    details={k:[] for k in modes};timings=[];integrity=[]
    # Warm each local code path once. This is a warm-cache query measurement.
    legacy.search(sample[0]);service.search(RetrievalRequest(query=sample[0],mode='lexical',scopes=[{'knowledge_base_id':'benchmark'}]))
    for i,q in enumerate(sample):
        for mode,latencies in modes.items():
            t=time.monotonic()
            if mode=='phase2_local':
                hits=legacy.search(q)['results'];diag={}
                elapsed=(time.monotonic()-t)*1000
            else:
                result=service.search(RetrievalRequest(query=q,mode='lexical',scopes=[{'knowledge_base_id':'benchmark'}],
                    budget={'timeout_seconds':10,'context_chars':100000},
                    fusion={'max_chunks_per_document':100 if mode.endswith('cap') else 3}))
                hits=[h.model_dump() for h in result.results];diag=result.diagnostics
                elapsed=(time.monotonic()-t)*1000
                if mode=='v2_lexical_diverse':
                    timings.append(diag)
                    keys=[(h['document_id'],h['version_id'],h['chunk_id']) for h in hits]
                    integrity.append(dict(returned=len(hits),unique=len(set(keys)),
                        verified=sum(h['verification_status']=='verified_current' for h in hits),
                        version_consistent=sum(repo.documents.document(h['document_id']).current_version_id==h['version_id'] for h in hits)))
            latencies.append(elapsed)
            if args.kind=='real':
                gold=baseline['quality']['judgments'][i]
                rank=next((n+1 for n,h in enumerate(hits) if h['document_id']==gold['target_document']),None)
                details[mode].append(dict(question_id=gold['question_id'],target_document=gold['target_document'],
                    rank=rank,chunks=[h['chunk_id'] for h in hits],document_ids=[h['document_id'] for h in hits]))
        if (i+1)%20==0: print(f'{args.kind}: {i+1}/100 questions',flush=True)
    results={}
    for mode,values in modes.items():
        ranks=[d['rank'] for d in details[mode]]
        results[mode]={**percentile(values),'query_count':len(values)}
        if ranks:
            results[mode].update(source_hit_at_1=sum(r==1 for r in ranks)/len(ranks),
                source_hit_at_5=sum(r is not None and r<=5 for r in ranks)/len(ranks),
                source_hit_at_10=sum(r is not None for r in ranks)/len(ranks),
                mrr_at_10=sum(1/r for r in ranks if r)/len(ranks))
    component={k:percentile([d[k] for d in timings]) for k in ['authorization_ms','evidence_ms','fusion_ms','total_ms']}
    total=sum(d['returned'] for d in integrity)
    metrics=dict(returned=total,verified_rate=sum(d['verified'] for d in integrity)/max(total,1),
        version_consistency=sum(d['version_consistent'] for d in integrity)/max(total,1),
        duplicate_rate=1-sum(d['unique'] for d in integrity)/max(total,1),
        nonempty_query_rate=sum(d['returned']>0 for d in integrity)/len(integrity),
        evidence_recall=None,correct_table_recall=None,
        annotation_status='No gold source spans; verification is integrity, not relevance or answer accuracy')
    remaining={'tatqa_test_'+str(i) for i in [9083,9084,9085,9097,9098,9119,9144,9157]}
    misses={k:[r for r in v if r['question_id'] in remaining] for k,v in details.items()}
    annotations=[]
    if args.kind=='real':
        for gold in baseline['quality']['judgments']:
            if gold['question_id'] not in remaining:continue
            q=questions[gold['question_id']]
            candidates=service.search(RetrievalRequest(query=q['question'],mode='lexical',
                scopes=[{'knowledge_base_id':'benchmark','document_ids':[gold['target_document']]}],top_k=3))
            annotations.append(dict(question_id=q['id'],question=q['question'],gold_context=q.get('context'),
                status='pending_human_review_not_gold',candidates=[h.model_dump() for h in candidates.results]))
    report=dict(kind=args.kind,documents=len(repo.list_all()),chunks=built['chunks'],
        baseline_sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(),records=records,
        environment=dict(platform=platform.platform(),python=platform.python_version()),
        results=results,component_timings=component,evidence_integrity=metrics,judgments=details,
        remaining_eight=misses,vector='not_evaluated_same_corpus_not_verified',
        hybrid='not_evaluated_no_comparable_real_vector_results',reranker='none',
        conditions='Fresh frozen corpus, original questions/split. Warm cache. Same live snapshot/permissions; no production data mutations. No score tuning on test cases.')
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (output/'annotations.json').write_text(json.dumps(annotations,ensure_ascii=False,indent=2))
    print(json.dumps({'results':results,'components':component,'integrity':metrics},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
