"""Repository extension for immutable retrieval builds and indexed structure reads."""
import json
from dataclasses import asdict
from .chunk_builder import ChunkConfig, build_chunks
from .document_lock import document_lock


CHUNK_SCHEMA = '''
CREATE TABLE IF NOT EXISTS chunk_builds(
 version_id TEXT NOT NULL,builder_version TEXT NOT NULL,config TEXT NOT NULL,
 status TEXT NOT NULL,PRIMARY KEY(version_id,builder_version));
CREATE TABLE IF NOT EXISTS retrieval_chunks(
 id TEXT PRIMARY KEY,version_id TEXT NOT NULL,builder_version TEXT NOT NULL,
 ordinal INTEGER NOT NULL,start INTEGER,end INTEGER,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS retrieval_chunk_range ON retrieval_chunks(version_id,builder_version,ordinal);
CREATE INDEX IF NOT EXISTS retrieval_chunk_offsets ON retrieval_chunks(version_id,builder_version,start,end);
CREATE TABLE IF NOT EXISTS chunk_selection(version_id TEXT PRIMARY KEY,builder_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS node_locations(version_id TEXT,node_id TEXT,kind TEXT,value TEXT,
 PRIMARY KEY(node_id,kind,value));
CREATE INDEX IF NOT EXISTS node_location_lookup ON node_locations(version_id,kind,value,node_id);
CREATE TABLE IF NOT EXISTS search_changes(document_id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS search_revisions(document_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
INSERT OR IGNORE INTO search_revisions SELECT document_id,revision FROM search_changes;
CREATE TABLE IF NOT EXISTS document_deletions(document_id TEXT PRIMARY KEY,source_json TEXT NOT NULL,status TEXT NOT NULL);
'''


class ChunkBuildRepository:
    def init_chunk_schema(self, conn):
        conn.executescript(CHUNK_SCHEMA)
        if not conn.execute('SELECT 1 FROM document_schema WHERE version=2').fetchone():
            for row in conn.execute("SELECT id,version_id,payload FROM document_nodes WHERE kind='blocks'").fetchall():
                self.store_locations(conn, row['id'], row['version_id'], json.loads(row['payload'])['locator'])
            conn.execute('INSERT OR IGNORE INTO document_schema VALUES(2)')

    @staticmethod
    def store_locations(conn, node_id, version_id, locator):
        for kind in ('page_numbers', 'section_ids'):
            conn.executemany('INSERT OR IGNORE INTO node_locations VALUES(?,?,?,?)',
                [(version_id, node_id, kind, str(v)) for v in locator.get(kind, [])])

    @staticmethod
    def mark_search_dirty(conn, document_id):
        conn.execute('''INSERT INTO search_revisions VALUES(?,1)
            ON CONFLICT(document_id) DO UPDATE SET revision=revision+1''', (document_id,))
        conn.execute('''INSERT OR REPLACE INTO search_changes
            SELECT document_id,revision FROM search_revisions WHERE document_id=?''', (document_id,))

    def rebuild_chunks(self, document_id, version_id=None, config=ChunkConfig(), activate=True):
        with document_lock(self.sources.db_path.parent/'.document-operations.lock'):
            version = self.version(document_id, version_id)
            with self.sources.connect() as conn:
                known = conn.execute('SELECT status FROM chunk_builds WHERE version_id=? AND builder_version=?',
                                     (version.id, config.version)).fetchone()
            if not known:
                chunks = build_chunks(version.markdown, version.id, self.nodes(document_id, 'blocks', version.id),
                    self.nodes(document_id, 'sections', version.id), self.nodes(document_id, 'tables', version.id), config)
                with self.sources.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute('INSERT INTO chunk_builds VALUES(?,?,?,?)',
                                 (version.id, config.version, json.dumps(asdict(config)), 'ready'))
                    conn.executemany('INSERT INTO retrieval_chunks VALUES(?,?,?,?,?,?,?)',
                        [(c['id'],version.id,config.version,c['ordinal'],c['locator']['start'],c['locator']['end'],
                          json.dumps(c,ensure_ascii=False)) for c in chunks])
            if activate:
                self.select_chunk_build(document_id, version.id, config.version)
            return config.version

    def select_chunk_build(self, document_id, version_id, builder_version):
        self.version(document_id, version_id)
        with self.sources.connect() as conn:
            if not conn.execute("SELECT 1 FROM chunk_builds WHERE version_id=? AND builder_version=? AND status='ready'",
                                (version_id,builder_version)).fetchone():
                raise KeyError(builder_version)
            old = conn.execute('SELECT builder_version FROM chunk_selection WHERE version_id=?', (version_id,)).fetchone()
            if not old or old[0] != builder_version:
                conn.execute('INSERT OR REPLACE INTO chunk_selection VALUES(?,?)', (version_id,builder_version))
                self.mark_search_dirty(conn, document_id)

    def selected_builder(self, version_id):
        with self.sources.connect() as conn:
            row = conn.execute('SELECT builder_version FROM chunk_selection WHERE version_id=?', (version_id,)).fetchone()
            return row[0] if row else None

    def chunks_page(self, document_id, version_id=None, builder_version=None, after=-1, limit=100):
        vid = self.resolve_version_id(document_id, version_id)
        builder_version = builder_version or self.selected_builder(vid)
        with self.sources.connect() as conn:
            if builder_version:
                rows = conn.execute('''SELECT payload FROM retrieval_chunks WHERE version_id=? AND builder_version=?
                    AND ordinal>? ORDER BY ordinal LIMIT ?''', (vid,builder_version,after,min(max(limit,1),200)))
            else:
                rows = conn.execute("SELECT payload FROM document_nodes WHERE version_id=? AND kind='chunks' AND ordinal>? ORDER BY ordinal LIMIT ?",
                                    (vid,after,min(max(limit,1),200)))
            return [json.loads(r[0]) for r in rows]

    def chunks_by_ids(self, document_id, ids, version_id=None):
        if len(ids) > 200:
            raise ValueError('At most 200 chunks per request')
        vid = self.resolve_version_id(document_id, version_id)
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        with self.sources.connect() as conn:
            rows = conn.execute(f'SELECT payload FROM retrieval_chunks WHERE version_id=? AND id IN ({marks})', (vid,*ids)).fetchall()
            rows += conn.execute(f"SELECT payload FROM document_nodes WHERE version_id=? AND kind='chunks' AND id IN ({marks})", (vid,*ids)).fetchall()
        found = {json.loads(r[0])['id']:json.loads(r[0]) for r in rows}
        return [found[i] for i in ids if i in found]
