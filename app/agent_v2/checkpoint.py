"""Official SQLite checkpoints plus ownership, OS locks and call receipts.

The receipt journal is conservative: a crash during a call is uncertain, never
an excuse to replay it. Only read-only V2 tools are admitted by this runtime.
"""
import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from langgraph.checkpoint.sqlite import SqliteSaver


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ExecutionStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'executions.db'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, agent TEXT NOT NULL,
                    scopes TEXT NOT NULL, conversation TEXT NOT NULL, runtime_version TEXT NOT NULL,
                    status TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                    UNIQUE(owner,agent,scopes,conversation));
                CREATE TABLE IF NOT EXISTS calls (
                    thread TEXT NOT NULL, run TEXT NOT NULL, call TEXT NOT NULL,
                    binding TEXT NOT NULL, status TEXT NOT NULL, output TEXT,
                    PRIMARY KEY(thread,run,call));
                CREATE TABLE IF NOT EXISTS approval_bindings (
                    thread TEXT NOT NULL, run TEXT NOT NULL, call TEXT NOT NULL,
                    binding TEXT NOT NULL, approval TEXT NOT NULL,
                    PRIMARY KEY(thread,run,call));
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def thread(self, owner, agent, scopes, conversation, thread_id=None):
        encoded = json.dumps(scopes, sort_keys=True, separators=(',', ':'))
        with self.connect() as db:
            if not thread_id:
                db.execute('INSERT OR IGNORE INTO threads VALUES(?,?,?,?,?,?,?,0,?)',
                           ('thread_' + uuid.uuid4().hex, owner, agent, encoded, conversation,
                            'react_v2.1', 'new', time.time()))
                row = db.execute('SELECT * FROM threads WHERE owner=? AND agent=? AND scopes=? AND conversation=?',
                                 (owner, agent, encoded, conversation)).fetchone()
            else:
                row = db.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
            if not row or (row['owner'], row['agent'], row['scopes']) != (owner, agent, encoded):
                raise PermissionError('thread_scope_mismatch')
            if row['runtime_version'] != 'react_v2.1' or row['status'] == 'archived':
                raise ValueError('thread_not_resumable')
            return dict(row)

    @contextmanager
    def lock(self, thread):
        # flock works across processes and is automatically released on process death.
        path = self.directory / (hashlib.sha256(thread.encode()).hexdigest() + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('thread_busy') from None
            yield
        finally:
            os.close(fd)

    @contextmanager
    def saver(self):
        path = self.directory / 'checkpoints.db'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with SqliteSaver.from_conn_string(str(path)) as saver:
            saver.setup()
            yield saver

    def status(self, thread, status, reset=False):
        with self.connect() as db:
            db.execute('UPDATE threads SET status=?, updated=?, cancelled=CASE WHEN ? THEN 0 ELSE cancelled END WHERE id=?',
                       (status, time.time(), reset, thread))

    def cancel(self, thread):
        with self.connect() as db:
            db.execute('UPDATE threads SET cancelled=1 WHERE id=?', (thread,))

    def cancelled(self, thread):
        with self.connect() as db:
            return bool(db.execute('SELECT cancelled FROM threads WHERE id=?', (thread,)).fetchone()[0])

    def claim(self, state, call, binding):
        key = (state['thread_id'], state['run_id'], call['id'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM calls WHERE thread=? AND run=? AND call=?', key).fetchone()
            if row:
                if row['binding'] != binding:
                    raise ValueError('tool_call_id_reused')
                if row['status'] != 'completed':
                    raise RuntimeError('call_outcome_uncertain')
                return json.loads(row['output'])
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,NULL)', (*key, binding, 'running'))
        return None

    def complete(self, state, call, output):
        with self.connect() as db:
            db.execute('UPDATE calls SET status=?,output=? WHERE thread=? AND run=? AND call=?',
                       ('completed', json.dumps(output, ensure_ascii=False),
                        state['thread_id'], state['run_id'], call['id']))

    def approval(self, state, call, binding, create):
        key = (state['thread_id'], state['run_id'], call['id'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM approval_bindings WHERE thread=? AND run=? AND call=?', key).fetchone()
            if row:
                if row['binding'] != binding:
                    raise PermissionError('approval_context_changed')
                return row['approval']
            ident = create()
            db.execute('INSERT INTO approval_bindings VALUES(?,?,?,?,?)', (*key, binding, ident))
            return ident

    def archive(self, thread):
        with self.lock(thread), self.saver() as saver:
            saver.delete_thread(thread)
            with self.connect() as db:
                for table in ('calls', 'approval_bindings'):
                    db.execute(f'DELETE FROM {table} WHERE thread=?', (thread,))
            self.status(thread, 'archived')
