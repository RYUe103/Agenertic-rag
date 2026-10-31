"""The indexed representation, read under a single SQLite snapshot.

Operational status and timestamps are deliberately excluded. Durable revisions
survive outbox acknowledgement (and document deletion), preventing ABA races.
"""
import json
from .tokenization import TOKENIZER_VERSION

INDEX_SCHEMA = 2
INDEX_CONFIG = 'bm25-fields-3-4-2-1-v1'
METADATA_FIELDS = ('filename', 'standard_title', 'standard_no', 'normalized_standard_no',
                   'standard_year', 'publish_date', 'effective_date', 'document_type',
                   'status', 'replaces', 'replaced_by', 'custom_metadata')


def metadata(row):
    result = {key: row[key] for key in METADATA_FIELDS}
    if isinstance(result['custom_metadata'], str):
        result['custom_metadata'] = json.loads(result['custom_metadata'])
    return result


def snapshot(conn, kb, bounded_scope=False, document_ids=None):
    ids = sorted(set(document_ids)) if document_ids is not None else None
    if ids is not None and len(ids)>10000:
        raise ValueError('At most 10000 explicit documents')
    scope_clause = (' AND s.source_id IN ('+','.join('?' for _ in ids)+')') if ids is not None else ''
    scope_args = ids or []
    if bounded_scope:
        sizes = '+'.join(f'COALESCE(length(CAST(s.{key} AS BLOB)),0)' for key in METADATA_FIELDS)
        size = conn.execute(f'SELECT count(*),COALESCE(sum({sizes}),0) FROM sources s WHERE 1=1'+scope_clause,scope_args).fetchone()
        if size[0] > 100000 or size[1] > 16_000_000:
            raise ValueError('Exact scope exceeds 100000 documents or 16 MB metadata; narrow document_ids')
    docs = {}
    columns = ','.join('s.'+key for key in METADATA_FIELDS)
    rows = conn.execute(f'''SELECT s.source_id,{columns}, d.current_version_id, v.content_hash,
        length(CAST(v.markdown AS BLOB)) AS markdown_bytes,
        (SELECT count(*) FROM retrieval_chunks x WHERE x.version_id=v.id AND x.builder_version=c.builder_version) AS chunk_count,
        c.builder_version AS selected_builder, COALESCE(r.revision,0) AS search_revision
        FROM documents d JOIN sources s ON s.source_id=d.id
        JOIN document_versions v ON v.id=d.current_version_id
        LEFT JOIN chunk_selection c ON c.version_id=v.id
        LEFT JOIN search_revisions r ON r.document_id=d.id
        WHERE d.deleted=0 AND d.knowledge_base_id=?
        AND NOT EXISTS(SELECT 1 FROM document_deletions x WHERE x.document_id=d.id)
        '''+scope_clause+' ORDER BY d.id', (kb,*scope_args))
    for row in rows:
        docs[row['source_id']] = dict(document_id=row['source_id'],
            version_id=row['current_version_id'], content_hash=row['content_hash'],
            builder_version=row['selected_builder'], metadata=metadata(row),
            revision=row['search_revision'], chunk_count=row['chunk_count'], markdown_bytes=row['markdown_bytes'])
    return dict(schema=INDEX_SCHEMA, config=INDEX_CONFIG, tokenizer=TOKENIZER_VERSION,
                knowledge_base_id=kb, documents=docs)


def compatible(manifest, kb):
    return (manifest.get('schema') == INDEX_SCHEMA and manifest.get('config') == INDEX_CONFIG
            and manifest.get('tokenizer') == TOKENIZER_VERSION and manifest.get('knowledge_base_id') == kb)


def representation(spec):
    return {k: v for k, v in spec.items() if k != 'revision'}
