from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field


class IntegrationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    client_type: Literal["opencode", "sansuo", "mcp_client", "custom"] = "custom"
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=100)
    tool_names: list[str] = Field(min_length=1, max_length=30)


class IntegrationPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=100)
    tool_names: list[str] = Field(min_length=1, max_length=30)


class ExternalToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_base_id: str = Field(min_length=1, max_length=64)
    arguments: dict[str, object] = Field(default_factory=dict)
    approval_id: str | None = Field(default=None, max_length=64)


class IntegrationStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS integrations (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, client_type TEXT NOT NULL,
                    owner TEXT NOT NULL, owner_is_admin INTEGER NOT NULL,
                    knowledge_base_ids TEXT NOT NULL, tool_names TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    last_used_at TEXT, revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS integration_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, integration_id TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                    status TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_integration_calls ON integration_calls(integration_id, id DESC);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _view(row: sqlite3.Row | dict) -> dict:
        result = dict(row)
        result.pop("token_hash", None)
        result["owner_is_admin"] = bool(result["owner_is_admin"])
        result["knowledge_base_ids"] = json.loads(result["knowledge_base_ids"])
        result["tool_names"] = json.loads(result["tool_names"])
        return result

    def create(self, value: IntegrationCreate, owner: str, owner_is_admin: bool) -> tuple[dict, str]:
        integration_id = f"int_{uuid.uuid4().hex}"
        token = "sk_kb_" + secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO integrations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (integration_id, value.name.strip(), value.client_type, owner, int(owner_is_admin),
                 json.dumps(sorted(set(value.knowledge_base_ids))), json.dumps(sorted(set(value.tool_names))),
                 self._digest(token), self._now(), None, None),
            )
        return self.get(integration_id), token

    def get(self, integration_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM integrations WHERE id=?", (integration_id,)).fetchone()
        if row is None:
            raise KeyError(integration_id)
        return self._view(row)

    def list(self, owner: str | None = None) -> list[dict]:
        with self.connect() as connection:
            if owner is None:
                rows = connection.execute("SELECT * FROM integrations ORDER BY created_at DESC").fetchall()
            else:
                rows = connection.execute("SELECT * FROM integrations WHERE owner=? ORDER BY created_at DESC", (owner,)).fetchall()
        return [self._view(row) for row in rows]

    def authenticate(self, token: str) -> dict | None:
        if not token.startswith("sk_kb_") or len(token) > 128:
            return None
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM integrations WHERE token_hash=? AND revoked_at IS NULL", (self._digest(token),)).fetchone()
        return self._view(row) if row else None

    def rotate(self, integration_id: str) -> str:
        token = "sk_kb_" + secrets.token_urlsafe(32)
        with self.connect() as connection:
            cursor = connection.execute("UPDATE integrations SET token_hash=? WHERE id=? AND revoked_at IS NULL", (self._digest(token), integration_id))
            if not cursor.rowcount:
                raise KeyError(integration_id)
        return token

    def update_permissions(self, integration_id: str, value: IntegrationPermissions) -> dict:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE integrations SET knowledge_base_ids=?, tool_names=? WHERE id=? AND revoked_at IS NULL",
                (json.dumps(sorted(set(value.knowledge_base_ids))),
                 json.dumps(sorted(set(value.tool_names))), integration_id),
            )
            if not cursor.rowcount:
                raise KeyError(integration_id)
        return self.get(integration_id)

    def revoke(self, integration_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute("UPDATE integrations SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (self._now(), integration_id))
            if not cursor.rowcount:
                raise KeyError(integration_id)

    def record_call(self, integration_id: str, kb_id: str, tool: str, status: str, duration_ms: int) -> None:
        now = self._now()
        with self.connect() as connection:
            connection.execute("INSERT INTO integration_calls(integration_id, knowledge_base_id, tool_name, status, duration_ms, created_at) VALUES (?,?,?,?,?,?)", (integration_id, kb_id, tool, status, duration_ms, now))
            connection.execute("UPDATE integrations SET last_used_at=? WHERE id=?", (now, integration_id))

    def calls(self, integration_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT knowledge_base_id,tool_name,status,duration_ms,created_at FROM integration_calls WHERE integration_id=? ORDER BY id DESC LIMIT 100", (integration_id,)).fetchall()
        return [dict(row) for row in rows]
