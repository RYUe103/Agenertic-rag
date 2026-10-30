from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.schemas import Source


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  normalized_filename TEXT NOT NULL,
  file_hash TEXT NOT NULL UNIQUE,
  document_type TEXT NOT NULL DEFAULT 'pdf',
  mime_type TEXT NOT NULL DEFAULT 'application/pdf',
  standard_no TEXT,
  normalized_standard_no TEXT,
  standard_title TEXT,
  standard_year INTEGER,
  publish_date TEXT,
  effective_date TEXT,
  status TEXT,
  replaces TEXT,
  replaced_by TEXT,
  local_pdf_path TEXT NOT NULL,
  local_source_path TEXT NOT NULL DEFAULT '',
  local_md_path TEXT NOT NULL,
  local_extracted_md_path TEXT NOT NULL DEFAULT '',
  dify_dataset_id TEXT,
  dify_document_id TEXT,
  pdf_platform_id TEXT,
  pdf_url TEXT,
  parse_status TEXT NOT NULL DEFAULT 'pending',
  dify_status TEXT NOT NULL DEFAULT 'pending',
  pdf_platform_status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error_message TEXT
  ,custom_metadata TEXT NOT NULL DEFAULT '{}'
  ,metadata_status TEXT NOT NULL DEFAULT 'pending'
  ,metadata_error TEXT
  ,metadata_extracted_at TEXT
  ,workflow_status TEXT NOT NULL DEFAULT 'disabled'
  ,workflow_error TEXT
  ,workflow_output_path TEXT
  ,rewrite_status TEXT NOT NULL DEFAULT 'not_requested'
  ,rewrite_prompt TEXT NOT NULL DEFAULT ''
  ,rewrite_preview_path TEXT
  ,rewrite_review_required INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sources_filename ON sources(normalized_filename);
CREATE INDEX IF NOT EXISTS idx_sources_standard ON sources(normalized_standard_no);
CREATE INDEX IF NOT EXISTS idx_sources_year ON sources(standard_year);
CREATE INDEX IF NOT EXISTS idx_sources_dify_document ON sources(dify_document_id);
"""


class SourceRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)").fetchall()}
            migrations = {
                "document_type": "TEXT NOT NULL DEFAULT 'pdf'",
                "mime_type": "TEXT NOT NULL DEFAULT 'application/pdf'",
                "local_source_path": "TEXT NOT NULL DEFAULT ''",
                "local_extracted_md_path": "TEXT NOT NULL DEFAULT ''",
                "custom_metadata": "TEXT NOT NULL DEFAULT '{}'",
                "metadata_status": "TEXT NOT NULL DEFAULT 'pending'",
                "metadata_error": "TEXT",
                "metadata_extracted_at": "TEXT",
                "workflow_status": "TEXT NOT NULL DEFAULT 'disabled'",
                "workflow_error": "TEXT",
                "workflow_output_path": "TEXT",
                "rewrite_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "rewrite_prompt": "TEXT NOT NULL DEFAULT ''",
                "rewrite_preview_path": "TEXT",
                "rewrite_review_required": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE sources ADD COLUMN {name} {definition}")
            connection.execute("UPDATE sources SET local_source_path = local_pdf_path WHERE local_source_path = ''")
            connection.execute("UPDATE sources SET local_extracted_md_path = local_md_path WHERE local_extracted_md_path = ''")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create(self, values: dict[str, Any]) -> Source:
        now = datetime.now(timezone.utc).isoformat()
        values = self._serialize({**values, "created_at": now, "updated_at": now})
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO sources ({columns}) VALUES ({placeholders})", tuple(values.values())
            )
        return self.get(values["source_id"])

    def update(self, source_id: str, **values: Any) -> Source:
        values = self._serialize(values)
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE sources SET {assignments} WHERE source_id = ?",
                (*values.values(), source_id),
            )
        return self.get(source_id)

    def get(self, source_id: str) -> Source:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return self._source(row)

    def by_hash(self, file_hash: str) -> Source | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE file_hash = ?", (file_hash,)
            ).fetchone()
        return self._source(row) if row else None

    def by_dify_document(self, document_id: str) -> Source | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE dify_document_id = ?", (document_id,)
            ).fetchone()
        return self._source(row) if row else None

    def delete(self, source_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
        if cursor.rowcount == 0:
            raise KeyError(source_id)

    def list(self, limit: int = 100, offset: int = 0) -> list[Source]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [self._source(row) for row in rows]

    def list_all(self) -> list[Source]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources ORDER BY created_at DESC"
            ).fetchall()
        return [self._source(row) for row in rows]

    def list_page(self, page: int = 1, page_size: int = 30, query: str = "") -> tuple[list[Source], int]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        where = ""
        params: list[Any] = []
        if query.strip():
            like = f"%{query.strip()}%"
            where = " WHERE filename LIKE ? OR normalized_filename LIKE ? OR coalesce(standard_title,'') LIKE ?"
            params.extend((like, like, like))
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM sources{where}", params).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM sources{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return [self._source(row) for row in rows], total

    def search(self, terms: list[str], limit: int = 20) -> list[Source]:
        if not terms:
            return self.list(limit)
        clauses: list[str] = []
        params: list[Any] = []
        for term in terms:
            like = f"%{term}%"
            clauses.append("(normalized_filename LIKE ? OR normalized_standard_no LIKE ? OR lower(coalesce(standard_title,'')) LIKE lower(?))")
            params.extend((like, like, like))
        sql = "SELECT * FROM sources WHERE " + " OR ".join(clauses) + " ORDER BY standard_year DESC, filename LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._source(row) for row in rows]

    @staticmethod
    def _serialize(values: dict[str, Any]) -> dict[str, Any]:
        result = dict(values)
        if "custom_metadata" in result and not isinstance(result["custom_metadata"], str):
            import json
            value = result["custom_metadata"]
            result["custom_metadata"] = json.dumps(
                {key: item.model_dump() if hasattr(item, "model_dump") else item for key, item in value.items()},
                ensure_ascii=False,
            )
        return result

    @staticmethod
    def _source(row: sqlite3.Row) -> Source:
        import json
        values = dict(row)
        values["custom_metadata"] = json.loads(values.get("custom_metadata") or "{}")
        return Source.model_validate(values)
