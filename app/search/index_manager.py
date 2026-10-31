"""Copy-on-write Tantivy generations, SQLite publication pointer and durable dirty outbox."""
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from app.chunk_builder import ChunkConfig
from app.document_lock import document_lock
from app.document_models import ExternalIndexMapping
from .tokenization import TOKENIZER_VERSION

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
                for block in iter(lambda: handle.read(1024*1024),b''):
                    h.update(block)
            result[file.name] = h.hexdigest()
    return result


class IndexManager:
    def __init__(self, repository):
        self.repo = repository
        self.root = repository.sources.db_path.parent/'tantivy'
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock = self.root/'.build.lock'
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

    def open_current(self):
        from .tantivy_adapter import TantivyAdapter
        row = self.current()
        if not row:
            raise RuntimeError('Local index pending; run index sync')
        path = self.root/row['id']
        # Check metadata stat each query; checksum all files on load/restart.
        try:
            stamp = tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(path.iterdir()) if not p.name.startswith('.'))
            key = (row['id'],stamp)
            if self._cache and self._cache[0] == key:
                return self._cache[1:]
            expected = json.loads((path/'checksums.json').read_text())
            if checksums(path) != expected:
                raise ValueError('Index checksum mismatch')
            adapter = TantivyAdapter(path)
        except Exception:
            with self.repo.sources.connect() as conn:
                conn.execute("UPDATE lexical_generations SET status='failed',error='corrupt index; rebuild required' WHERE id=?",(row['id'],))
            raise RuntimeError('Local index corrupt; run rebuild') from None
        manifest = json.loads(row['manifest'])
        if any(spec.get('tokenizer')!=TOKENIZER_VERSION for spec in manifest.values()):
            with self.repo.sources.connect() as conn:
                conn.execute("UPDATE lexical_generations SET status='failed',error='tokenizer changed; rebuild required' WHERE id=?",(row['id'],))
            raise RuntimeError('Tokenizer changed; rebuild required')
        self._cache = (key,adapter,manifest,row['id'])
        return adapter,manifest,row['id']

    def sync(self, full=False, config=None):
        from .tantivy_adapter import TantivyAdapter
        with document_lock(self.lock):
            with self.repo.sources.connect() as conn:
                conn.execute("UPDATE lexical_generations SET status='failed',error='interrupted build; retryable' WHERE status='building'")
            current = self.current()
            previous = {}
            if current:
                try:
                    _, previous, _ = self.open_current()
                except (RuntimeError,OSError):
                    full = True
            # Short source lock only snapshots immutable version/build pairs, not Tantivy IO.
            with document_lock(self.repo.sources.db_path.parent/'.document-operations.lock'):
                desired = {}
                for doc in self.repo.index_manifest():
                    if doc['deleted'] or not doc['current_version_id']:
                        continue
                    try:
                        source = self.repo.sources.get(doc['id'])
                    except KeyError:
                        continue
                    vid = doc['current_version_id']
                    builder = self.repo.selected_builder(vid)
                    if config or not builder:
                        builder = self.repo.rebuild_chunks(doc['id'],vid,config or ChunkConfig())
                    metadata = source.model_dump(mode='json')
                    desired[doc['id']] = dict(version_id=vid,builder_version=builder,
                        tokenizer=TOKENIZER_VERSION,metadata=metadata)
                with self.repo.sources.connect() as conn:
                    dirty = dict(conn.execute('SELECT document_id,revision FROM search_changes').fetchall())
            changed = set(desired) if full else {d for d in desired if desired[d] != previous.get(d)}
            removed = set(previous)-set(desired)
            if current and not full and not changed and not removed:
                self._ack(dirty)
                return {'generation_id':current['id'],'changed':0,'deleted':0,'noop':True}
            gid = uuid.uuid4().hex
            path = self.root/gid
            with self.repo.sources.connect() as conn:
                conn.execute('INSERT INTO lexical_generations VALUES(?,?,?,NULL)',(gid,'building',json.dumps(desired)))
            try:
                if current and not full:
                    shutil.copytree(self.root/current['id'],path,ignore=shutil.ignore_patterns('.*','checksums.json'))
                else:
                    path.mkdir()
                adapter = TantivyAdapter(path,create=full or not current)
                adapter.update(self.repo, desired, changed, removed)
                expected = sum(adapter.count_document(d) for d in desired)
                if expected != adapter.count():
                    raise RuntimeError('Index contains unexpected documents')
                for d in changed:
                    count = sum(1 for _ in self.repo.iter_chunks(d,desired[d]['version_id'],desired[d]['builder_version']))
                    if adapter.count_document(d) != count:
                        raise RuntimeError('Incomplete index generation')
                (path/'checksums.json').write_text(json.dumps(checksums(path),sort_keys=True))
                for file in path.iterdir():
                    if file.is_file():
                        with file.open('rb') as handle:
                            os.fsync(handle.fileno())
                # Refuse publication if any source/build selection changed during IO.
                with document_lock(self.repo.sources.db_path.parent/'.document-operations.lock'):
                    latest = {d['id']:d for d in self.repo.index_manifest() if not d['deleted'] and d['current_version_id']}
                    if set(latest) != set(desired) or any(latest[d]['current_version_id'] != v['version_id'] or
                        self.repo.selected_builder(v['version_id']) != v['builder_version'] for d,v in desired.items()):
                        raise RuntimeError('Sources changed during build; retry pending')
                    with self.repo.sources.connect() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        for row in conn.execute('SELECT id,payload FROM external_index_mappings').fetchall():
                            payload=json.loads(row['payload'])
                            if payload.get('adapter')=='tantivy':
                                version=conn.execute('SELECT d.deleted FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?',(payload['version_id'],)).fetchone()
                                payload['status']='deleted' if version and version[0] else 'superseded'
                                conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
                        for doc,spec in desired.items():
                            mapping=ExternalIndexMapping(id=f'{spec["version_id"]}:tantivy:{gid}',
                                version_id=spec['version_id'],builder_version=spec['builder_version'],
                                adapter='tantivy',index_version=gid,status='ready')
                            conn.execute('INSERT OR REPLACE INTO external_index_mappings VALUES(?,?,?)',
                                (mapping.id,mapping.version_id,mapping.model_dump_json()))
                        conn.execute("UPDATE lexical_generations SET status='superseded' WHERE status='ready'")
                        conn.execute("UPDATE lexical_generations SET status='ready' WHERE id=?",(gid,))
                        conn.execute('INSERT OR REPLACE INTO lexical_current VALUES(1,?)',(gid,))
                    self._ack(dirty)
                self._cache = None
                return {'generation_id':gid,'changed':len(changed),'deleted':len(removed),'noop':False,'chunks':expected}
            except Exception as exc:
                with self.repo.sources.connect() as conn:
                    conn.execute("UPDATE lexical_generations SET status='failed',error=? WHERE id=?",(type(exc).__name__,gid))
                raise

    def _ack(self, dirty):
        with self.repo.sources.connect() as conn:
            conn.executemany('DELETE FROM search_changes WHERE document_id=? AND revision=?',list(dirty.items()))

    def rollback(self, generation_id):
        """Rollback derived index/build selection only, never resurrect document content."""
        with document_lock(self.lock), document_lock(self.repo.sources.db_path.parent/'.document-operations.lock'):
            with self.repo.sources.connect() as conn:
                row=conn.execute("SELECT * FROM lexical_generations WHERE id=? AND status IN ('ready','superseded')",(generation_id,)).fetchone()
            if not row:
                raise KeyError(generation_id)
            path=self.root/generation_id
            if checksums(path)!=json.loads((path/'checksums.json').read_text()):
                raise RuntimeError('Rollback generation is corrupt')
            manifest=json.loads(row['manifest'])
            current={d['id']:d for d in self.repo.index_manifest() if not d['deleted'] and d['current_version_id']}
            if set(manifest)!=set(current) or any(current[d]['current_version_id']!=s['version_id'] for d,s in manifest.items()):
                raise ValueError('Rollback would change source versions or revive deleted documents; rebuild instead')
            with self.repo.sources.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                for spec in manifest.values():
                    conn.execute('INSERT OR REPLACE INTO chunk_selection VALUES(?,?)',(spec['version_id'],spec['builder_version']))
                conn.execute("UPDATE lexical_generations SET status='superseded' WHERE status='ready'")
                conn.execute("UPDATE lexical_generations SET status='ready' WHERE id=?",(generation_id,))
                conn.execute('INSERT OR REPLACE INTO lexical_current VALUES(1,?)',(generation_id,))
                for row in conn.execute('SELECT id,payload FROM external_index_mappings').fetchall():
                    payload=json.loads(row['payload'])
                    if payload.get('adapter')=='tantivy':
                        payload['status']='ready' if payload['index_version']==generation_id else 'superseded'
                        conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
            self._cache=None
            return {'generation_id':generation_id,'status':'ready'}
