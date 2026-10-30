from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from app.schemas import BatchIngestCreate, IngestJob, JobItem, TaskSummary


logger = logging.getLogger("agentic_rag.ingest")


TERMINAL_STATUSES = {"success", "duplicate", "preview_ready", "failed"}
SUCCESS_STATUSES = {"success", "duplicate"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_jobs (
  job_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  rewrite_enabled INTEGER NOT NULL DEFAULT 0,
  rewrite_prompt TEXT NOT NULL DEFAULT '',
  rewrite_review_required INTEGER NOT NULL DEFAULT 0,
  workflow_enabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_job_items (
  item_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  filename TEXT NOT NULL,
  staging_path TEXT,
  source_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  message TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  FOREIGN KEY(job_id) REFERENCES ingest_jobs(job_id) ON DELETE CASCADE,
  UNIQUE(job_id, position)
);
CREATE INDEX IF NOT EXISTS idx_ingest_items_dispatch
  ON ingest_job_items(status, next_attempt_at, job_id);
CREATE INDEX IF NOT EXISTS idx_ingest_items_source ON ingest_job_items(source_id);
"""


class IngestQueueRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            connection.execute(
                "UPDATE ingest_job_items SET status='retrying', message='服务重启，任务已恢复' WHERE status='running'"
            )
        self._restore_staging_filenames()

    def _restore_staging_filenames(self) -> None:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT item_id,filename,staging_path FROM ingest_job_items WHERE staging_path IS NOT NULL"
            ).fetchall()
            for row in rows:
                current = Path(row["staging_path"])
                expected = current.parent / Path(row["filename"]).name
                if current == expected or not current.is_file():
                    continue
                try:
                    if expected.exists():
                        current.unlink()
                    else:
                        current.replace(expected)
                    connection.execute(
                        "UPDATE ingest_job_items SET staging_path=? WHERE item_id=?",
                        (str(expected.resolve()), row["item_id"]),
                    )
                except OSError:
                    # Keep the existing path usable; the item can still be
                    # retried manually if the filesystem blocks the rename.
                    continue

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create(self, agent_id: str, value: BatchIngestCreate) -> IngestJob:
        job_id = f"job_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO ingest_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id, agent_id, "uploading", int(value.rewrite_enabled), value.rewrite_prompt,
                    int(value.rewrite_review_required), int(value.workflow_enabled), now, now,
                ),
            )
            connection.executemany(
                "INSERT INTO ingest_job_items(item_id,job_id,position,filename) VALUES (?,?,?,?)",
                [(f"item_{uuid.uuid4().hex}", job_id, position, Path(name).name) for position, name in enumerate(value.filenames)],
            )
        return self.get(job_id)

    def job_record(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ingest_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def get(self, job_id: str) -> IngestJob:
        job = self.job_record(job_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ingest_job_items WHERE job_id=? ORDER BY position", (job_id,)
            ).fetchall()
        items = [JobItem(**dict(row)) for row in rows]
        uploaded = sum(item.status != "pending" for item in items)
        completed = sum(item.status in TERMINAL_STATUSES for item in items)
        return IngestJob(
            job_id=job_id,
            status=job["status"],
            total=len(items),
            uploaded=uploaded,
            completed=completed,
            succeeded=sum(item.status in SUCCESS_STATUSES for item in items),
            failed=sum(item.status == "failed" for item in items),
            preview_ready=sum(item.status == "preview_ready" for item in items),
            items=items,
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
                       SUM(CASE WHEN i.status IN ('success','duplicate','preview_ready','failed') THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN i.status IN ('success','duplicate') THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN i.status='preview_ready' THEN 1 ELSE 0 END) AS review_required
                    FROM ingest_jobs j
                    LEFT JOIN ingest_job_items i ON i.job_id=j.job_id
                    {where}
                    GROUP BY j.job_id
                    ORDER BY j.created_at DESC""",
                params,
            ).fetchall()
        summaries: list[TaskSummary] = []
        for row in rows:
            raw_status = str(row["status"])
            failed = int(row["failed"] or 0)
            review_required = int(row["review_required"] or 0)
            if raw_status in {"uploading", "pending"} or review_required:
                status = "PENDING"
            elif raw_status in {"running", "paused"}:
                status = "RUNNING"
            elif raw_status == "completed" and failed:
                status = "FAILED"
            else:
                status = "SUCCEEDED"
            total = int(row["total"] or 0)
            summaries.append(TaskSummary(
                task_id=row["job_id"], task_type="document_ingest", agent_id=row["agent_id"],
                title=f"批量入库 · {total} 个文件", status=status, raw_status=raw_status,
                total=total, completed=int(row["completed"] or 0), succeeded=int(row["succeeded"] or 0),
                failed=failed, review_required=review_required, created_at=row["created_at"], updated_at=row["updated_at"],
                can_pause=raw_status == "running", can_resume=raw_status == "paused",
                can_retry=failed > 0,
            ))
        return summaries

    def has_active_job(self, agent_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM ingest_jobs
                   WHERE agent_id=? AND status IN ('uploading','pending','running','paused')
                   LIMIT 1""",
                (agent_id,),
            ).fetchone()
        return row is not None

    def cancel_inactive_jobs_for_bulk_delete(self, agent_id: str) -> int:
        """Cancel queued/staged work while refusing to race an executing item."""
        staging_paths: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                """SELECT COUNT(*) FROM ingest_job_items i
                   JOIN ingest_jobs j ON j.job_id=i.job_id
                   WHERE j.agent_id=? AND j.status IN ('uploading','pending','running','paused')
                     AND i.status='running'""",
                (agent_id,),
            ).fetchone()[0]
            if running:
                raise ValueError(f"当前仍有 {running} 个文件正在后台入库，请等待其完成后重试")
            rows = connection.execute(
                """SELECT i.staging_path FROM ingest_job_items i
                   JOIN ingest_jobs j ON j.job_id=i.job_id
                   WHERE j.agent_id=? AND j.status IN ('uploading','pending','running','paused')
                     AND i.status IN ('pending','uploaded','queued','retrying')""",
                (agent_id,),
            ).fetchall()
            staging_paths = [str(row["staging_path"]) for row in rows if row["staging_path"]]
            cursor = connection.execute(
                """UPDATE ingest_job_items
                   SET status='failed',message='资料库清空，未完成的入库项已取消',next_attempt_at=0
                   WHERE job_id IN (
                     SELECT job_id FROM ingest_jobs
                     WHERE agent_id=? AND status IN ('uploading','pending','running','paused')
                   ) AND status IN ('pending','uploaded','queued','retrying')""",
                (agent_id,),
            )
            connection.execute(
                """UPDATE ingest_jobs SET status='completed',updated_at=?
                   WHERE agent_id=? AND status IN ('uploading','pending','running','paused')""",
                (datetime.now(timezone.utc).isoformat(), agent_id),
            )
            cancelled = cursor.rowcount
        for value in staging_paths:
            path = Path(value)
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
                path.parent.parent.rmdir()
            except OSError:
                # The staging artifact is no longer dispatchable. A leftover
                # empty/non-empty directory is harmless and can be cleaned later.
                continue
        if cancelled:
            logger.info("ingest_jobs_cancelled_for_bulk_delete agent_id=%s items=%s", agent_id, cancelled)
        return cancelled

    def item_for_position(self, job_id: str, position: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ingest_job_items WHERE job_id=? AND position=?", (job_id, position)
            ).fetchone()
        if row is None:
            raise KeyError(f"{job_id}:{position}")
        return dict(row)

    def mark_uploaded(self, item_id: str, staging_path: Path) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE ingest_job_items SET staging_path=?,status='uploaded',message='已上传，等待任务启动' WHERE item_id=?",
                (str(staging_path.resolve()), item_id),
            )

    def start(self, job_id: str) -> IngestJob:
        job = self.get(job_id)
        if job.uploaded != job.total:
            raise ValueError(f"仍有 {job.total - job.uploaded} 个文件未上传")
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                "UPDATE ingest_job_items SET status='queued',message='等待后台处理' WHERE job_id=? AND status='uploaded'",
                (job_id,),
            )
            connection.execute("UPDATE ingest_jobs SET status='running',updated_at=? WHERE job_id=?", (now, job_id))
        return self.get(job_id)

    def pause(self, job_id: str) -> IngestJob:
        self.job_record(job_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE ingest_jobs SET status='paused',updated_at=? WHERE job_id=?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
        return self.get(job_id)

    def resume(self, job_id: str) -> IngestJob:
        self.job_record(job_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE ingest_jobs SET status='running',updated_at=? WHERE job_id=?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
        self.recompute(job_id)
        return self.get(job_id)

    def retry_failed(self, job_id: str) -> IngestJob:
        self.job_record(job_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE ingest_job_items SET status='retrying',message='等待重新处理',next_attempt_at=0 WHERE job_id=? AND status='failed' AND staging_path IS NOT NULL",
                (job_id,),
            )
            connection.execute(
                "UPDATE ingest_jobs SET status='running',updated_at=? WHERE job_id=?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
        return self.get(job_id)

    def claim(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT i.*,j.agent_id,j.rewrite_enabled,j.rewrite_prompt,j.rewrite_review_required,j.workflow_enabled
                   FROM ingest_job_items i JOIN ingest_jobs j ON j.job_id=i.job_id
                   WHERE j.status='running' AND i.status IN ('queued','retrying') AND i.next_attempt_at<=?
                   ORDER BY j.created_at,i.position LIMIT ?""",
                (time.time(), limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE ingest_job_items SET status='running',message='正在处理',attempts=attempts+1 WHERE item_id=?",
                    (row["item_id"],),
                )
            connection.commit()
            return [dict(row) for row in rows]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_item(self, item_id: str, status: str, source_id: str | None, message: str) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT job_id FROM ingest_job_items WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE ingest_job_items SET status=?,source_id=?,message=?,next_attempt_at=0 WHERE item_id=?",
                (status, source_id, message[:500], item_id),
            )
            job_id = row["job_id"]
        self.recompute(job_id)

    def schedule_retry(
        self,
        item_id: str,
        message: str,
        delay_seconds: int,
        source_id: str | None = None,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT job_id FROM ingest_job_items WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                return
            connection.execute(
                """UPDATE ingest_job_items
                   SET status='retrying',message=?,next_attempt_at=?,source_id=COALESCE(?,source_id)
                   WHERE item_id=?""",
                (
                    f"{message[:420]}；{delay_seconds} 秒后自动重试",
                    time.time() + delay_seconds,
                    source_id,
                    item_id,
                ),
            )

    def recompute(self, job_id: str) -> None:
        with self.connect() as connection:
            job = connection.execute("SELECT status FROM ingest_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or job["status"] in {"paused", "uploading"}:
                return
            rows = connection.execute("SELECT status FROM ingest_job_items WHERE job_id=?", (job_id,)).fetchall()
            status = "completed" if rows and all(row["status"] in TERMINAL_STATUSES for row in rows) else "running"
            connection.execute(
                "UPDATE ingest_jobs SET status=?,updated_at=? WHERE job_id=?",
                (status, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def complete_source(self, source_id: str) -> None:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT item_id,job_id FROM ingest_job_items WHERE source_id=? AND status='preview_ready'", (source_id,)
            ).fetchall()
            connection.executemany(
                "UPDATE ingest_job_items SET status='success',message='改写已确认并完成入库' WHERE item_id=?",
                [(row["item_id"],) for row in rows],
            )
        for job_id in {row["job_id"] for row in rows}:
            self.recompute(job_id)


class IngestQueueManager:
    RETRY_DELAYS = (5, 15, 60)
    DIFY_POLL_DELAYS = (5, 10, 15, 30)
    DIFY_MAX_POLL_ATTEMPTS = 60

    def __init__(
        self,
        repository: IngestQueueRepository,
        runtime_resolver: Callable[[str], Any],
        worker_count: int = 3,
        max_retries: int = 3,
    ):
        self.repository = repository
        self.runtime_resolver = runtime_resolver
        self.worker_count = max(1, worker_count)
        self.max_retries = max(0, max_retries)
        self._executor = ThreadPoolExecutor(max_workers=self.worker_count, thread_name_prefix="ingest")
        self._active: set[Future[Any]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name="ingest-dispatch", daemon=True)

    def start(self) -> None:
        if not self._dispatcher.is_alive():
            self._dispatcher.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._dispatcher.is_alive():
            self._dispatcher.join(timeout=2)
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _dispatch_loop(self) -> None:
        while not self._stop.wait(0.35):
            with self._lock:
                self._active = {future for future in self._active if not future.done()}
                available = self.worker_count - len(self._active)
            for item in self.repository.claim(available):
                future = self._executor.submit(self._process, item)
                with self._lock:
                    self._active.add(future)

    def _process(self, item: dict[str, Any]) -> None:
        source_id: str | None = None
        job_id = str(item.get("job_id") or "")
        item_id = str(item.get("item_id") or "")
        filename = str(item.get("filename") or "")
        attempt = int(item.get("attempts") or 0) + 1
        logger.info(
            "ingest_started job_id=%s item_id=%s filename=%r attempt=%d",
            job_id, item_id, filename, attempt,
        )
        try:
            runtime = self.runtime_resolver(str(item["agent_id"]))
            duplicate = False
            existing_source_id = str(item.get("source_id") or "")
            if existing_source_id:
                existing = runtime.ingestion.repository.get(existing_source_id)
            else:
                existing = None
            poll_dify_only = bool(
                existing
                and existing.parse_status == "success"
                and existing.pdf_platform_status == "success"
                and existing.dify_status == "processing"
            )
            if poll_dify_only:
                source = runtime.ingestion.refresh_dify_status(existing_source_id)
            else:
                source, duplicate = runtime.ingestion.ingest(
                    Path(str(item["staging_path"])),
                    bool(item["rewrite_enabled"]),
                    str(item["rewrite_prompt"]),
                    bool(item["workflow_enabled"]),
                    bool(item["rewrite_review_required"]),
                    getattr(runtime, "owner_username", "sansuokb"),
                )
            source_id = source.source_id
            if source.rewrite_status == "preview_ready":
                self.repository.finish_item(item["item_id"], "preview_ready", source_id, "AI 改写完成，等待预览确认")
                logger.info(
                    "ingest_preview_ready job_id=%s item_id=%s filename=%r source_id=%s",
                    job_id, item_id, filename, source_id,
                )
                self._cleanup_staging(item)
                return
            all_success = all(value == "success" for value in (source.parse_status, source.dify_status, source.pdf_platform_status))
            if all_success:
                self.repository.finish_item(
                    item["item_id"], "duplicate" if duplicate else "success", source_id, source.error_message or "入库完成"
                )
                logger.info(
                    "ingest_succeeded job_id=%s item_id=%s filename=%r source_id=%s duplicate=%s",
                    job_id, item_id, filename, source_id, duplicate,
                )
                self._cleanup_staging(item)
                return
            status_message = self._status_message(source)
            if source.dify_status == "processing" and all(
                value == "success" for value in (source.parse_status, source.pdf_platform_status)
            ):
                if attempt <= self.DIFY_MAX_POLL_ATTEMPTS:
                    delay = self.DIFY_POLL_DELAYS[min(attempt - 1, len(self.DIFY_POLL_DELAYS) - 1)]
                    self.repository.schedule_retry(item_id, status_message, delay, source_id)
                    logger.info(
                        "ingest_dify_poll_scheduled job_id=%s item_id=%s filename=%r source_id=%s "
                        "attempt=%d delay_seconds=%d statuses=%s",
                        job_id, item_id, filename, source_id, attempt, delay, status_message,
                    )
                    return
                status_message = f"Dify 索引等待超时；{status_message}"
            raise RuntimeError(status_message)
        except Exception as exc:
            message = str(exc)[:500]
            if attempt <= self.max_retries and self._transient(message):
                delay = self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)]
                self.repository.schedule_retry(item["item_id"], message, delay, source_id)
                logger.warning(
                    "ingest_retry_scheduled job_id=%s item_id=%s filename=%r source_id=%s "
                    "attempt=%d delay_seconds=%d error=%r",
                    job_id, item_id, filename, source_id or "", attempt, delay, message,
                    exc_info=True,
                )
            else:
                self.repository.finish_item(item["item_id"], "failed", source_id, message)
                logger.exception(
                    "ingest_failed job_id=%s item_id=%s filename=%r source_id=%s attempt=%d error=%r",
                    job_id, item_id, filename, source_id or "", attempt, message,
                )

    @staticmethod
    def _status_message(source: Any) -> str:
        statuses = (
            f"parse_status={source.parse_status}, "
            f"dify_status={source.dify_status}, "
            f"pdf_platform_status={source.pdf_platform_status}"
        )
        if source.error_message:
            return f"{source.error_message}；{statuses}"
        return f"入库流程未全部完成：{statuses}"

    @staticmethod
    def _transient(message: str) -> bool:
        value = message.casefold()
        return any(token in value for token in ("timeout", "timed out", "429", "502", "503", "504", "connection", "temporarily"))

    @staticmethod
    def _cleanup_staging(item: dict[str, Any]) -> None:
        path = Path(str(item.get("staging_path") or ""))
        if not path.name:
            return
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
