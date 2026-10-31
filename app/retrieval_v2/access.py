"""Request-local scope snapshots. No authorization decisions survive a request."""
from app.search.snapshot import snapshot
import hashlib


class AccessPolicy:
    def __init__(self, authorize_kb, principal, authorize_documents=None):
        self.authorize_kb = authorize_kb
        self.principal = principal
        # Optional batch ACL: returns allowed IDs, must be evaluated live each time.
        self.authorize_documents = authorize_documents

    def scope(self, repo, requested, filters):
        kb = repo.knowledge_base_id
        self.authorize_kb(kb)
        with repo.sources.connect() as conn:
            conn.execute('BEGIN')
            current = snapshot(conn,kb,bounded_scope=True,document_ids=requested.document_ids)['documents']
            allowed = set(current)
            if self.authorize_documents:
                allowed &= set(self.authorize_documents(kb, sorted(allowed)))
            result = {}
            for doc, spec in current.items():
                if doc not in allowed:
                    continue
                meta = spec['metadata']
                if filters.document_type and meta.get('document_type') != filters.document_type:
                    continue
                if any(meta.get(k)!=v for k,v in filters.metadata.items()):
                    continue
                spec = {**spec, 'current_version_id':spec['version_id']}
                if doc in requested.versions:
                    row = conn.execute('''SELECT v.id,v.content_hash,c.builder_version,
                        length(CAST(v.markdown AS BLOB)) AS markdown_bytes FROM document_versions v
                        JOIN chunk_selection c ON c.version_id=v.id WHERE v.id=? AND v.document_id=?''',
                        (requested.versions[doc],doc)).fetchone()
                    if row is None:
                        raise ValueError('Version does not belong to requested document')
                    spec.update(version_id=row['id'], content_hash=row['content_hash'],
                                builder_version=row['builder_version'],markdown_bytes=row['markdown_bytes'])
                result[doc] = spec
            # Mapping acknowledgements can change independently of source metadata.
            # Include their digest in the final scope recheck, without N connections.
            mapping_parts = {}
            for row in conn.execute('''SELECT v.document_id,m.version_id,m.payload FROM external_index_mappings m
                JOIN document_versions v ON v.id=m.version_id WHERE json_extract(m.payload,'$.adapter')='dify'
                ORDER BY m.id'''):
                if row['document_id'] in result and row['version_id']==result[row['document_id']]['version_id']:
                    mapping_parts.setdefault(row['document_id'],[]).append(row['payload'])
            for doc,spec in result.items():
                spec['external_mapping_digest']=hashlib.sha256('\n'.join(mapping_parts.get(doc,[])).encode()).hexdigest()
        return result
