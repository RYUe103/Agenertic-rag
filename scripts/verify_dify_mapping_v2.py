"""Read-only Dify audit and real fusion on an isolated Repository copy. No uploads."""
import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from app.config import Settings
from app.db import SourceRepository
from app.clients.dify import DifyClient
from app.retrieval_v2.adapters import DifyVectorAdapter
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.models import RetrievalRequest
from app.search.index_manager import IndexManager
from app.dify_mapping import remote_file_info


class PacedAuditAdapter(DifyVectorAdapter):
    """Optional pacing for read-only audit of cloud request quotas; not production retrieval."""
    def __init__(self, client, spacing):
        super().__init__(client)
        self.spacing,self.last_request=spacing,0

    def _json(self, method, path, deadline, **kwargs):
        delay=max(0,self.spacing-(time.monotonic()-self.last_request))
        if delay:time.sleep(delay)
        self.last_request=time.monotonic()
        return super()._json(method,path,deadline,**kwargs)


def digest(value):
    return hashlib.sha256(value if isinstance(value,bytes) else value.encode()).hexdigest()


def original_file(adapter, dataset, doc):
    deadline=time.monotonic()+20
    response=adapter._json('GET',f'/datasets/{dataset}/documents/{doc}/download',deadline)
    url=response.get('url','')
    if urlsplit(url).scheme!='https' or urlsplit(url).username:
        raise ValueError('Unsafe download URL')
    data=bytearray()
    # Never send the Dify API credential to a storage host or log signed URLs.
    with httpx.Client(timeout=8,follow_redirects=False) as client:
        with client.stream('GET',url) as response:
            response.raise_for_status()
            for part in response.iter_bytes():
                if time.monotonic()>deadline:raise TimeoutError()
                data.extend(part)
                if len(data)>4_000_000:raise ValueError('Original file budget exceeded')
    return bytes(data)


def redacted_result(result):
    return dict(status=result.status,diagnostics=result.diagnostics,results=[dict(
        document=digest(h.document_id),version=digest(h.version_id),chunk=digest(h.chunk_id) if h.chunk_id else None,
        content_sha256=digest(h.content),content_chars=len(h.content),verification_status=h.verification_status,
        fused_score=h.fused_score,source_adapter=h.source_adapter,source_rank=h.source_rank,
        provenance=h.provenance,locator={'start':h.locator.get('start'),'end':h.locator.get('end'),
            'page_numbers':h.page_numbers,'block_ids':[digest(x) for x in h.locator.get('block_ids',[])],
            'section_ids':[digest(x) for x in h.section_ids],'table_ids':[digest(x) for x in h.table_ids]},
        matched_ranges=h.matched_ranges) for h in result.results])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path,default=Path('data/sources.db'))
    parser.add_argument('--request-spacing',type=float,default=0)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    database=args.output/'sources.db'
    if database.exists():raise ValueError('Fresh isolated output required')
    with sqlite3.connect('file:'+str(args.source)+'?mode=ro',uri=True) as original:
        with sqlite3.connect(database) as clone:original.backup(clone)
        original_versions=original.execute('SELECT count(*) FROM document_versions').fetchone()[0]
        original_mappings=original.execute('SELECT count(*) FROM external_index_mappings').fetchone()[0]
    with sqlite3.connect(database) as conn:
        row=conn.execute('SELECT knowledge_base_id FROM documents LIMIT 1').fetchone()
    kb=row[0] if row else 'default-kb'
    repo=SourceRepository(database,kb);repo.documents.backfill_all()
    if not 0<=args.request_spacing<=5:raise ValueError('Request spacing must be 0..5 seconds')
    s=Settings();adapter=PacedAuditAdapter(DifyClient(s.dify_base_url,s.dify_knowledge_api_key,s.dify_dataset_id),args.request_spacing)
    documents=[]
    for page in range(1,11):
        catalog=adapter._json('GET',f'/datasets/{s.dify_dataset_id}/documents?page={page}&limit=100',time.monotonic()+15)
        documents.extend(catalog.get('data',[]))
        if not catalog.get('has_more'):break
    else:raise ValueError('Remote catalog budget exceeded')
    sources=repo.list_all();known={x.dify_document_id:x for x in sources if x.dify_dataset_id==s.dify_dataset_id}
    report=dict(boundary='Read-only real Dify; local reconciliation only in isolated clone; no source bodies, names, credentials or signed URLs',
        original_versions=original_versions,original_mappings=original_mappings,local_documents=len(sources),
        remote_documents=len(documents),known_remote_documents=sum(d['id'] in known for d in documents),
        remote_index_states=dict(Counter(d['indexing_status'] for d in documents)),
        known_remote_disabled=sum(d.get('enabled') is False for d in documents if d['id'] in known),
        outside_remote_enabled=sum(d.get('enabled') is True for d in documents if d['id'] not in known),
        request_spacing_seconds=args.request_spacing,
        remote_duplicate_name_count=sum(n-1 for n in Counter(d['name'] for d in documents).values()),
        rejection_audit=[],source_audit=[],closure=[])
    for query in ['预算','Token','API']:
        started=time.monotonic();body=adapter.search(query,40,time.monotonic()+15)
        counters=Counter();ids=set()
        for item in body['candidates']:
            segment=item['external'].get('segment') or {};doc=segment.get('document_id')
            ids.add(doc)
            counters['outside_local_repository' if doc not in known else 'known_source_legacy_receipt_missing']+=1
        report['rejection_audit'].append(dict(query=query,candidates=len(body['candidates']),
            unique_remote_documents=len(ids),primary_reasons=dict(counters),elapsed_ms=(time.monotonic()-started)*1000,
            top_k_saturated=len(body['candidates'])==40))
        expanded=adapter.search(query,100,time.monotonic()+15)
        report['rejection_audit'][-1]['expanded_top100_candidates']=len(expanded['candidates'])
        report['rejection_audit'][-1]['known_documents_in_top100']=sum(
            x['external'].get('segment',{}).get('document_id') in known for x in expanded['candidates'])
    verified=[]
    for doc in documents:
        if doc['id'] not in known:continue
        source=known[doc['id']];version=repo.documents.version(source.source_id)
        item=dict(document=digest(source.source_id),remote_document=digest(doc['id']),version=digest(version.id),
            local_content_hash=version.content_hash,source_dataset_matches=True,remote_index_status=doc['indexing_status'],
            remote_enabled=doc.get('enabled'),remote_archived=doc.get('archived'))
        try:
            payload=original_file(adapter,s.dify_dataset_id,doc['id'])
            info=adapter._json('GET',f'/datasets/{s.dify_dataset_id}/documents/{doc["id"]}',time.monotonic()+15)
            # Details may be richer; preserve verified ID from requested catalog identity.
            if not info.get('id'):raise ValueError('Missing remote identity')
            file_before=remote_file_info(doc).get('id')
            file_after=remote_file_info(info).get('id')
            if file_before!=file_after:raise ValueError('Remote file changed during audit')
            item.update(remote_original_hash=digest(payload),remote_bytes=len(payload),
                        submitted_equals_local=digest(payload)==version.content_hash,
                        original_equals_local_source=digest(payload)==source.file_hash)
            if digest(payload)==version.content_hash:
                binding=repo.documents.verify_legacy_dify_file(source.source_id,s.dify_dataset_id,doc['id'],info,payload)
                if binding.status=='completed':verified.append(source)
                item['outcome']='verified_remote_original_sha256' if binding.status=='completed' else 'binding_verified_but_remote_disabled'
            else:item['outcome']='remote_original_not_current_markdown'
        except Exception as exc:
            item['outcome']=type(exc).__name__
            item['error_code']=getattr(exc,'code','audit_verification_failed')
        report['source_audit'].append(item)
    IndexManager(repo.documents).sync()
    service=UnifiedRetrievalService({kb:repo.documents},AccessPolicy(lambda k:None,'isolated-audit'),{kb:adapter})
    baseline=json.loads(Path('docs/LOCAL_SEARCH_BENCHMARK_2026-10-31.json').read_text())
    docs_by_hash={r['sha256']:r['document_id'] for r in baseline['records']}
    questions={r['id']:r for r in map(json.loads,Path('data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl').read_text().splitlines())}
    for source in verified:
        target=docs_by_hash.get(source.file_hash)
        gold=next((r for r in baseline['quality']['judgments'] if r['target_document']==target),None)
        question=questions[gold['question_id']]['question'] if gold else '秋游出行需要采取哪些安全保障措施？'
        entry={'question_id':gold['question_id'] if gold else 'business-safety-smoke',
            'query_sha256':digest(question),'document':digest(source.source_id),'modes':{}}
        for mode in ['lexical','vector','hybrid']:
            request=RetrievalRequest(query=question,mode=mode,scopes=[{'knowledge_base_id':kb,'document_ids':[source.source_id]}],
                top_k=10,budget={'backend_candidates':100,'timeout_seconds':20},fusion={'max_chunks_per_document':10})
            result=service.search(request)
            entry['modes'][mode]=redacted_result(result)
        report['closure'].append(entry)
        if any(len({p['adapter'] for p in h['provenance']})>1 for h in entry['modes']['hybrid']['results']):
            # Local-only version mutation: real remote index deliberately remains old.
            repo.save_revision(repo.get(source.source_id),'Isolated replacement version for stale-evidence rejection.','local','isolated audit')
            stale=service.search(request.model_copy(update={'mode':'vector'}))
            entry['after_version_change']=redacted_result(stale)
            service.access.authorize_kb=lambda kb:(_ for _ in ()).throw(PermissionError())
            try:service.search(request)
            except PermissionError:entry['permission_revocation']='denied'
            break
    report['true_hybrid_verified']=any(len({p['adapter'] for p in h['provenance']})>1
        for e in report['closure'] for h in e['modes']['hybrid']['results'])
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
