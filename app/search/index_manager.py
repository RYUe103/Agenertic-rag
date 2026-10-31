"""Immutable Tantivy generations, atomic publication and cross-process leases."""
import fcntl
import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from app.chunk_builder import ChunkConfig
from app.document_lock import document_lock
from app.document_models import ExternalIndexMapping
from .snapshot import snapshot, compatible, representation

SCHEMA = '''
CREATE TABLE IF NOT EXISTS lexical_generations(
 id TEXT PRIMARY KEY,status TEXT NOT NULL,manifest TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS lexical_current(singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation_id TEXT);
'''


def checksums(path):
    result = {}
    for file in sorted(path.iterdir()):
        if file.is_file() and not file.name.startswith('.') and file.name != 'checksums.json':
            h = hashlib.sha256()
            with file.open('rb') as handle:
                for block in iter(lambda: handle.read(1024*1024), b''):
                    h.update(block)
            result[file.name] = h.hexdigest()
    return result


def disk_bytes(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.exists() else 0


class IndexManager:
    def __init__(self, repository, retain_history=None):
        self.repo = repository
        self.root = repository.sources.db_path.parent/'tantivy'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = self.root/'.build.lock'
        self.catalog = self.root/'.catalog.lock'
        self.retain_history = max(0, int(retain_history if retain_history is not None
            else os.environ.get('LOCAL_INDEX_RETAIN_HISTORY', '2')))
        self._cache = None
        with self.repo.sources.connect() as conn:
            conn.executescript(SCHEMA)

    def state(self):
        with self.repo.sources.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM lexical_generations ORDER BY rowid DESC LIMIT 30')]

    def current(self):
        with self.repo.sources.connect() as conn:
            row = conn.execute('''SELECT g.* FROM lexical_current c JOIN lexical_generations g ON g.id=c.generation_id
                WHERE c.singleton=1''').fetchone()
            return dict(row) if row else None

    def _snapshot(self):
        with self.repo.sources.connect() as conn:
            conn.execute('BEGIN')
            desired = snapshot(conn, self.repo.knowledge_base_id)
            dirty = dict(conn.execute('SELECT document_id,revision FROM search_changes').fetchall())
        return desired, dirty

    def status(self):
        with document_lock(self.catalog):
            current = self.current()
            desired, dirty = self._snapshot()
            old = json.loads(current['manifest']) if current else {}
            usable = False
            if current:
                try:
                    self._open(current)
                    usable = True
                except (RuntimeError, OSError):
                    pass
            return dict(generation_id=current['id'] if current else None,
                usable=usable,
                stale=old != desired or bool(dirty), pending=len(dirty), retained_history=self.retain_history,
                current_bytes=disk_bytes(self.root/current['id']) if current else 0,
                total_bytes=disk_bytes(self.root),
                generations=[dict(id=r['id'], status=r['status'], error=r['error'],
                    bytes=disk_bytes(self.root/r['id'])) for r in self.state()])

    def _open(self, row):
        from .tantivy_adapter import TantivyAdapter
        path = self.root/row['id']
        manifest = json.loads(row['manifest'])
        if not compatible(manifest, self.repo.knowledge_base_id):
            with self.repo.sources.connect() as conn:
                conn.execute("UPDATE lexical_generations SET status='failed',error='schema/config/tokenizer changed; rebuild required' WHERE id=?", (row['id'],))
            raise RuntimeError('Index schema/config/tokenizer changed; rebuild required')
        try:
            stamp = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns)
                          for p in sorted(path.iterdir()) if not p.name.startswith('.'))
            key = (row['id'], stamp, hashlib.sha256(row['manifest'].encode()).hexdigest())
            cached = self._cache
            if cached and cached[0] == key:
                return cached[1:]
            if checksums(path) != json.loads((path/'checksums.json').read_text()):
                raise ValueError('Index checksum mismatch')
            adapter = TantivyAdapter(path)
        except Exception:
            with self.repo.sources.connect() as conn:
                conn.execute("UPDATE lexical_generations SET status='failed',error='corrupt index; rebuild required' WHERE id=?", (row['id'],))
            raise RuntimeError('Local index corrupt; run rebuild') from None
        opened = (adapter, manifest['documents'], row['id'])
        self._cache = (key, *opened)
        return opened

    @contextmanager
    def reader(self):
        # Close the pointer-read / lease-acquisition / GC race. Lock files are
        # outside generation directories and never unlinked (stable inode).
        with document_lock(self.catalog):
            row = self.current()
            if not row:
                raise RuntimeError('Local index pending; run index sync')
            lease = (self.root/('.lease-'+row['id'])).open('a')
            fcntl.flock(lease, fcntl.LOCK_SH)
        try:
            yield self._open(row)
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()

    def open_current(self):
        """Compatibility loader; hold reader() for the entire query."""
        with self.reader() as opened:
            return opened

    @staticmethod
    def _ack(conn, dirty):
        conn.executemany('DELETE FROM search_changes WHERE document_id=? AND revision=?', list(dirty.items()))

    def _publish(self, conn, gid, desired, dirty):
        if snapshot(conn, self.repo.knowledge_base_id) != desired:
            raise RuntimeError('Sources or dirty revisions changed during build; retry pending')
        self._mappings(conn, gid, desired['documents'])
        conn.execute("UPDATE lexical_generations SET status='superseded' WHERE status='ready'")
        conn.execute("UPDATE lexical_generations SET status='ready' WHERE id=?", (gid,))
        conn.execute('INSERT OR REPLACE INTO lexical_current VALUES(1,?)', (gid,))
        self._ack(conn, dirty)

    def _mappings(self, conn, gid, documents):
        for row in conn.execute('SELECT id,payload FROM external_index_mappings').fetchall():
            payload = json.loads(row['payload'])
            if payload.get('adapter') == 'tantivy':
                version = conn.execute('SELECT d.deleted FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?', (payload['version_id'],)).fetchone()
                payload['status'] = 'deleted' if version and version[0] else 'superseded'
                conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        for spec in documents.values():
            mapping = ExternalIndexMapping(id=f'{spec["version_id"]}:tantivy:{gid}',
                version_id=spec['version_id'], builder_version=spec['builder_version'],
                adapter='tantivy', index_version=gid, status='ready')
            conn.execute('INSERT OR REPLACE INTO external_index_mappings VALUES(?,?,?)',
                         (mapping.id, mapping.version_id, mapping.model_dump_json()))

    def sync(self, full=False, config=None):
        from .tantivy_adapter import TantivyAdapter
        with document_lock(self.lock):
            with self.repo.sources.connect() as conn:
                conn.execute("UPDATE lexical_generations SET status='failed',error='interrupted build; retryable' WHERE status='building'")
            current, previous = self.current(), {}
            if current:
                try:
                    _, previous, _ = self.open_current()
                except (RuntimeError, OSError):
                    full = True
            for doc in self.repo.index_manifest():
                if not doc['deleted'] and doc['current_version_id']:
                    if config or not self.repo.selected_builder(doc['current_version_id']):
                        self.repo.rebuild_chunks(doc['id'], doc['current_version_id'], config or ChunkConfig())
            desired, dirty = self._snapshot()
            docs = desired['documents']
            changed = set(docs) if full else {d for d in docs
                if representation(docs[d]) != representation(previous.get(d, {}))}
            removed = set(previous)-set(docs)
            if current and not full and not changed and not removed:
                with self.repo.sources.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if snapshot(conn, self.repo.knowledge_base_id) != desired:
                        raise RuntimeError('Sources changed; retry pending')
                    conn.execute('UPDATE lexical_generations SET manifest=? WHERE id=?', (json.dumps(desired), current['id']))
                    self._ack(conn, dirty)
                self._cache = None
                self._cleanup_locked()
                return dict(generation_id=current['id'], changed=0, deleted=0, noop=True)
            gid = uuid.uuid4().hex
            path = self.root/gid
            with self.repo.sources.connect() as conn:
                conn.execute('INSERT INTO lexical_generations VALUES(?,?,?,NULL)', (gid, 'building', json.dumps(desired)))
            try:
                if current and not full:
                    shutil.copytree(self.root/current['id'], path, ignore=shutil.ignore_patterns('.*', 'checksums.json'))
                else:
                    path.mkdir()
                adapter = TantivyAdapter(path, create=full or not current)
                adapter.update(self.repo, docs, changed, removed)
                expected = sum(adapter.count_document(d) for d in docs)
                if expected != adapter.count():
                    raise RuntimeError('Index contains unexpected documents')
                for d in changed:
                    count = docs[d]['chunk_count']
                    if adapter.count_document(d) != count:
                        raise RuntimeError('Incomplete index generation')
                (path/'checksums.json').write_text(json.dumps(checksums(path), sort_keys=True))
                for file in path.iterdir():
                    if file.is_file():
                        with file.open('rb') as handle:
                            os.fsync(handle.fileno())
                for folder in (path, self.root):
                    directory = os.open(folder, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                with self.repo.sources.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    self._publish(conn, gid, desired, dirty)
                self._cache = None
            except Exception as exc:
                with self.repo.sources.connect() as conn:
                    conn.execute("""UPDATE lexical_generations SET status='failed',error=?
                        WHERE id=? AND id NOT IN (SELECT generation_id FROM lexical_current)""",
                        (type(exc).__name__+': '+str(exc)[:300], gid))
                raise
            self._cleanup_locked()
            return dict(generation_id=gid, changed=len(changed), deleted=len(removed), noop=False, chunks=expected)

    def rollback(self, generation_id):
        with document_lock(self.lock):
            with self.repo.sources.connect() as conn:
                row = conn.execute("SELECT * FROM lexical_generations WHERE id=? AND status IN ('ready','superseded','retired')",
                                   (generation_id,)).fetchone()
            if not row:
                raise KeyError(generation_id)
            manifest = json.loads(row['manifest'])
            if not compatible(manifest, self.repo.knowledge_base_id):
                raise ValueError('Rollback schema/config/tokenizer incompatible; rebuild')
            if row['status'] == 'retired':
                raise ValueError('Rollback generation retired; metadata/source incompatible or retention expired')
            path = self.root/generation_id
            if checksums(path) != json.loads((path/'checksums.json').read_text()):
                raise RuntimeError('Rollback generation is corrupt')
            with self.repo.sources.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                latest = snapshot(conn, self.repo.knowledge_base_id)
                target, current = manifest['documents'], latest['documents']
                if set(target) != set(current):
                    raise ValueError('Rollback would revive deleted documents or omit sources')
                for d, spec in target.items():
                    if any(spec[k] != current[d][k] for k in ('document_id', 'version_id', 'content_hash', 'metadata')):
                        raise ValueError('Rollback metadata/source version incompatible')
                    if not conn.execute("SELECT 1 FROM chunk_builds WHERE version_id=? AND builder_version=? AND status='ready'",
                                        (spec['version_id'], spec['builder_version'])).fetchone():
                        raise ValueError('Rollback chunk build unavailable')
                    if spec['builder_version'] != current[d]['builder_version']:
                        conn.execute('INSERT OR REPLACE INTO chunk_selection VALUES(?,?)',
                                     (spec['version_id'], spec['builder_version']))
                        self.repo.mark_search_dirty(conn, d)
                verified = snapshot(conn, self.repo.knowledge_base_id)
                dirty = dict(conn.execute('SELECT document_id,revision FROM search_changes').fetchall())
                conn.execute('UPDATE lexical_generations SET manifest=? WHERE id=?', (json.dumps(verified), generation_id))
                self._publish(conn, generation_id, verified, dirty)
            self._cache = None
            return dict(generation_id=generation_id, status='ready')

    def cleanup(self):
        with document_lock(self.lock):
            return self._cleanup_locked()

    def _cleanup_locked(self):
        removed, busy = [], []
        with document_lock(self.catalog):
            current = self.current()
            with self.repo.sources.connect() as conn:
                rows = [dict(r) for r in conn.execute('SELECT * FROM lexical_generations ORDER BY rowid DESC')]
            keep = {current['id']} if current else set()
            latest, _ = self._snapshot()
            live = latest['documents']
            def rollback_candidate(row):
                old = json.loads(row['manifest'])
                docs = old.get('documents',{})
                return (compatible(old, self.repo.knowledge_base_id) and set(docs)==set(live)
                    and all(all(spec[k]==live[d][k] for k in ('document_id','version_id','content_hash','metadata'))
                            for d,spec in docs.items()))
            history = [r['id'] for r in rows if r['id'] not in keep and r['status'] == 'superseded'
                       and rollback_candidate(r)]
            keep.update(history[:self.retain_history])
            for path in self.root.iterdir():
                if not path.is_dir() or path.name in keep:
                    continue
                with (self.root/('.lease-'+path.name)).open('a') as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        busy.append(path.name)
                        continue
                    shutil.rmtree(path)
                    removed.append(path.name)
                    with self.repo.sources.connect() as conn:
                        conn.execute("UPDATE lexical_generations SET status='retired' WHERE id=? AND status='superseded'", (path.name,))
        return dict(removed=removed, busy=busy)
