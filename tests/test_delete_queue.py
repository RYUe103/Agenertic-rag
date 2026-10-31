from __future__ import annotations

from app.delete_queue import DeleteQueueRepository
from app.schemas import Source
import pytest


def fake_source(source_id: str, filename: str) -> Source:
    return Source(
        source_id=source_id,
        filename=filename,
        normalized_filename=filename,
        file_hash=source_id,
        local_pdf_path=f"/tmp/{source_id}/source.pdf",
        local_source_path=f"/tmp/{source_id}/source.pdf",
        local_md_path=f"/tmp/{source_id}/document.md",
        created_at="2026-10-19T00:00:00+00:00",
        updated_at="2026-10-19T00:00:00+00:00",
    )


def test_running_delete_item_is_recovered_after_restart(tmp_path) -> None:
    database = tmp_path / "delete-queue.db"
    repository = DeleteQueueRepository(database)
    job = repository.create("default", "default-kb", "默认知识库", [fake_source("src_one", "one.pdf")])
    claimed = repository.claim()
    assert claimed is not None
    assert repository.get(job.job_id).items[0].status == "running"

    recovered = DeleteQueueRepository(database)
    item = recovered.get(job.job_id).items[0]
    assert item.status == "pending"
    assert "服务重启" in item.message


def test_empty_delete_job_completes_immediately(tmp_path) -> None:
    repository = DeleteQueueRepository(tmp_path / "delete-queue.db")
    job = repository.create("default", "default-kb", "默认知识库", [])

    assert job.status == "completed"
    assert job.total == 0
    assert repository.claim() is None


def test_only_one_active_delete_job_per_knowledge_base(tmp_path) -> None:
    repository = DeleteQueueRepository(tmp_path / 'delete-queue.db')
    repository.create('agent-a', 'kb', '知识库', [fake_source('one', 'one.pdf')])
    with pytest.raises(ValueError):
        repository.create('agent-b', 'kb', '知识库', [fake_source('two', 'two.pdf')])
    assert repository.active_for_knowledge_base('kb')
    assert not repository.active_for_knowledge_base('other')


def test_cancel_stops_pending_items_and_waits_for_running_item(tmp_path) -> None:
    repository = DeleteQueueRepository(tmp_path / "delete-queue.db")
    job = repository.create("default", "default-kb", "默认知识库", [fake_source("one", "one.pdf"), fake_source("two", "two.pdf")])
    item = repository.claim()
    assert item is not None
    cancelling = repository.cancel(job.job_id)
    assert cancelling.status == "cancelling"
    assert [entry.status for entry in cancelling.items] == ["running", "cancelled"]
    assert repository.active_for_agent("default") is not None
    assert repository.claim() is None
    repository.finish(item["item_id"], "success", "已删除")
    completed = repository.get(job.job_id)
    assert completed.status == "cancelled"
    assert completed.completed == 2
    assert completed.succeeded == 1
    assert repository.task_summaries()[0].status == "CANCELLED"
    assert repository.active_for_agent("default") is None


def test_cancelled_running_item_is_not_retried_after_restart(tmp_path) -> None:
    database = tmp_path / "delete-queue.db"
    repository = DeleteQueueRepository(database)
    job = repository.create("default", "default-kb", "默认知识库", [fake_source("one", "one.pdf")])
    assert repository.claim() is not None
    repository.cancel(job.job_id)
    recovered = DeleteQueueRepository(database)
    assert recovered.get(job.job_id).status == "cancelled"
    assert recovered.get(job.job_id).items[0].status == "unknown"
    assert recovered.claim() is None
