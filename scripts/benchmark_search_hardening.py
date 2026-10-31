"""Isolated phase-2 acceptance: frozen real corpus or explicitly synthetic scale."""
import argparse
import hashlib
import json
import platform
import re
import resource
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pymupdf
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.search.service import LocalSearchService
from app.search.tokenization import tokens


def measured(fn):
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter()-start


def add(repo, output, doc, text, filename=None, file_hash=None, path=None, **metadata):
    folder = output/doc
    folder.mkdir()
    md = folder/'document.md'
    md.write_text(text)
    source = repo.create(dict(source_id=doc, filename=filename or doc+'.txt',
        normalized_filename=filename or doc, file_hash=file_hash or doc,
        local_pdf_path=str(path or md), local_source_path=str(path or md),
        local_md_path=str(md), parse_status='success', **metadata))
    repo.save_revision(source, text, 'local', 'isolated acceptance benchmark')


def synthetic(repo, output, count):
    for i in range(count):
        title = ['电缆检测报告','燃料材料研究','项目会议纪要','Financial performance'][i%4]
        prose = [
            '电缆型号 ZC-YJLHV22 26/35kV 3×500mm²，依据 GB/T 31840.3-2025 检测，合格率99.95%。',
            'U-10Zr and U-Mo metallic fuel experiment. Temperature and material characteristics vary.',
            '项目要求逐条审核，预算12345.67元，验收节点及负责人需要在会议中确认。',
            'Revenue, operating profit, tax and cash flows reflect financial performance during this period.',
        ][i%4]
        text = f'# {title} {i}\n\nDocument marker UNIQUE-{i:05}. Region {i%17}. Year {2000+i%27}.\n\n'
        repetitions = 300 if i%100 == 0 else 2+i%19
        text += '\n\n'.join(f'## Section {j}\n\n{prose} Measurement {i*0.73+j:.4f}.' for j in range(repetitions))
        if i%25 == 0:
            text += '\n\n表 1 明细（单位：元）\n\n| 型号 | 年份 | 金额 | 合格率 |\n|---|---|---|---|\n'
            text += ''.join(f'| R-{i}-{j} | {2000+j%27} | {i+j/100:.4f}元 | 99.95% |\n' for j in range(120))
        add(repo, output, f'syn-{i:05}', text, document_type='txt', standard_year=2000+i%27,
            standard_title=title)
    return []


def real(repo, output, baseline, data):
    required = {r['sha256']:r for r in baseline['records']}
    paths = {}
    for path in sorted(data.rglob('*.pdf')):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in required and digest not in paths:
            paths[digest] = path.resolve()
    if set(paths) != set(required):
        raise RuntimeError(f'Missing frozen PDFs: {set(required)-set(paths)}')
    records = []
    for record in baseline['records']:
        path = paths[record['sha256']]
        with pymupdf.open(path) as pdf:
            text = '\n\n'.join(f'<!-- page: {i+1} -->\n'+page.get_text() for i,page in enumerate(pdf))
        assert len(text.encode()) == record['markdown_bytes'], 'Parser output changed from baseline'
        add(repo,output,record['document_id'],text,path.name,record['sha256'],path,document_type='pdf')
        records.append({**record, 'filename':path.name})
    return records


def quality(service, baseline, metadata):
    questions = {row['id']:row for row in map(json.loads, metadata.read_text().splitlines())}
    judgments, misses = [], []
    for gold in baseline['quality']['judgments']:
        row = questions[gold['question_id']]
        hits = service.search(row['question'],top_k=10)['results']
        rank = next((i+1 for i,h in enumerate(hits) if h['document_id']==gold['target_document']),None)
        judgments.append({**gold, 'rank_in_top10_chunks':rank})
        if gold['rank_in_top10_chunks'] is None:
            diagnostic = service.search(row['question'],top_k=100)['results']
            diagnostic_rank = next((i+1 for i,h in enumerate(diagnostic) if h['document_id']==gold['target_document']),None)
            document = gold['target_document']
            text = service.repo.version(document).markdown
            chunks = list(service.repo.iter_chunks(document))
            terms = set(tokens(row['question']))
            raw_terms = set(tokens(text))
            numeric = list(dict.fromkeys(re.findall(r'(?<!\w)-?\d[\d,]*(?:\.\d+)?%?', str(row.get('context','')))))
            matches = {n:n.casefold() in text.casefold() for n in numeric}
            target_hits = service.search(row['question'], document_ids=[document], top_k=3)['results']
            answer = str(row.get('original_answer',row.get('program_answer','')))
            misses.append(dict(question_id=row['id'], question=row['question'], target_document=document,
                filename=service.repo.sources.get(document).filename,
                original_answer=row.get('original_answer'), program_answer=row.get('program_answer'),
                gold_context=row.get('context'), original_rank=None, rank_in_top100=diagnostic_rank,
                indexed_chunks=len(chunks), markdown_chars=len(text),
                gold_numeric_presence=matches, answer_literal_present=answer.casefold() in text.casefold(),
                query_tokens=sorted(terms), missing_query_tokens=sorted(terms-raw_terms),
                raw_text_covered=all(any(c['locator']['start']<=b['locator']['start'] and
                    c['locator']['end']>=b['locator']['end'] for c in chunks)
                    for b in service.repo.nodes(document,'blocks') if len(b['content'])<1800),
                structured_tables=len(service.repo.nodes(document,'tables')),
                restricted_top_chunks=[dict(chunk_id=h['chunk_id'],content=h['content'],score=h['score']) for h in target_hits],
                extracted_text=text))
    count = len(judgments)
    ranks = [r['rank_in_top10_chunks'] for r in judgments]
    return dict(questions=count, judgments=judgments,
        source_document_hit_at_1=sum(r==1 for r in ranks)/count,
        source_document_hit_at_5=sum(r is not None and r<=5 for r in ranks)/count,
        source_document_hit_at_10=sum(r is not None for r in ranks)/count,
        mrr_at_10=sum(1/r for r in ranks if r)/count), misses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=['real','synthetic','business'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--count',type=int,default=2001)
    parser.add_argument('--input',type=Path,default=Path('data'))
    parser.add_argument('--baseline',type=Path,default=Path('docs/LOCAL_SEARCH_BENCHMARK_2026-10-31.json'))
    parser.add_argument('--metadata',type=Path,default=Path('data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl'))
    parser.add_argument('--business-db',type=Path,default=Path('data/sources.db'))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    database = output/'sources.db'
    if database.exists():
        parser.error('Fresh isolated output directory required')
    baseline = json.loads(args.baseline.read_text())
    if args.kind == 'business':
        with sqlite3.connect(f'file:{args.business_db.resolve()}?mode=ro',uri=True) as original, sqlite3.connect(database) as clone:
            original.backup(clone)
        with sqlite3.connect(database) as clone:
            row = clone.execute('SELECT knowledge_base_id FROM documents LIMIT 1').fetchone()
            kb = row[0] if row else 'default-kb'
        repo = SourceRepository(database,kb)
        records, import_seconds = measured(repo.documents.backfill_all)
    else:
        repo = SourceRepository(database,'acceptance-'+args.kind)
        records, import_seconds = measured(lambda: real(repo,output,baseline,args.input)
            if args.kind=='real' else synthetic(repo,output,args.count))
    manager = IndexManager(repo.documents)
    built, build_seconds = measured(manager.sync)
    service = LocalSearchService(repo.documents,lambda kb,doc:repo.get(doc),'benchmark')
    # New Python process, warm OS file cache; not an OS cold boot.
    code = ('from pathlib import Path; from app.db import SourceRepository; '
            'from app.search.index_manager import IndexManager; import time,json; '
            't=time.perf_counter(); m=IndexManager(SourceRepository(Path(__import__("sys").argv[1]),'
            '__import__("sys").argv[2]).documents); m.open_current(); print(json.dumps({"load_seconds":time.perf_counter()-t}))')
    restart, restart_wall = measured(lambda: subprocess.run([sys.executable,'-c',code,str(database),repo.documents.knowledge_base_id],
        check=True,capture_output=True,text=True))
    query_list = ['financial results','total revenue','operating profit','cash flow','2020','2019',
                  'share capital','income tax','total assets','risk management']
    if args.kind != 'real':
        query_list = ['电缆','GB/T 31840.3-2025','U-10Zr','U-Mo','99.95%','12345.67元',
                      '项目验收','cash flow','26/35kV','3×500mm²']
    if args.kind == 'business':
        query_list = ['篮球','秋游','Token','API','risk','NIST','Artificial intelligence','预算','活动','安全']
    latency = []
    for i in range(100):
        _, duration = measured(lambda:service.search(query_list[i%len(query_list)]))
        latency.append(duration*1000)
    scored, misses = quality(service,baseline,args.metadata) if args.kind=='real' else ({},[])
    smoke = []
    if args.kind == 'business':
        for query in query_list:
            matches = service.exact(query,case_sensitive=False,limit=100,timeout=2)
            smoke.append(dict(query=query,bm25_hits=len(service.search(query)['results']),
                exact_hits=len(matches['results']),exact_partial=matches['partial']))
    exact = []
    regex_query = r'UNIQUE-[0-9]{5}' if args.kind=='synthetic' else r'\d+\.\d+%'
    for query, regexp in [('2019',False), ('__ABSENT_ACCEPTANCE_SENTINEL__',False), (regex_query,True)]:
        token, pages, chars, hits = None,0,0,0
        started = time.perf_counter()
        while True:
            page = service.exact(query,regex_mode=regexp,limit=100,timeout=2,
                                 max_chars=2_000_000,continuation=token)
            pages += 1; chars += page['scanned_chars']; hits += len(page['results'])
            token = page['continuation']
            if not page['partial'] or pages>=200:
                break
        exact.append(dict(query=query,regex_mode=regexp,pages=pages,scanned_chars=chars,hits=hits,
            seconds=time.perf_counter()-started,complete=not page['partial'],last_reason=page['reason']))
    docs = repo.list_all()
    first = docs[0]
    before_status = manager.status()
    with repo.connect() as conn:
        chars, markdown_bytes = 0,0
        for r in conn.execute('SELECT v.markdown FROM documents d JOIN document_versions v ON v.id=d.current_version_id WHERE d.deleted=0'):
            chars += len(r[0]); markdown_bytes += len(r[0].encode())
    # Operational-only updates must produce no generation.
    repo.update(first.source_id,dify_status='failed',error_message='isolated status benchmark')
    noop, status_seconds = measured(manager.sync)
    assert noop['noop']
    repo.save_revision(repo.get(first.source_id),repo.documents.version(first.source_id).markdown,'local','same-content reparse')
    incremental, incremental_seconds = measured(manager.sync)
    assert incremental['changed']==1
    start = time.perf_counter()
    for doc in docs[:min(100,len(docs))]:
        repo.update(doc.source_id,standard_title='Metadata batch '+doc.source_id)
    batch_write_seconds = time.perf_counter()-start
    batch, batch_seconds = measured(manager.sync)
    _, delete_write_seconds = measured(lambda:repo.delete(first.source_id))
    _, delete_seconds = measured(manager.sync)
    _, rebuild_seconds = measured(lambda:manager.sync(full=True))
    final_status = manager.status()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = dict(kind=args.kind,documents=len(docs),chunks=built['chunks'],
        markdown_bytes=markdown_bytes,markdown_chars=chars,sqlite_bytes=database.stat().st_size,
        active_index_bytes=before_status['current_bytes'],total_index_bytes=final_status['total_bytes'],
        retained_generations=final_status['generations'],parse_and_repository_seconds=import_seconds,
        full_index_seconds=build_seconds,incremental_index_seconds=incremental_seconds,
        operational_update_noop=noop['noop'],operational_sync_seconds=status_seconds,
        batch_metadata_documents=batch['changed'],batch_metadata_write_seconds=batch_write_seconds,
        batch_metadata_index_seconds=batch_seconds,delete_repository_seconds=delete_write_seconds,
        delete_index_seconds=delete_seconds,rebuild_seconds=rebuild_seconds,
        restart_load_seconds=json.loads(restart.stdout)['load_seconds'],restart_process_wall_seconds=restart_wall,
        query_count=100,query_p50_ms=statistics.median(latency),query_p95_ms=sorted(latency)[94],
        exact=exact,peak_rss_bytes=rss if sys.platform=='darwin' else rss*1024,
        environment=dict(platform=platform.platform(),machine=platform.machine(),python=sys.version,
            hardware=subprocess.run(['sysctl','-n','machdep.cpu.brand_string'],capture_output=True,text=True).stdout.strip()),
        boundaries='Warm OS cache. Queries include SQLite and per-document authorization. Build excludes parsing/import. '
            'Peak RSS covers import/index/query in this process. Restart is new process, warm OS cache. No Dify/MinerU calls.',
        quality=scored,records=records,baseline_sha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest())
    result['business_smoke'] = smoke
    result['builder_versions'] = sorted({s['builder_version'] for s in manager._snapshot()[0]['documents'].values()})
    (output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    if misses:
        (output/'misses.json').write_text(json.dumps(misses,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('quality','records','retained_generations')},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
