from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from app.schemas import DeleteJob, DeleteJobItem, Source, TaskSummary


logger = logging.getLogger("agentic_rag.delete")

SCHEMA = """
CREATE TABLE IF NOT EXISTS delete_jobs (
  job_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  knowledge_base_id TEXT NOT NULL,
  knowledge_base_name TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delete_job_items (
  item_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  source_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  message TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(job_id) REFERENCES delete_jobs(job_id) ON DELETE CASCADE,
  UNIQUE(job_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_delete_items_dispatch
  ON delete_job_items(status, job_id, position);
"""


class DeleteQueueRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            connection.execute(
                "UPDATE delete_job_items SET status='pending',message='服务重启，等待继续删除' WHERE status='running'"
            )
            connection.execute(
                "UPDATE delete_jobs SET status='running' WHERE status!='completed'"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create(
        self,
        agent_id: str,
        knowledge_base_id: str,
        knowledge_base_name: str,
        sources: list[Source],
    ) -> DeleteJob:
        if self.active_for_agent(agent_id) is not None:
            raise ValueError("当前 Agent 已有清空任务正在执行")
        job_id = f"delete_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        status = "running" if sources else "completed"
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO delete_jobs VALUES (?,?,?,?,?,?,?)",
                (job_id, agent_id, knowledge_base_id, knowledge_base_name, status, now, now),
            )
            connection.executemany(
                """INSERT INTO delete_job_items
                   (item_id,job_id,position,source_id,filename) VALUES (?,?,?,?,?)""",
                [
                    (f"delete_item_{uuid.uuid4().hex}", job_id, position, source.source_id, source.filename)
                    for position, source in enumerate(sources)
                ],
            )
        logger.info(
            "bulk_delete_started job_id=%s agent_id=%s knowledge_base_id=%s total=%s",
            job_id, agent_id, knowledge_base_id, len(sources),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> DeleteJob:
        with self.connect() as connection:
            job = connection.execute("SELECT * FROM delete_jobs WHERE job_id=?", (job_id,)).fetchone()
            items = connection.execute(
                "SELECT * FROM delete_job_items WHERE job_id=? ORDER BY position", (job_id,)
            ).fetchall()
        if job is None:
            raise KeyError(job_id)
        values = [DeleteJobItem(**dict(item)) for item in items]
        completed = sum(item.status in {"success", "failed"} for item in values)
        return DeleteJob(
            job_id=job_id,
            agent_id=job["agent_id"],
            knowledge_base_id=job["knowledge_base_id"],
            knowledge_base_name=job["knowledge_base_name"],
            status=job["status"],
            total=len(values),
            completed=completed,
            succeeded=sum(item.status == "success" for item in values),
            failed=sum(item.status == "failed" for item in values),
            items=values,
            created_at=job["created_at"],
            updated_at=job["updated_at"],
        )

    def task_summaries(self, agent_ids: set[str] | None = None) -> list[TaskSummary]:
        where = ""
        params: list[Any] = []
        if agent_ids is not None:
            if not agent_ids:
                return []
            placeholders = ",".join("?" for _ in agent_ids)
            where = f"WHERE j.agent_id IN ({placeholders})"
            params.extend(sorted(agent_ids))
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT j.*,
                       COUNT(i.item_id) AS total,
                       SUM(CASE WHEN i.status IN ('success','failed') THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN i.status='success' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END) AS failed
                    FROM delete_jobs j
                    LEFT JOIN delete_job_items i ON i.job_id=j.job_id
                    {where}
                    GROUP BY j.job_id
                    ORDER BY j.created_at DESC""",
                params,
            ).fetchall()
        summaries: list[TaskSummary] = []
        for row in rows:
            raw_status = str(row["status"])
            failed = int(row["failed"] or 0)
            status = "RUNNING" if raw_status == "running" else ("FAILED" if failed else "SUCCEEDED")
            total = int(row["total"] or 0)
            summaries.append(TaskSummary(
                task_id=row["job_id"], task_type="document_delete", agent_id=row["agent_id"],
                title=f"清空资料库 · {row['knowledge_base_name']}", status=status, raw_status=raw_status,
                total=total, completed=int(row["completed"] or 0), succeeded=int(row["succeeded"] or 0),
                failed=failed, created_at=row["created_at"], updated_at=row["updated_at"],
            ))
        return summaries

    def active_for_agent(self, agent_id: str) -> DeleteJob | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT job_id FROM delete_jobs WHERE agent_id=? AND status='running' ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self.get(row["job_id"]) if row else None

    def claim(self) -> dict[str, Any] | None:
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT i.*,j.agent_id FROM delete_job_items i
                   JOIN delete_jobs j ON j.job_id=i.job_id
                   WHERE j.status='running' AND i.status='pending'
                   ORDER BY j.created_at,i.position LIMIT 1"""
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE delete_job_items SET status='running',message='正在删除' WHERE item_id=?",
                    (row["item_id"],),
                )
            connection.commit()
            return dict(row) if row else None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish(self, item_id: str, status: str, message: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT job_id FROM delete_job_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE delete_job_items SET status=?,message=? WHERE item_id=?",
                (status, message[:500], item_id),
            )
            job_id = row["job_id"]
            pending = connection.execute(
                "SELECT 1 FROM delete_job_items WHERE job_id=? AND status IN ('pending','running') LIMIT 1",
                (job_id,),
            ).fetchone()
            if pending is None:
                connection.execute(
                    "UPDATE delete_jobs SET status='completed',updated_at=? WHERE job_id=?",
                    (datetime.now(timezone.utc).isoformat(), job_id),
                )
        job = self.get(job_id)
        if job.status == "completed":
            logger.info(
                "bulk_delete_completed job_id=%s succeeded=%s failed=%s",
                job_id, job.succeeded, job.failed,
            )


class DeleteQueueManager:
    def __init__(self, repository: DeleteQueueRepository, runtime_resolver: Callable[[str], Any]):
        self.repository = repository
        self.runtime_resolver = runtime_resolver
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="delete-queue", daemon=True)

    def start(self) -> None:
        if not self._worker.is_alive():
            self._worker.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self.repository.claim()
            if item is None:
                self._stop.wait(0.35)
                continue
            try:
                self.runtime_resolver(item["agent_id"]).ingestion.delete_source(item["source_id"])
            except Exception as exc:
                message = str(exc)[:500]
                logger.exception(
                    "bulk_delete_item_failed job_id=%s source_id=%s filename=%r error=%r",
                    item["job_id"], item["source_id"], item["filename"], message,
                )
                self.repository.finish(item["item_id"], "failed", message)
            else:
                self.repository.finish(item["item_id"], "success", "已删除 Dify 文档和本地文件")
