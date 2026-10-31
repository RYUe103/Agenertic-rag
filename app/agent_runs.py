from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class AgentRunStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL, operation TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
                    duration_ms INTEGER, tool_calls INTEGER, evidence_count INTEGER,
                    evidence_insufficient INTEGER, trace TEXT NOT NULL DEFAULT '[]',
                    question TEXT NOT NULL DEFAULT '', conversation_id TEXT NOT NULL DEFAULT '', history TEXT NOT NULL DEFAULT '[]',
                    approval_id TEXT, approval_tool TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_owner ON agent_runs(owner_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_kb ON agent_runs(knowledge_base_id, created_at DESC);
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")}
            for name, declaration in (("question", "TEXT NOT NULL DEFAULT ''"), ("conversation_id", "TEXT NOT NULL DEFAULT ''"), ("history", "TEXT NOT NULL DEFAULT '[]'"), ("approval_id", "TEXT"), ("approval_tool", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {declaration}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def start(self, owner_id: str, kb_id: str, agent_id: str, agent_name: str, operation: str, question: str = "", conversation_id: str = "", history: list[object] | None = None) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        with self.connect() as connection:
            serialized = [item.model_dump() if hasattr(item, "model_dump") else item for item in (history or [])]
            connection.execute("INSERT INTO agent_runs(run_id,owner_id,knowledge_base_id,agent_id,agent_name,operation,status,created_at,question,conversation_id,history) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (run_id, owner_id, kb_id, agent_id, agent_name, operation, "running", datetime.now(timezone.utc).isoformat(), question, conversation_id, json.dumps(serialized, ensure_ascii=False)))
        return run_id

    def waiting(self, run_id: str, approval_id: str, tool_name: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE agent_runs SET status='waiting_approval',approval_id=?,approval_tool=? WHERE run_id=?", (approval_id, tool_name, run_id))

    def resumed(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE agent_runs SET status='resumed',completed_at=? WHERE run_id=? AND status='waiting_approval'", (datetime.now(timezone.utc).isoformat(), run_id))

    def finish(self, run_id: str, status: str, duration_ms: int, result: object | None = None) -> None:
        trace = []
        for event in getattr(result, "trace", []):
            trace.append(event.model_dump(include={"sequence", "kind", "title", "status", "round", "tool_name", "evidence_count"}))
        with self.connect() as connection:
            connection.execute("""UPDATE agent_runs SET status=?,completed_at=?,duration_ms=?,tool_calls=?,
                               evidence_count=?,evidence_insufficient=?,trace=? WHERE run_id=?""",
                               (status, datetime.now(timezone.utc).isoformat(), duration_ms,
                                getattr(result, "tool_calls", None), len(getattr(result, "evidences", [])) if result is not None else None,
                                int(getattr(result, "evidence_insufficient", False)) if result is not None else None,
                                json.dumps(trace, ensure_ascii=False), run_id))

    def list(self, owner_id: str | None, limit: int, offset: int) -> list[dict]:
        with self.connect() as connection:
            if owner_id is None:
                rows = connection.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM agent_runs WHERE owner_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (owner_id, limit, offset)).fetchall()
        return [self._view(row) for row in rows]

    def get(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._view(row)

    def resume_data(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT question,conversation_id,history FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def page(self, owner_id: str | None, knowledge_base_ids: list[str] | None,
             status: str, operation: str, limit: int, offset: int) -> tuple[list[dict], int]:
        conditions: list[str] = []
        parameters: list[object] = []
        if owner_id is not None:
            conditions.append("owner_id = ?")
            parameters.append(owner_id)
        if knowledge_base_ids is not None:
            if not knowledge_base_ids:
                return [], 0
            conditions.append(f"knowledge_base_id IN ({','.join('?' for _ in knowledge_base_ids)})")
            parameters.extend(knowledge_base_ids)
        for column, value in (("status", status), ("operation", operation)):
            if value:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM agent_runs" + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT * FROM agent_runs" + where +
                                      " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
                                      [*parameters, limit, offset]).fetchall()
        return [self._view(row) for row in rows], total

    def summary(self) -> dict:
        with self.connect() as connection:
            row = connection.execute("""SELECT COUNT(*) AS runs,
                SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                SUM(CASE WHEN status NOT IN ('succeeded','running') THEN 1 ELSE 0 END) AS failed,
                AVG(duration_ms) AS avg_duration_ms FROM agent_runs""").fetchone()
        return {key: (float(value) if key == 'avg_duration_ms' and value is not None else int(value or 0))
                for key, value in dict(row).items()}

    @staticmethod
    def _view(row: sqlite3.Row) -> dict:
        value = dict(row)
        for field in ("question", "conversation_id", "history"):
            value.pop(field, None)
        value["trace"] = json.loads(value["trace"])
        if value["evidence_insufficient"] is not None:
            value["evidence_insufficient"] = bool(value["evidence_insufficient"])
        return value
