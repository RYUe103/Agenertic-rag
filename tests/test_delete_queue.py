from __future__ import annotations

from app.delete_queue import DeleteQueueRepository
from app.schemas import Source


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
