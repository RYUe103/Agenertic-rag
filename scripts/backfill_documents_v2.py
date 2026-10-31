"""Back up a source SQLite database, then idempotently backfill V2 document structures.

Run with PYTHONPATH=. .runtime/bin/python scripts/backfill_documents_v2.py
  --database data/sources.db --knowledge-base default-kb
Does not contact MinerU, Dify, or a model provider.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db import SourceRepository
from app.document_lock import document_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--knowledge-base', required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error('Database does not exist')
    backup = args.database.with_name(args.database.name + '.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.v1-backup')
    with document_lock(args.database.parent / '.document-operations.lock'):
        with sqlite3.connect(args.database) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        backup.chmod(0o600)
        repo = SourceRepository(args.database, args.knowledge_base)
        result = repo.documents.backfill_all()
    print(json.dumps({'backup': str(backup), 'results': result}, ensure_ascii=False, indent=2))
    return int(any(item['status'] == 'failed' for item in result))


if __name__ == '__main__':
    raise SystemExit(main())
