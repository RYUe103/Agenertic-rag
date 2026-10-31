"""Offline operator entry point. Database access is trusted administrator access."""
import argparse
import json
from pathlib import Path
from app.db import SourceRepository
from app.search.index_manager import IndexManager
from app.search.service import LocalSearchService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--knowledge-base',required=True)
    parser.add_argument('action',choices=['sync','rebuild','status','bm25','exact'])
    parser.add_argument('--query',default='')
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error('Database does not exist')
    repo = SourceRepository(args.database,args.knowledge_base).documents
    if args.action in ('sync','rebuild'):
        failures = [r for r in repo.backfill_all() if r['status']=='failed']
        if failures:
            raise RuntimeError(f'Backfill failed for {len(failures)} documents')
        result = IndexManager(repo).sync(full=args.action=='rebuild')
    elif args.action == 'status':
        result = IndexManager(repo).state()
    else:
        service = LocalSearchService(repo,lambda kb,doc: repo.document(doc))
        result = service.search(args.query) if args.action=='bm25' else service.exact(args.query)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
