"""Reconstruct ONE historical upload in an isolated KB using manifest ID + original SHA256.

All remote operations are GET/retrieve. Never enables, uploads or deletes remote documents.
"""
import argparse
import json
import time
from pathlib import Path
from app.config import Settings
from app.db import SourceRepository
from app.clients.dify import DifyClient
from app.dify_mapping import remote_file_info
from app.search.index_manager import IndexManager
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.adapters import DifyVectorAdapter
from app.retrieval_v2.models import RetrievalRequest
from app.retrieval_v2.service import UnifiedRetrievalService
from scripts.verify_dify_mapping_v2 import digest, original_file, redacted_result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if (args.output/'sources.db').exists():raise ValueError('Fresh isolated output required')
    settings=Settings();dataset=settings.dify_dataset_id
    adapter=DifyVectorAdapter(DifyClient(settings.dify_base_url,settings.dify_knowledge_api_key,dataset))
    manifest_path=Path('data/t2-ragbench/results/formal-ingest-100.json')
    manifest=json.loads(manifest_path.read_text())
    catalog=adapter._json('GET',f'/datasets/{dataset}/documents?page=1&limit=100',time.monotonic()+15)
    if catalog.get('has_more'):raise ValueError('Catalog incomplete')
    remote={r['id']:r for r in catalog['data']}
    candidates=[r for r in manifest if remote.get(r.get('document_id'),{}).get('enabled') is True]
    report={'boundary':'One isolated historical formal-ingestion document. Real read-only Dify. No production writes.',
        'manifest_sha256':digest(manifest_path.read_bytes()),'remote_enabled_manifest_matches':len(candidates),
        'source_proof':None,'modes':{},'true_hybrid_verified':False}
    if not candidates:raise ValueError('No enabled historical upload with stable Document ID')
    row=candidates[0];doc=remote[row['document_id']]
    path=Path('data/t2-ragbench/parsed')/row['context_id']/'mineru-original.md'
    local_bytes=path.read_bytes();downloaded=original_file(adapter,dataset,doc['id'])
    if digest(local_bytes)!=digest(downloaded):raise ValueError('Historical local payload differs from remote original')
    info=adapter._json('GET',f'/datasets/{dataset}/documents/{doc["id"]}',time.monotonic()+15)
    if remote_file_info(info).get('id')!=remote_file_info(doc).get('id'):raise ValueError('Remote changed during download')
    kb='isolated-formal-evidence';repo=SourceRepository(args.output/'sources.db',kb)
    sid='formal-'+row['context_id']
    # Preserve the actual prior submitted Markdown; do not reconstruct it from retrieved segments.
    payload_path=args.output/'submitted.md';payload_path.write_bytes(local_bytes)
    source=repo.create(dict(source_id=sid,filename='audited-submission.md',normalized_filename='audited-submission',
        file_hash=digest(local_bytes),local_pdf_path=str(payload_path.resolve()),local_md_path=str(payload_path.resolve()),
        parse_status='success',dify_dataset_id=dataset,dify_document_id=doc['id'],dify_status='success'))
    repo.save_revision(source,local_bytes.decode('utf-8'),'mineru','Historical upload manifest and exact remote original verified')
    mapping=repo.documents.verify_legacy_dify_file(sid,dataset,doc['id'],info,downloaded)
    report['source_proof']=dict(local_payload_sha256=digest(local_bytes),remote_original_sha256=digest(downloaded),
        remote_document=digest(doc['id']),remote_file=digest(remote_file_info(info)['id']),
        source=digest(sid),version=digest(mapping.version_id),status=mapping.status,bytes=len(local_bytes),
        remote_enabled=info.get('enabled'),remote_archived=info.get('archived'),binding=mapping.binding_method)
    IndexManager(repo.documents).sync()
    service=UnifiedRetrievalService({kb:repo.documents},AccessPolicy(lambda k:None,'isolated-formal-auditor'),{kb:adapter})
    qa=next(r for r in map(json.loads,Path('data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl').read_text().splitlines()) if r['context_id']==row['context_id'])
    report['question_id']=qa['id'];report['query_sha256']=digest(qa['question'])
    request=RetrievalRequest(query=qa['question'],mode='lexical',scopes=[{'knowledge_base_id':kb,'document_ids':[sid]}],
        top_k=10,budget={'backend_candidates':100,'timeout_seconds':20},fusion={'max_chunks_per_document':10})
    for mode in ['lexical','vector','hybrid']:
        result=service.search(request.model_copy(update={'mode':mode}))
        report['modes'][mode]=redacted_result(result)
    report['true_hybrid_verified']=any({'lexical','vector'}<=set(p['adapter'] for p in h['provenance']) for h in report['modes']['hybrid']['results'])
    # The remote service is unchanged. Only the isolated Repository current version changes.
    repo.save_revision(repo.get(sid),'Isolated replacement version; original evidence is no longer current.','local','stale evidence audit')
    report['after_version_change']=redacted_result(service.search(request.model_copy(update={'mode':'vector'})))
    service.access.authorize_kb=lambda kb:(_ for _ in ()).throw(PermissionError())
    try:service.search(request)
    except PermissionError:report['permission_revocation']='denied'
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
