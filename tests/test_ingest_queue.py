from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.ingest_queue import IngestQueueRepository
from app.schemas import BatchIngestCreate


def queued_repository(tmp_path) -> tuple[IngestQueueRepository, str]:
    database = tmp_path / "ingest-queue.db"
    repository = IngestQueueRepository(database)
    job = repository.create("default", BatchIngestCreate(filenames=["document.txt"]))
    item = repository.item_for_position(job.job_id, 0)
    staged = tmp_path / "document.txt"
    staged.write_text("body", encoding="utf-8")
    repository.mark_uploaded(item["item_id"], staged)
    repository.start(job.job_id)
    return repository, job.job_id


def test_running_items_are_recovered_after_restart(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    claimed = repository.claim(1)
    assert len(claimed) == 1
    assert repository.get(job_id).items[0].status == "running"

    recovered = IngestQueueRepository(repository.db_path)
    item = recovered.get(job_id).items[0]
    assert item.status == "retrying"
    assert "服务重启" in item.message


def test_paused_job_does_not_dispatch_items(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    paused = repository.pause(job_id)
    assert paused.status == "paused"
    assert repository.claim(1) == []


def test_bulk_delete_cancels_paused_items_and_removes_staging_file(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    repository.pause(job_id)
    item = repository.item_for_position(job_id, 0)
    staging_path = Path(item["staging_path"])

    cancelled = repository.cancel_inactive_jobs_for_bulk_delete("default")

    assert cancelled == 1
    assert repository.get(job_id).status == "completed"
    assert repository.get(job_id).items[0].status == "failed"
    assert not staging_path.exists()


def test_bulk_delete_refuses_when_an_item_is_executing(tmp_path) -> None:
    repository, _job_id = queued_repository(tmp_path)
    assert repository.claim(1)

    try:
        repository.cancel_inactive_jobs_for_bulk_delete("default")
    except ValueError as exc:
        assert "正在后台入库" in str(exc)
    else:
        raise AssertionError("expected active ingestion to block bulk deletion")


def test_restart_restores_original_staging_filename(tmp_path) -> None:
    database = tmp_path / "ingest-queue.db"
    repository = IngestQueueRepository(database)
    job = repository.create("default", BatchIngestCreate(filenames=["原始名称.txt"]))
    item = repository.item_for_position(job.job_id, 0)
    old_path = tmp_path / item["item_id"] / "source.txt"
    old_path.parent.mkdir()
    old_path.write_text("body", encoding="utf-8")
    repository.mark_uploaded(item["item_id"], old_path)

    recovered = IngestQueueRepository(database)
    recovered_item = recovered.item_for_position(job.job_id, 0)
    recovered_path = tmp_path / item["item_id"] / "原始名称.txt"
    assert recovered_item["staging_path"] == str(recovered_path.resolve())
    assert recovered_path.read_text(encoding="utf-8") == "body"
    assert not old_path.exists()


class PollingIngestion:
    def __init__(self, source, refreshed_source) -> None:
        self.source = source
        self.refreshed_source = refreshed_source
        self.ingest_calls = 0
        self.refresh_calls = 0
        self.repository = SimpleNamespace(get=lambda _source_id: self.source)

    def ingest(self, *_args, **_kwargs):
        self.ingest_calls += 1
        return self.source, False

    def refresh_dify_status(self, _source_id):
        self.refresh_calls += 1
        self.source = self.refreshed_source
        return self.refreshed_source


def source(dify_status: str):
    return SimpleNamespace(
        source_id="src_test",
        parse_status="success",
        dify_status=dify_status,
        pdf_platform_status="success",
        rewrite_status="not_requested",
        error_message=None,
    )


def test_dify_processing_is_scheduled_for_polling_and_source_id_is_saved(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    item = repository.claim(1)[0]
    ingestion = PollingIngestion(source("processing"), source("processing"))
    from app.ingest_queue import IngestQueueManager

    manager = IngestQueueManager(repository, lambda _agent_id: SimpleNamespace(ingestion=ingestion))
    manager._process(item)

    queued = repository.get(job_id).items[0]
    assert queued.status == "retrying"
    assert queued.source_id == "src_test"
    assert "dify_status=processing" in queued.message
    assert ingestion.ingest_calls == 1


def test_dify_poll_refreshes_without_reuploading_and_finishes(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    first_item = repository.claim(1)[0]
    ingestion = PollingIngestion(source("processing"), source("processing"))
    from app.ingest_queue import IngestQueueManager

    manager = IngestQueueManager(repository, lambda _agent_id: SimpleNamespace(ingestion=ingestion))
    manager._process(first_item)
    with repository.connect() as connection:
        connection.execute(
            "UPDATE ingest_job_items SET next_attempt_at=0 WHERE item_id=?",
            (first_item["item_id"],),
        )
    ingestion.refreshed_source = source("success")

    manager._process(repository.claim(1)[0])

    completed = repository.get(job_id).items[0]
    assert completed.status == "success"
    assert ingestion.ingest_calls == 1
    assert ingestion.refresh_calls == 1
    assert not Path(first_item["staging_path"]).exists()
