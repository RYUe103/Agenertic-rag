from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest

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


def test_cancel_upload_removes_staged_files_and_rejects_late_upload(tmp_path) -> None:
    repository = IngestQueueRepository(tmp_path / 'queue.db')
    job = repository.create('default', BatchIngestCreate(filenames=['one.txt', 'two.txt']), 'alice')
    assert repository.job_record(job.job_id)['source_user'] == 'alice'
    first = repository.item_for_position(job.job_id, 0)
    directory = tmp_path / 'stage'
    directory.mkdir()
    staged = directory / 'one.txt'
    staged.write_text('uploaded', encoding='utf-8')
    repository.mark_uploaded(first['item_id'], staged)
    assert repository.task_summaries()[0].can_cancel is True
    assert repository.cancel(job.job_id).status == 'cancelled'
    assert not staged.exists()
    second = repository.item_for_position(job.job_id, 1)
    late = tmp_path / 'late.tmp'
    late.write_text('late', encoding='utf-8')
    with pytest.raises(ValueError, match='停止接收'):
        repository.mark_uploaded(second['item_id'], late, tmp_path / 'two.txt')
    assert not (tmp_path / 'two.txt').exists()
    assert repository.get(job.job_id).items[1].status == 'cancelled'


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


def test_real_ingestion_records_stage_timings(tmp_path, ingestion) -> None:
    from app.ingest_queue import IngestQueueManager

    repository, job_id = queued_repository(tmp_path)
    manager = IngestQueueManager(repository, lambda _: SimpleNamespace(ingestion=ingestion), max_retries=0)
    manager._process(repository.claim(1)[0])
    events = repository.events(job_id)['items']
    for phase in ('source_persist', 'parse', 'metadata_extract', 'dify_submit', 'dify_status', 'source_platform'):
        stage = [event for event in events if event['phase'] == phase]
        assert [event['status'] for event in reversed(stage)] == ['running', 'success']
        assert stage[0]['duration_ms'] is not None and stage[0]['duration_ms'] >= 0
        assert stage[1]['duration_ms'] is None
    assert repository.get(job_id).items[0].status == 'success'


def test_failed_parser_records_failure_without_exposing_error_in_stage(tmp_path, ingestion, monkeypatch) -> None:
    from app.ingest_queue import IngestQueueManager

    def fail(*args, **kwargs):
        raise RuntimeError('private parser error')

    monkeypatch.setattr('app.ingestion.document_to_markdown', fail)
    repository, job_id = queued_repository(tmp_path)
    manager = IngestQueueManager(repository, lambda _: SimpleNamespace(ingestion=ingestion), max_retries=0)
    manager._process(repository.claim(1)[0])
    stage = [event for event in repository.events(job_id)['items'] if event['phase'] == 'parse']
    assert [event['status'] for event in reversed(stage)] == ['running', 'failed']
    assert stage[0]['duration_ms'] is not None
    assert 'private parser error' not in stage[0]['message']
    assert repository.get(job_id).items[0].status == 'failed'


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


def test_reparse_polls_index_after_restart_without_reparsing_again(tmp_path) -> None:
    from app.ingest_queue import IngestQueueManager

    repository = IngestQueueRepository(tmp_path / "queue.db")
    job = repository.create_reparse("default", [SimpleNamespace(source_id="src_test", filename="doc.txt")], "alice")
    ingestion = PollingIngestion(source("processing"), source("success"))
    calls = []
    def reparse(source_id, source_platform_user):
        calls.append((source_id, source_platform_user))
        return ingestion.source
    ingestion.reparse_source = reparse
    manager = IngestQueueManager(repository, lambda _agent_id: SimpleNamespace(ingestion=ingestion))
    manager._process(repository.claim(1)[0])
    with repository.connect() as connection:
        connection.execute("UPDATE ingest_job_items SET next_attempt_at=0 WHERE job_id=?", (job.job_id,))
    recovered = IngestQueueRepository(repository.db_path)
    manager = IngestQueueManager(recovered, lambda _agent_id: SimpleNamespace(ingestion=ingestion))
    manager._process(recovered.claim(1)[0])
    assert recovered.get(job.job_id).items[0].status == "success"
    assert calls == [("src_test", "alice")]
    assert ingestion.refresh_calls == 1


def test_reindex_polls_after_restart_without_resubmitting(tmp_path) -> None:
    from app.ingest_queue import IngestQueueManager
    repository = IngestQueueRepository(tmp_path / 'queue.db')
    job = repository.create_reparse('default', [SimpleNamespace(source_id='src_test', filename='doc.txt')], 'alice', 'document_reindex')
    ingestion = PollingIngestion(source('processing'), source('success'))
    calls = []
    ingestion.reindex_source = lambda source_id: calls.append(source_id) or ingestion.source
    manager = IngestQueueManager(repository, lambda _agent_id: SimpleNamespace(ingestion=ingestion))
    manager._process(repository.claim(1)[0])
    with repository.connect() as connection:
        connection.execute('UPDATE ingest_job_items SET next_attempt_at=0 WHERE job_id=?', (job.job_id,))
    recovered = IngestQueueRepository(repository.db_path)
    manager = IngestQueueManager(recovered, lambda _agent_id: SimpleNamespace(ingestion=ingestion))
    manager._process(recovered.claim(1)[0])
    assert recovered.get(job.job_id).items[0].status == 'success'
    assert calls == ['src_test'] and ingestion.refresh_calls == 1


def test_cancel_waits_for_running_item_and_prevents_retry(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    claimed = repository.claim(1)[0]
    assert repository.cancel(job_id).status == "cancelling"
    repository.schedule_retry(claimed["item_id"], "temporary error", 5, "src_test")
    job = repository.get(job_id)
    assert job.status == "cancelled"
    assert job.items[0].status == "cancelled"
    assert repository.task_summaries()[0].status == "CANCELLED"
    assert repository.claim(1) == []
    for action in (repository.start, repository.resume, repository.pause, repository.retry_failed):
        with pytest.raises(ValueError):
            action(job_id)


def test_cancel_queued_items_survives_restart_and_running_results_are_preserved(tmp_path) -> None:
    repository = IngestQueueRepository(tmp_path / "queue.db")
    sources = [SimpleNamespace(source_id=f"src_{i}", filename=f"{i}.txt") for i in range(2)]
    job = repository.create_reparse("default", sources, "alice")
    running = repository.claim(1)[0]
    cancelled = repository.cancel(job.job_id)
    assert cancelled.status == "cancelling"
    assert [item.status for item in cancelled.items] == ["running", "cancelled"]
    repository.finish_item(running["item_id"], "success", running["source_id"], "已完成")
    finished = repository.get(job.job_id)
    assert finished.status == "cancelled"
    assert [item.status for item in finished.items] == ["success", "cancelled"]
    assert IngestQueueRepository(repository.db_path).get(job.job_id).status == "cancelled"


def test_restart_during_cancellation_does_not_resume_unknown_work(tmp_path) -> None:
    repository, job_id = queued_repository(tmp_path)
    repository.claim(1)
    repository.cancel(job_id)
    recovered = IngestQueueRepository(repository.db_path)
    assert recovered.get(job_id).status == "cancelled"
    assert "执行结果未知" in recovered.get(job_id).items[0].message
    assert recovered.claim(1) == []
