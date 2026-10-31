"""Reproducible local PDF benchmark. No external services, no source DB writes."""
import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path
import pymupdf
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.search.service import LocalSearchService


def elapsed(fn):
    start=time.perf_counter(); value=fn()
    return value,time.perf_counter()-start


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=Path('data'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--metadata',type=Path)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if (args.output/'sources.db').exists():
        parser.error('Use a fresh output directory to measure full build')
    repo=SourceRepository(args.output/'sources.db','benchmark')
    seen=set(); records=[]; skipped=0
    start=time.perf_counter()
    for path in sorted(args.input.rglob('*.pdf')):
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen: continue
        seen.add(digest)
        try:
            with pymupdf.open(path) as pdf:
                text='\n\n'.join(f'<!-- page: {i+1} -->\n'+page.get_text() for i,page in enumerate(pdf))
                pages=len(pdf)
            if len(text.strip())<100: skipped+=1; continue
        except Exception:
            skipped+=1;continue
        doc_id='pdf_'+digest[:24]
        files=args.output/doc_id;files.mkdir()
        md=files/'document.md';md.write_text(text)
        s=repo.create(dict(source_id=doc_id,filename=path.name,file_hash=digest,
            normalized_filename=path.stem,local_pdf_path=str(path.resolve()),local_source_path=str(path.resolve()),
            local_md_path=str(md.resolve()),parse_status='success',document_type='pdf'))
        repo.save_revision(s,text,'local','benchmark import')
        records.append(dict(document_id=doc_id,sha256=digest,pages=pages,markdown_bytes=len(text.encode())))
        if len(records)>=args.count: break
    parsing_seconds=time.perf_counter()-start
    manager=IndexManager(repo.documents)
    built,build_seconds=elapsed(manager.sync)
    _,restart_seconds=elapsed(lambda: IndexManager(SourceRepository(repo.db_path,'benchmark').documents).open_current())
    service=LocalSearchService(repo.documents,lambda kb,doc:repo.get(doc))
    queries=['financial results','total revenue','operating profit','cash flow','2020','2019',
             'share capital','income tax','total assets','risk management']
    latency=[];nonempty=0
    for i in range(100):
        response,duration=elapsed(lambda:service.search(queries[i%len(queries)]))
        latency.append(duration*1000); nonempty+=bool(response['results'])
    quality = {'kind':'unlabeled nonempty responses only'}
    if args.metadata:
        by_context={Path(s.filename).stem:s.source_id for s in repo.list_all()}
        judged=[]
        for line in args.metadata.read_text().splitlines():
            row=json.loads(line)
            target=by_context.get(row['context_id'])
            if not target or not row['question'] or len(row['question'])>512:
                continue
            hits=service.search(row['question'],top_k=10)['results']
            rank=next((i+1 for i,h in enumerate(hits) if h['document_id']==target),None)
            judged.append({'question_id':row['id'],'target_document':target,'rank_in_top10_chunks':rank})
            if len(judged)>=100:break
        quality=dict(kind='TAT-DQA context_id is the gold source PDF; first 100 eligible questions; top-K chunks',
            questions=len(judged),source_document_hit_at_1=sum(r['rank_in_top10_chunks']==1 for r in judged)/max(1,len(judged)),
            source_document_hit_at_5=sum(r['rank_in_top10_chunks'] is not None and r['rank_in_top10_chunks']<=5 for r in judged)/max(1,len(judged)),
            source_document_hit_at_10=sum(r['rank_in_top10_chunks'] is not None for r in judged)/max(1,len(judged)),
            mrr_at_10=sum(1/r['rank_in_top10_chunks'] for r in judged if r['rank_in_top10_chunks'])/max(1,len(judged)),
            judgments=judged,limitation='Source PDF retrieval only; not answer accuracy, evidence recall, or table computation')
    gid=manager.current()['id']
    size=sum(p.stat().st_size for p in (manager.root/gid).rglob('*') if p.is_file())
    first=records[0]['document_id'];source=repo.get(first)
    # Explicit same-content reparse creates a genuine new version; no invented PDF.
    text=repo.documents.version(first).markdown
    repo.save_revision(source,text,'local','benchmark same-content reparse')
    _,incremental_seconds=elapsed(manager.sync)
    repo.delete(first)
    _,delete_seconds=elapsed(manager.sync)
    _,rebuild_seconds=elapsed(lambda:manager.sync(full=True))
    result=dict(documents=len(records),chunks=built['chunks'],markdown_bytes=sum(r['markdown_bytes'] for r in records),
        active_index_bytes=size,parse_and_repository_seconds=parsing_seconds,full_index_seconds=build_seconds,
        incremental_index_seconds=incremental_seconds,delete_index_seconds=delete_seconds,rebuild_seconds=rebuild_seconds,
        restart_load_seconds=restart_seconds,query_count=len(latency),query_p50_ms=statistics.median(latency),
        query_p95_ms=sorted(latency)[94],query_max_ms=max(latency),nonempty_queries=nonempty,
        skipped=skipped,parser='PyMuPDF local text, not MinerU; no inferred table geometry',
        quality=quality,
        records=records)
    (args.output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:({kk:vv for kk,vv in v.items() if kk!='judgments'} if k=='quality' else v)
                      for k,v in result.items() if k!='records'},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
