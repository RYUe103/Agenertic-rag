"""Read-only real Dify probe against an isolated copy of the legacy source database."""
import argparse
import json
import sqlite3
import time
from pathlib import Path
from app.config import Settings
from app.db import SourceRepository
from app.clients.dify import DifyClient
from app.retrieval_v2.adapters import DifyVectorAdapter
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.models import RetrievalRequest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path,default=Path('data/sources.db'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    target=args.output/'sources.db'
    if target.exists():raise RuntimeError('Use a new isolated directory')
    with sqlite3.connect('file:'+str(args.source)+'?mode=ro',uri=True) as original:
        with sqlite3.connect(target) as clone:original.backup(clone)
    with sqlite3.connect(target) as conn:
        row=conn.execute('SELECT knowledge_base_id FROM documents LIMIT 1').fetchone()
    kb=row[0] if row else 'default-kb'
    repo=SourceRepository(target,kb);repo.documents.backfill_all()
    s=Settings()
    adapter=DifyVectorAdapter(DifyClient(s.dify_base_url,s.dify_knowledge_api_key,s.dify_dataset_id))
    service=UnifiedRetrievalService({kb:repo.documents},AccessPolicy(lambda k:None,'isolated-smoke'),{kb:adapter})
    report={'kind':'real_dify_read_only_smoke','local_sources':len(repo.list_all()),'queries':[],
        'boundary':'Isolated local copy. No uploads. Existing corpus does not equal frozen 100-PDF benchmark.'}
    try:
        docs=adapter._json('GET','/datasets/'+s.dify_dataset_id+'/documents?page=1&limit=100',time.monotonic()+8)
        report['remote_documents']=docs.get('total');report['remote_has_more']=docs.get('has_more')
    except Exception as exc:report['catalog_error']=type(exc).__name__
    for query in ['预算','Token','API']:
        result=service.search(RetrievalRequest(query=query,mode='vector',scopes=[{'knowledge_base_id':kb}],budget={'timeout_seconds':12}))
        report['queries'].append(dict(query=query,status=result.status,verified_results=len(result.results),diagnostics=result.diagnostics))
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
