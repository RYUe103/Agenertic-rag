"""Transactional, versioned source storage. Internal repository; use read service at boundaries."""
import hashlib
import json
from pathlib import Path

from .document_models import Document, DocumentVersion, ExternalIndexMapping
from .document_structure import build_structure
from .document_lock import document_lock


SCHEMA = '''
CREATE TABLE IF NOT EXISTS document_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO document_schema VALUES(1);
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL, current_version_id TEXT,
 metadata TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS document_versions(
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version_no INTEGER NOT NULL,
 markdown TEXT NOT NULL, content_hash TEXT NOT NULL, builder_version TEXT NOT NULL,
 provenance TEXT NOT NULL, UNIQUE(document_id,version_no));
CREATE TABLE IF NOT EXISTS document_nodes(
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL, kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
 payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS document_nodes_version ON document_nodes(version_id,kind,ordinal);
CREATE TABLE IF NOT EXISTS document_artifacts(
 hash TEXT PRIMARY KEY, content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS document_version_artifacts(
 version_id TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(version_id,name));
CREATE TABLE IF NOT EXISTS external_index_mappings(
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS external_mapping_version ON external_index_mappings(version_id);
CREATE TABLE IF NOT EXISTS document_backfill_errors(
 document_id TEXT PRIMARY KEY, error TEXT NOT NULL);
'''


class DocumentRepository:
    def __init__(self, sources, knowledge_base_id):
        self.sources = sources
        self.knowledge_base_id = knowledge_base_id
        with sources.connect() as conn:
            conn.executescript(SCHEMA)
            existing = conn.execute('SELECT DISTINCT knowledge_base_id FROM documents').fetchall()
            if any(row[0] != knowledge_base_id for row in existing):
                raise ValueError('Repository knowledge base mismatch')

    def _artifacts(self, source):
        # Snapshot only known parser artifacts and original bytes, not secrets or arbitrary directory files.
        root = Path(source.source_path).parent
        paths = {'original': Path(source.source_path)}
        for name in ('extracted.md', 'mineru-original.md', 'mineru-response.json',
                     'mineru-content-list.json', 'mineru-result.zip', 'metadata.json',
                     'workflow.md', 'workflow-result.json'):
            paths[name] = root / name
        if source.local_extracted_md_path:
            paths['extracted.md'] = Path(source.local_extracted_md_path)
        return {name: path.read_bytes() for name, path in paths.items() if path.is_file()}

    def publish(self, conn, source, version_id, version_no, markdown, provenance, artifacts=None):
        known = conn.execute('SELECT 1 FROM document_versions WHERE id=?', (version_id,)).fetchone()
        if known:
            return version_id
        artifacts = artifacts or {}
        content_list = []
        parser_engine = provenance.get('parser_engine') or provenance.get('legacy_revision', {}).get('parser_engine')
        if 'mineru-content-list.json' in artifacts and parser_engine not in {'local', 'local_fallback', 'qwenvl'}:
            try:
                parsed = json.loads(artifacts['mineru-content-list.json'])
                content_list = parsed if isinstance(parsed, list) else []
            except (ValueError, UnicodeDecodeError):
                # Keep malformed original bytes for audit; valid Markdown remains publishable.
                content_list = []
        structure = build_structure(markdown, version_id, content_list)
        version = DocumentVersion(id=version_id, document_id=source.source_id, version_no=version_no,
                                  markdown=markdown, content_hash=hashlib.sha256(markdown.encode()).hexdigest(),
                                  provenance={**provenance, 'source_metadata': None if 'legacy_revision' in provenance else source.model_dump()})
        conn.execute('''INSERT INTO documents(id,knowledge_base_id,metadata) VALUES(?,?,?)
                     ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata''',
                     (source.source_id, self.knowledge_base_id, source.model_dump_json()))
        conn.execute('INSERT INTO document_versions VALUES(?,?,?,?,?,?,?)',
                     (version.id, version.document_id, version.version_no, version.markdown,
                      version.content_hash, version.builder_version, json.dumps(version.provenance, ensure_ascii=False)))
        for kind, nodes in structure.items():
            for ordinal, node in enumerate(nodes):
                conn.execute('INSERT INTO document_nodes VALUES(?,?,?,?,?)',
                             (node.id, version_id, kind, ordinal, node.model_dump_json()))
                if kind == 'tables':
                    for cell in node.cells:
                        conn.execute('INSERT INTO document_nodes VALUES(?,?,?,?,?)',
                                     (cell.id, version_id, 'cells', ordinal, json.dumps({**cell.model_dump(), 'table_id': node.id})))
        for name, content in artifacts.items():
            digest = hashlib.sha256(content).hexdigest()
            conn.execute('INSERT OR IGNORE INTO document_artifacts VALUES(?,?)', (digest, content))
            conn.execute('INSERT INTO document_version_artifacts VALUES(?,?,?)', (version_id, name, digest))
        conn.execute('UPDATE documents SET current_version_id=? WHERE id=?', (version_id, source.source_id))
        # Historical external IDs are only attached when their version association is known.
        if provenance.get('current_mapping') and source.dify_document_id:
            self.sync_mapping(conn, source, version_id)
        return version_id

    def sync_mapping(self, conn, source, version_id=None):
        row = conn.execute('SELECT current_version_id FROM documents WHERE id=? AND deleted=0', (source.source_id,)).fetchone()
        version_id = version_id or (row[0] if row else None)
        if not version_id or not source.dify_document_id:
            return
        for old in conn.execute('SELECT id,payload FROM external_index_mappings WHERE version_id != ?', (version_id,)).fetchall():
            payload = json.loads(old['payload'])
            if payload.get('adapter') == 'dify' and payload.get('external_document_id') == source.dify_document_id and payload.get('dataset_id') == source.dify_dataset_id:
                payload['status'] = 'superseded'
                conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?', (json.dumps(payload), old['id']))
        mapping = ExternalIndexMapping(id=f'{version_id}:dify', version_id=version_id,
            adapter='dify', index_version=version_id, status=source.dify_status,
            external_document_id=source.dify_document_id, dataset_id=source.dify_dataset_id)
        conn.execute('INSERT OR REPLACE INTO external_index_mappings VALUES(?,?,?)',
                     (mapping.id, version_id, mapping.model_dump_json()))

    def backfill(self, document_id):
        """Per-document atomic replay. Failure leaves all legacy rows/files intact."""
        with document_lock(self.sources.db_path.parent / '.document-operations.lock'):
            self._backfill_locked(document_id)

    def _backfill_locked(self, document_id):
        try:
            with self.sources.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                raw = conn.execute('SELECT * FROM sources WHERE source_id=?', (document_id,)).fetchone()
                if raw is None:
                    raise KeyError(document_id)
                source = self.sources._source(raw)
                existing = conn.execute('SELECT current_version_id FROM documents WHERE id=? AND deleted=0', (document_id,)).fetchone()
                revisions = conn.execute('SELECT * FROM source_revisions WHERE source_id=? ORDER BY version_no', (document_id,)).fetchall()
                legacy_states = [dict(row) for row in conn.execute('SELECT * FROM source_versions WHERE source_id=?', (document_id,))]
                current_path = Path(source.local_md_path)
                current_text = current_path.read_text(encoding='utf-8') if current_path.is_file() and source.rewrite_status != 'preview_ready' else None
                for revision in revisions:
                    is_current = revision == revisions[-1] and revision['markdown'] == current_text
                    self.publish(conn, source, revision['revision_id'], revision['version_no'], revision['markdown'],
                                 {'legacy_revision': dict(revision), 'legacy_states': legacy_states},
                                 self._artifacts(source) if is_current and not existing else {})
                if existing and existing['current_version_id']:
                    # Once migrated, only explicit publication can switch the current version.
                    conn.execute('UPDATE documents SET current_version_id=? WHERE id=?',
                                 (existing['current_version_id'], document_id))
                elif current_path.is_file() and source.rewrite_status != 'preview_ready':
                    markdown = current_path.read_text(encoding='utf-8')
                    digest = hashlib.sha256(markdown.encode()).hexdigest()
                    last = conn.execute('SELECT id,content_hash FROM document_versions WHERE document_id=? ORDER BY version_no DESC LIMIT 1', (document_id,)).fetchone()
                    if not last or last['content_hash'] != digest:
                        number = max([row['version_no'] for row in revisions] + [0]) + 1
                        number = max(number, conn.execute('SELECT COALESCE(MAX(version_no),0)+1 FROM document_versions WHERE document_id=?', (document_id,)).fetchone()[0])
                        vid = f'{document_id}:backfill:{digest[:24]}'
                        self.publish(conn, source, vid, number, markdown,
                                     {'backfill': True, 'legacy_states': legacy_states, 'current_mapping': True}, self._artifacts(source))
                    elif not existing:
                        # Existing revision is a verified exact copy of the current Markdown.
                        for name, content in self._artifacts(source).items():
                            digest_artifact = hashlib.sha256(content).hexdigest()
                            conn.execute('INSERT OR IGNORE INTO document_artifacts VALUES(?,?)', (digest_artifact, content))
                            conn.execute('INSERT OR IGNORE INTO document_version_artifacts VALUES(?,?,?)', (last['id'], name, digest_artifact))
                        self.sync_mapping(conn, source, last['id'])
                if not conn.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone():
                    conn.execute('INSERT INTO documents(id,knowledge_base_id,metadata) VALUES(?,?,?)',
                                 (document_id, self.knowledge_base_id, source.model_dump_json()))
                conn.execute('DELETE FROM document_backfill_errors WHERE document_id=?', (document_id,))
        except Exception as exc:
            if not isinstance(exc, KeyError):
                with self.sources.connect() as conn:
                    conn.execute('INSERT OR REPLACE INTO document_backfill_errors VALUES(?,?)', (document_id, type(exc).__name__))
            raise

    def backfill_all(self):
        results = []
        for source in self.sources.list_all():
            try:
                self.backfill(source.source_id)
                results.append({'id': source.source_id, 'status': 'complete'})
            except Exception as exc:
                results.append({'id': source.source_id, 'status': 'failed', 'error': type(exc).__name__})
        return results

    def document(self, document_id):
        # Sources remain the membership and deletion authority during compatibility period.
        source = self.sources.get(document_id)
        with self.sources.connect() as conn:
            row = conn.execute('SELECT * FROM documents WHERE id=? AND knowledge_base_id=? AND deleted=0',
                               (document_id, self.knowledge_base_id)).fetchone()
        if not row:
            self.backfill(document_id)
            return self.document(document_id)
        return Document(id=document_id, knowledge_base_id=self.knowledge_base_id,
                        current_version_id=row['current_version_id'], metadata=source.model_dump())

    def version(self, document_id, version_id=None):
        doc = self.document(document_id)
        with self.sources.connect() as conn:
            row = conn.execute('SELECT * FROM document_versions WHERE id=? AND document_id=?',
                               (version_id or doc.current_version_id, document_id)).fetchone()
        if row is None:
            raise KeyError(version_id)
        return DocumentVersion(**{**dict(row), 'provenance': json.loads(row['provenance'])})

    def versions(self, document_id):
        self.document(document_id)
        with self.sources.connect() as conn:
            rows = conn.execute('SELECT id FROM document_versions WHERE document_id=? ORDER BY version_no', (document_id,)).fetchall()
        return [self.version(document_id, row[0]) for row in rows]

    def nodes(self, document_id, kind, version_id=None):
        version = self.version(document_id, version_id)
        with self.sources.connect() as conn:
            rows = conn.execute('SELECT payload FROM document_nodes WHERE version_id=? AND kind=? ORDER BY ordinal', (version.id, kind)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def node(self, document_id, node_id, version_id=None):
        version = self.version(document_id, version_id)
        with self.sources.connect() as conn:
            row = conn.execute('SELECT payload FROM document_nodes WHERE id=? AND version_id=?', (node_id, version.id)).fetchone()
        if row is None:
            raise KeyError(node_id)
        return json.loads(row[0])

    def adjacent_chunks(self, document_id, chunk_id, version_id=None, before=1, after=1):
        chunks = self.nodes(document_id, 'chunks', version_id)
        index = next((i for i, chunk in enumerate(chunks) if chunk['id'] == chunk_id), None)
        if index is None:
            raise KeyError(chunk_id)
        return chunks[max(0, index-min(max(before, 0), 20)):index+min(max(after, 0), 20)+1]

    def located_blocks(self, document_id, version_id=None, page=None, section_id=None):
        if section_id:
            section = self.node(document_id, section_id, version_id)
            if 'title' not in section:
                raise KeyError(section_id)
        return [block for block in self.nodes(document_id, 'blocks', version_id)
                if (page is None or page in block['locator']['page_numbers'])
                and (section_id is None or section_id in block['locator']['section_ids'])]

    def mappings(self, document_id, version_id=None):
        version = self.version(document_id, version_id)
        with self.sources.connect() as conn:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM external_index_mappings WHERE version_id=?', (version.id,))]

    def artifacts(self, document_id, version_id=None):
        version = self.version(document_id, version_id)
        with self.sources.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT name,hash FROM document_version_artifacts WHERE version_id=?', (version.id,))]

    def artifact(self, document_id, name, version_id=None):
        version = self.version(document_id, version_id)
        with self.sources.connect() as conn:
            row = conn.execute('''SELECT a.content FROM document_version_artifacts v JOIN document_artifacts a ON a.hash=v.hash
                                  WHERE v.version_id=? AND v.name=?''', (version.id, name)).fetchone()
        if row is None:
            raise KeyError(name)
        return bytes(row[0])

    def iter_chunks(self, document_id, version_id=None):
        """Stage 2 index input: stable version/builder IDs and source locators."""
        yield from self.nodes(document_id, 'chunks', version_id)

    def put_mapping(self, document_id, mapping: ExternalIndexMapping):
        """Record an adapter's acknowledged index generation; does not perform indexing."""
        self.version(document_id, mapping.version_id)
        if mapping.chunk_id:
            chunk = self.node(document_id, mapping.chunk_id, mapping.version_id)
            if 'builder_version' not in chunk:
                raise ValueError('Mapping target must be a Chunk')
        with self.sources.connect() as conn:
            existing = conn.execute('SELECT version_id FROM external_index_mappings WHERE id=?', (mapping.id,)).fetchone()
            if existing and existing[0] != mapping.version_id:
                raise ValueError('Mapping ID belongs to another version')
            conn.execute('INSERT OR REPLACE INTO external_index_mappings VALUES(?,?,?)',
                         (mapping.id, mapping.version_id, mapping.model_dump_json()))

    def index_manifest(self):
        """Current versions and tombstones, for later independent index workers."""
        with self.sources.connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT id,knowledge_base_id,current_version_id,deleted FROM documents WHERE knowledge_base_id=? ORDER BY id',
                (self.knowledge_base_id,))]


class DocumentReadService:
    """All business reads call a live authorization callback, including old versions."""
    READS = {'document', 'version', 'versions', 'nodes', 'node', 'adjacent_chunks',
             'located_blocks', 'mappings', 'artifacts', 'artifact'}

    def __init__(self, repository, authorize):
        self.repository = repository
        self.authorize = authorize

    def read(self, operation, document_id, **kwargs):
        self.authorize(self.repository.knowledge_base_id, document_id)
        if operation not in self.READS:
            raise ValueError('Unknown read operation')
        return getattr(self.repository, operation)(document_id, **kwargs)
