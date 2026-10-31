from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
import uuid
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

RUNNER = r'''
import base64, json, os, stat, subprocess, sys
language, marker = sys.argv[1:]
payload = json.load(sys.stdin.buffer)
code = payload['code'].encode('utf-8')
docs = payload.get('documents', [])
os.makedirs('/tmp/sdk', exist_ok=True)
with open('/tmp/sdk/documents.json', 'w', encoding='utf-8') as file:
    json.dump(docs, file, ensure_ascii=False)
with open('/tmp/sdk/knowledge_sdk.py', 'w', encoding='utf-8') as file:
    file.write('import json\nfrom pathlib import Path\n_DATA = json.loads(Path("/tmp/sdk/documents.json").read_text(encoding="utf-8"))\ndef list_documents():\n    return [{"source_id": d["source_id"], "filename": d["filename"]} for d in _DATA]\ndef read_document(source_id):\n    for d in _DATA:\n        if d["source_id"] == source_id:\n            return d["markdown"]\n    raise KeyError(source_id)\n')
os.makedirs('/tmp/output', exist_ok=True)
python_runner = "import sys; sys.path.insert(0, '/tmp/sdk'); exec(compile(sys.stdin.read(), '<sandbox>', 'exec'))"
result = subprocess.run([sys.executable, '-I', '-c', python_runner] if language == 'python' else ['/bin/sh', '-s'], input=code)
files = []
total = 0
for root, dirs, names in os.walk('/tmp/output', followlinks=False):
    dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
    for name in sorted(names):
        path = os.path.join(root, name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024 - total or len(files) >= 20:
                    continue
                data = file.read(2 * 1024 * 1024 - total + 1)
                if len(data) > 2 * 1024 * 1024 - total:
                    continue
            total += len(data)
            files.append({'name': os.path.relpath(path, '/tmp/output'), 'data': base64.b64encode(data).decode('ascii')})
        except OSError:
            continue
sys.stdout.flush()
sys.stdout.write('\n' + marker + json.dumps(files, ensure_ascii=True, separators=(',', ':')) + '\n')
sys.stdout.flush()
sys.exit(result.returncode)
'''


class SandboxPolicy(BaseModel):
    enabled: bool = False
    allow_users: bool = False
    allow_python: bool = True
    allow_shell: bool = False
    allow_documents: bool = False
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    memory_mb: int = Field(default=128, ge=64, le=1024)
    cpu: float = Field(default=0.5, ge=0.1, le=2)
    workspace_mb: int = Field(default=16, ge=1, le=128)


class SandboxRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    language: Literal['python', 'shell']
    code: str = Field(min_length=1, max_length=32768)
    agent_id: str = Field(default='', max_length=200)
    source_ids: list[str] = Field(default_factory=list, max_length=10)


class SandboxStore:
    """Docker sandbox settings and private execution records."""

    def __init__(self, path: Path):
        self.path = path
        self.slots = threading.BoundedSemaphore(2)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                name TEXT NOT NULL, language TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL,
                stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '', duration_ms INTEGER, container_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                name TEXT NOT NULL, content BLOB NOT NULL);''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(runs)').fetchall()}
            if 'resource_usage' not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN resource_usage TEXT NOT NULL DEFAULT '{}' ")
            if 'container_name' not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN container_name TEXT NOT NULL DEFAULT ''")
            running = db.execute("SELECT id,container_name FROM runs WHERE status='running'").fetchall()
            for row in running:
                container = str(row['container_name'] or '')
                recovered = False
                if container:
                    try:
                        inspected = subprocess.run(['docker', 'inspect', container], capture_output=True, timeout=5, check=False)
                        if inspected.returncode == 0:
                            subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=10, check=False)
                            recovered = True
                    except (OSError, subprocess.SubprocessError):
                        pass
                status = 'failed' if recovered else 'unknown'
                message = '服务重启后已清理遗留 Sandbox 容器，执行结果不可恢复' if recovered else '服务重启，容器执行结果未知，请重新提交'
                db.execute("UPDATE runs SET status=?,stderr=?,resource_usage=? WHERE id=?", (status, message, json.dumps({'status': 'not_collected'}), row['id']))

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def policy(self) -> SandboxPolicy:
        with self.connect() as db:
            row = db.execute('SELECT value FROM policy WHERE id=1').fetchone()
        return SandboxPolicy.model_validate_json(row['value']) if row else SandboxPolicy()

    def set_policy(self, policy: SandboxPolicy) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO policy(id,value) VALUES(1,?)', (policy.model_dump_json(),))

    def list(self, owner: str | None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT id,owner,name,language,status,duration_ms,created_at FROM runs ' +
                              ('WHERE owner=? ' if owner is not None else '') + 'ORDER BY created_at DESC LIMIT 100',
                              (owner,) if owner is not None else ()).fetchall()
        return [dict(row) for row in rows]

    def resource_summary(self) -> dict:
        with self.connect() as db:
            rows = db.execute('SELECT resource_usage FROM runs WHERE resource_usage IS NOT NULL ORDER BY created_at DESC LIMIT 100').fetchall()
        values = []
        for row in rows:
            try:
                value = json.loads(row[0] or '{}')
                if value.get('status') in {'collected', 'partial'}:
                    values.append(value)
            except (TypeError, json.JSONDecodeError):
                continue
        if not values:
            return {'status': 'not_collected', 'message': '尚未采集容器 CPU、内存和磁盘用量'}
        return {'status': 'collected', 'samples': len(values),
                'cpu_percent_peak': max(item.get('cpu_percent_peak', 0) for item in values),
                'memory_bytes_peak': max(item.get('memory_bytes_peak', 0) for item in values),
                'pids_peak': max(item.get('pids_peak', 0) for item in values)}

    def get(self, run_id: str, owner: str | None) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?' + (' AND owner=?' if owner is not None else ''),
                             (run_id, owner) if owner is not None else (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        try:
            result['resource_usage'] = json.loads(result.get('resource_usage') or '{}')
        except (TypeError, json.JSONDecodeError):
            result['resource_usage'] = {'status': 'not_collected'}
        with self.connect() as db:
            result['artifacts'] = [dict(item) for item in db.execute(
                'SELECT id,name,length(content) AS size FROM artifacts WHERE run_id=? ORDER BY name', (run_id,)).fetchall()]
        return result

    def artifact(self, run_id: str, artifact_id: str, owner: str | None) -> tuple[str, bytes]:
        self.get(run_id, owner)
        with self.connect() as db:
            row = db.execute('SELECT name,content FROM artifacts WHERE run_id=? AND id=?', (run_id, artifact_id)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return row['name'], bytes(row['content'])

    def authorize(self, request: SandboxRequest, is_admin: bool) -> None:
        policy = self.policy()
        if not policy.enabled or (not is_admin and not policy.allow_users) or not getattr(policy, 'allow_' + request.language):
            raise PermissionError('当前策略不允许执行此语言')
        if request.source_ids and not policy.allow_documents:
            raise PermissionError('当前策略不允许读取文档')

    def run(self, owner: str, request: SandboxRequest, *, is_admin: bool = False, documents: list[dict] | None = None) -> dict:
        self.authorize(request, is_admin)
        policy = self.policy()
        if not self.slots.acquire(blocking=False):
            raise RuntimeError('Sandbox 正忙，请稍后重试')
        run_id = uuid.uuid4().hex
        container = 'rag-sandbox-' + run_id
        created = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        try:
            with self.connect() as db:
                db.execute('INSERT INTO runs(id,owner,name,language,code,status,container_name,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           (run_id, owner, request.name, request.language, request.code, 'running', container, created))
            command = ['docker', 'run', '--rm', '-i', '--name', container, '--pull=never',
                       '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                       '--pids-limit=64', '--memory', f'{policy.memory_mb}m', '--memory-swap', f'{policy.memory_mb}m',
                       '--cpus', str(policy.cpu), '--user', '65534:65534', '--tmpfs',
                       f'/tmp:rw,noexec,nosuid,nodev,size={policy.workspace_mb}m,mode=1777',
                       '--workdir', '/tmp', '--log-driver=none', 'python:3.11-slim']
            marker = 'RAG_ARTIFACTS_' + uuid.uuid4().hex + ':'
            command += ['python', '-I', '-c', RUNNER, request.language, marker]
            try:
                payload = json.dumps({'code': request.code, 'documents': documents or []}, ensure_ascii=False)
                execution = self._execute(command, container, payload, policy.timeout_seconds, marker)
                if len(execution) == 4:
                    stdout, stderr, status, artifacts = execution
                    resources = {'status': 'not_collected'}
                else:
                    stdout, stderr, status, artifacts, resources = execution
            except Exception:
                with self.connect() as db:
                    db.execute("UPDATE runs SET status='failed',stderr=? WHERE id=?", ('执行器发生内部错误，请查看服务日志', run_id))
                raise
            duration = int((time.monotonic() - started) * 1000)
            with self.connect() as db:
                db.execute('UPDATE runs SET status=?,stdout=?,stderr=?,duration_ms=?,resource_usage=? WHERE id=?',
                           (status, stdout, stderr, duration, json.dumps(resources, ensure_ascii=False), run_id))
                db.executemany('INSERT INTO artifacts(id,run_id,name,content) VALUES(?,?,?,?)',
                               [(uuid.uuid4().hex, run_id, name, data) for name, data in artifacts])
            return self.get(run_id, owner)
        finally:
            self.slots.release()

    @staticmethod
    def _execute(command: list[str], container: str, code: str, timeout: int, marker: str) -> tuple[str, str, str, list[tuple[str, bytes]], dict]:
        outputs: list[bytearray] = [bytearray(), bytearray()]
        tail = bytearray()
        truncated = [False, False]
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            return '', f'无法启动 Docker: {exc}', 'failed', [], {'status': 'not_collected'}

        usage = {'status': 'collected', 'cpu_percent_peak': 0.0, 'memory_bytes_peak': 0,
                 'memory_limit_bytes': 0, 'pids_peak': 0}
        stop_stats = threading.Event()
        def collect_stats() -> None:
            while not stop_stats.wait(0.5):
                try:
                    result = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', container],
                                            capture_output=True, text=True, timeout=5, check=False)
                    if result.returncode != 0 or not result.stdout.strip():
                        continue
                    value = json.loads(result.stdout.strip().splitlines()[-1])
                    usage['cpu_percent_peak'] = max(usage['cpu_percent_peak'], float(str(value.get('CPUPerc', '0')).rstrip('%') or 0))
                    memory = str(value.get('MemUsage', '0B / 0B')).split('/', 1)
                    usage['memory_bytes_peak'] = max(usage['memory_bytes_peak'], _bytes(memory[0]))
                    usage['memory_limit_bytes'] = max(usage['memory_limit_bytes'], _bytes(memory[1]))
                    usage['pids_peak'] = max(usage['pids_peak'], int(float(str(value.get('PIDs', '0')).split('/')[0] or 0)))
                except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                    usage['status'] = 'partial'
        def _bytes(value: str) -> int:
            value = value.strip().upper().replace('IB', 'B')
            units = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}
            for unit, factor in units.items():
                if value.endswith(unit):
                    return int(float(value[:-len(unit)].strip()) * factor)
            return 0
        stats_thread = threading.Thread(target=collect_stats, daemon=True)
        stats_thread.start()

        def read(stream, index: int) -> None:
            while chunk := stream.read(8192):
                remaining = max(0, 65536 - len(outputs[index]))
                outputs[index].extend(chunk[:remaining])
                truncated[index] |= len(chunk) > remaining
                if index == 0:
                    tail.extend(chunk)
                    if len(tail) > 3 * 1024 * 1024:
                        del tail[:len(tail) - 3 * 1024 * 1024]

        def write() -> None:
            try:
                process.stdin.write(code.encode('utf-8'))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        readers = [threading.Thread(target=read, args=(process.stdout, 0), daemon=True),
                   threading.Thread(target=read, args=(process.stderr, 1), daemon=True)]
        for thread in readers:
            thread.start()
        threading.Thread(target=write, daemon=True).start()
        try:
            exit_code = process.wait(timeout=timeout)
            status = 'success' if exit_code == 0 else 'failed'
        except subprocess.TimeoutExpired:
            status = 'timeout'
            try:
                subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                process.kill()
                process.wait()
        stop_stats.set()
        stats_thread.join(timeout=2)
        for thread in readers:
            thread.join(timeout=2)
        artifacts: list[tuple[str, bytes]] = []
        marker_bytes = b'\n' + marker.encode('ascii')
        location = tail.rfind(marker_bytes)
        if location >= 0 and status != 'timeout':
            try:
                payload = json.loads(bytes(tail[location + len(marker_bytes):]).strip())
                total = 0
                for item in payload[:20]:
                    name, content = item['name'], base64.b64decode(item['data'], validate=True)
                    total += len(content)
                    if not isinstance(name, str) or not name or len(name) > 500 or total > 2 * 1024 * 1024:
                        raise ValueError('invalid artifact')
                    artifacts.append((name, content))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                artifacts = []
            position = outputs[0].find(marker_bytes)
            if position >= 0:
                del outputs[0][position:]
                truncated[0] = False
        decoded = [value.decode('utf-8', errors='replace') + ('\n[输出已截断]' if truncated[index] else '')
                   for index, value in enumerate(outputs)]
        return decoded[0], decoded[1], status, artifacts, usage
