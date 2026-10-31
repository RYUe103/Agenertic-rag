from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


# Only explicitly named routes are recorded; request bodies and query strings
# may contain credentials or document contents and are never persisted.
ROUTES: list[tuple[str, re.Pattern[str], str, str]] = [
    ("POST", re.compile(r"^/api/auth/login$"), "login", "session"),
    ("POST", re.compile(r"^/api/auth/logout$"), "logout", "session"),
    ("POST", re.compile(r"^/v1/chat/completions$"), "invoke", "openai_api"),
    ("POST", re.compile(r"^/api/admin/knowledge-bases$"), "create", "knowledge_base"),
    ("PUT", re.compile(r"^/api/admin/knowledge-bases/([a-z0-9_-]+)$"), "update", "knowledge_base"),
    ("DELETE", re.compile(r"^/api/admin/knowledge-bases/([a-z0-9_-]+)$"), "delete", "knowledge_base"),
    ("GET", re.compile(r"^/api/agents/[^/]+/sources/(src_[a-zA-Z0-9_-]+)(?:/markdown|/segments|/outline)?$"), "read", "document"),
    ("GET", re.compile(r"^/api/files/([a-zA-Z0-9_-]+)$"), "read", "document"),
    ("GET", re.compile(r"^/api/agents/[^/]+/sources/page$"), "read", "document"),
    ("GET", re.compile(r"^/api/agents/[^/]+/sources/stats$"), "read", "document"),
    ("DELETE", re.compile(r"^/api/agents/[^/]+/sources/([a-zA-Z0-9_-]+)$"), "delete", "document"),
    ("POST", re.compile(r"^/api/agents/[^/]+/sources/[^/]+/(?:retry|reparse|refresh-dify-status|rewrite-preview|confirm-rewrite|use-original)$"), "update", "document"),
    ("PUT", re.compile(r"^/api/agents/[^/]+/sources/[^/]+/metadata$"), "update", "document"),
    ("POST", re.compile(r"^/api/agents/[^/]+/sources/(?:extract-metadata-batch|delete-all)$"), "update", "document"),
    ("POST", re.compile(r"^/api/agents/[^/]+/(?:ingest-jobs|reparse-jobs|reindex-jobs|sources/delete-all|sources/delete-batch)$"), "create", "document_task"),
    ("POST", re.compile(r"^/api/jobs/([a-zA-Z0-9_-]+)/(?:start|pause|resume|retry-failed|cancel)$"), "update", "document_task"),
    ("POST", re.compile(r"^/api/jobs/[a-zA-Z0-9_-]+/upload-batch$"), "create", "document"),
    ("POST", re.compile(r"^/api/delete-jobs/([a-zA-Z0-9_-]+)/cancel$"), "update", "document_task"),
    ("PUT", re.compile(r"^/api/admin/sandbox/policy$"), "update", "sandbox_policy"),
    ("POST", re.compile(r"^/api/sandbox/runs$"), "invoke", "sandbox"),
    ("GET", re.compile(r"^/api/sandbox/runs/[^/]+(?:/artifacts/[^/]+)?$"), "read", "sandbox"),
    ("POST", re.compile(r"^/api/knowledge-bases/([a-z0-9_-]+)/tools/[a-z0-9_]+/test$"), "invoke", "tool"),
    ("POST", re.compile(r"^/api/knowledge-bases/([a-z0-9_-]+)/retrieval-test$"), "read", "knowledge_base"),
    ("POST", re.compile(r"^/api/integrations$"), "create", "integration"),
    ("PUT", re.compile(r"^/api/integrations/([a-zA-Z0-9_-]+)/permissions$"), "permissions", "integration"),
    ("POST", re.compile(r"^/api/integrations/([a-zA-Z0-9_-]+)/rotate$"), "rotate", "integration"),
    ("POST", re.compile(r"^/api/integrations/([a-zA-Z0-9_-]+)/revoke$"), "revoke", "integration"),
    ("POST", re.compile(r"^/api/admin/(?:agents|model-connections)$"), "create", "configuration"),
    ("PUT", re.compile(r"^/api/admin/(?:agents|model-connections)/([a-z0-9_-]+)$"), "update", "configuration"),
    ("DELETE", re.compile(r"^/api/admin/(?:agents|model-connections)/([a-z0-9_-]+)$"), "delete", "configuration"),
    ("PUT", re.compile(r"^/api/admin/access/(?:roles|user-role)$"), "permissions", "access"),
    ("PUT", re.compile(r"^/api/admin/tools/([a-z0-9_]+)$"), "update", "tool"),
    ("PUT", re.compile(r"^/api/admin/ldap$"), "update", "ldap"),
    ("POST", re.compile(r"^/api/admin/ldap/test$"), "test", "ldap"),
    ("POST", re.compile(r"^/api/admin/organizations$"), "create", "organization"),
    ("PUT", re.compile(r"^/api/admin/organizations/members$"), "permissions", "organization"),
    ("DELETE", re.compile(r"^/api/admin/organizations/([a-z0-9_-]+)/members/[^/]+$"), "permissions", "organization"),
]


def classify(method: str, path: str) -> tuple[str, str, str] | None:
    for expected_method, pattern, action, resource_type in ROUTES:
        match = pattern.fullmatch(path)
        if method == expected_method and match:
            return action, resource_type, match.group(1) if match.lastindex else ""
    return None


class AuditStore:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL, status_code INTEGER NOT NULL
            ); CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_events_actor ON audit_events(actor, id DESC);""")

    def record(self, actor: str, action: str, resource_type: str, resource_id: str, status_code: int) -> None:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute("INSERT INTO audit_events(created_at,actor,action,resource_type,resource_id,status_code) VALUES(?,?,?,?,?,?)",
                       (datetime.now(timezone.utc).isoformat(), actor, action, resource_type, resource_id, status_code))

    def prune(self, before: str) -> int:
        with sqlite3.connect(self.path, timeout=15) as db:
            result = db.execute("DELETE FROM audit_events WHERE created_at<?", (before,))
            return int(result.rowcount)

    def page(self, page: int, size: int, actor: str, action: str, resource_type: str, start: str, end: str) -> dict:
        clauses, values = [], []
        for column, value, operator in (("actor", actor, "="), ("action", action, "="),
                                        ("resource_type", resource_type, "="), ("created_at", start, ">="), ("created_at", end, "<=")):
            if value:
                clauses.append(f"{column} {operator} ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            total = db.execute("SELECT COUNT(*) FROM audit_events" + where, values).fetchone()[0]
            rows = db.execute("SELECT * FROM audit_events" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                              [*values, size, (page - 1) * size]).fetchall()
        return {"items": [dict(row) for row in rows], "page": page, "page_size": size, "total": total}
