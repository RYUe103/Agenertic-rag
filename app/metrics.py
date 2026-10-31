from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MetricsStore:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, path TEXT NOT NULL,
                status_code INTEGER NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_metrics_created ON requests(created_at DESC)')

    def observe(self, method: str, path: str, status_code: int, duration_ms: int) -> None:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('INSERT INTO requests(method,path,status_code,duration_ms,created_at) VALUES(?,?,?,?,?)',
                       (method, path[:300], status_code, duration_ms, datetime.now(timezone.utc).isoformat()))

    def summary(self) -> dict:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            total = db.execute('SELECT COUNT(*) AS n FROM requests').fetchone()['n']
            rows = db.execute('''SELECT method,path,COUNT(*) AS calls,AVG(duration_ms) AS avg_ms,
                SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS failures,
                MAX(created_at) AS last_called_at FROM requests GROUP BY method,path ORDER BY calls DESC LIMIT 100''').fetchall()
        return {'request_count': total, 'items': [dict(row) for row in rows],
                'sandbox_resources': {'status': 'not_collected', 'message': '尚未采集容器 CPU、内存和磁盘用量'}}
