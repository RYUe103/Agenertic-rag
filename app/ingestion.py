from __future__ import annotations

import json
import shutil
import threading
from app.document_lock import document_lock
from functools import wraps
import uuid
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

from app.clients.pdf_platform import PdfPlatformClient
from app.db import SourceRepository
from app.document_parser import document_info, document_to_markdown
from app.metadata import extract_metadata
from app.ocr import OcrProvider
from app.mineru import MinerUClient
from app.qwenvl import QwenVLClient
from app.schemas import IngestJob, JobItem, MetadataFieldDefinition, Source
from app.utils import normalize_filename, redact_error, sha256_file

logger = logging.getLogger(__name__)
ingestion_event_sink: ContextVar[Callable[[str, str, int | None], None] | None] = ContextVar('ingestion_event_sink', default=None)


@contextmanager
def tracked_stage(phase: str):
    sink = ingestion_event_sink.get()
    if sink is None:
        yield
        return
    started = time.monotonic()
    def record(status: str, duration: int | None) -> None:
        try:
            sink(phase, status, duration)
        except Exception:
            logger.exception('Unable to persist ingestion stage')
    record('running', None)
    try:
        yield
    except Exception:
        record('failed', round((time.monotonic() - started) * 1000))
        raise
    else:
        record('success', round((time.monotonic() - started) * 1000))


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, IngestJob] = {}
        self._lock = threading.Lock()

    def create(self, filenames: list[str]) -> IngestJob:
        job = IngestJob(job_id=f"job_{uuid.uuid4().hex}", total=len(filenames), items=[JobItem(filename=name) for name in filenames])
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> IngestJob:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return self._jobs[job_id].model_copy(deep=True)

    def mutate(self, job_id: str, callback: Callable[[IngestJob], None]) -> None:
        with self._lock:
            callback(self._jobs[job_id])


def document_operation(exclusive: bool = True):
    def decorate(method):
        @wraps(method)
        def locked(self, *args, **kwargs):
            lock_path = self.repository.db_path.resolve().parent / '.document-operations.lock'
            with document_lock(lock_path, exclusive):
                return method(self, *args, **kwargs)
        return locked
    return decorate


class IngestionService:
    def __init__(self, repository: SourceRepository, files_dir: Path, dify: object, pdf_platform: PdfPlatformClient, secrets: list[str] | None = None, ocr: OcrProvider | None = None, llm: object | None = None, metadata_fields: list[MetadataFieldDefinition] | None = None, metadata_ai_enabled: bool = False, metadata_prompt: str = "", workflow: object | None = None, mineru: MinerUClient | None = None, vision: QwenVLClient | None = None, chunking_mode: str = '', chunk_max_tokens: int | None = None, embedding_model: str = ''):
        self.repository = repository
        self.files_dir = files_dir
        self.dify = dify
        self.pdf_platform = pdf_platform
        self.secrets = secrets or []
        self.ocr = ocr
        self.llm = llm
        self.metadata_fields = metadata_fields or []
        self.metadata_ai_enabled = metadata_ai_enabled
        self.metadata_prompt = metadata_prompt
        self.workflow = workflow
        self.mineru = mineru
        self.vision = vision
        self.chunking_mode = chunking_mode
        self.chunk_max_tokens = chunk_max_tokens
        self.embedding_model = embedding_model

    @document_operation()
    def ingest(self, input_path: Path, rewrite_enabled: bool = False, rewrite_prompt: str = "", workflow_enabled: bool = False, rewrite_review_required: bool = False, source_platform_user: str = "sansuokb") -> tuple[Source, bool]:
        file_hash = sha256_file(input_path)
        existing = self.repository.by_hash(file_hash)
        if existing:
            return self.retry_failed(
                existing.source_id,
                rewrite_enabled=rewrite_enabled,
                rewrite_prompt=rewrite_prompt,
                workflow_enabled=workflow_enabled,
                rewrite_review_required=rewrite_review_required, source_platform_user=source_platform_user,
            ), True
        source_id = f"src_{uuid.uuid4().hex}"
        document_type, mime_type = document_info(input_path)
        source_dir = self.files_dir / source_id
        source_dir.mkdir(parents=True, exist_ok=False)
        source_path = source_dir / f"source{input_path.suffix.casefold()}"
        md_path = source_dir / "document.md"
        extracted_md_path = source_dir / "extracted.md"
        with tracked_stage('source_persist'):
            shutil.copy2(input_path, source_path)
        page_count = None
        if document_type == 'pdf':
            try:
                import fitz
                with fitz.open(source_path) as document:
                    page_count = document.page_count
            except Exception:
                logger.warning('Unable to determine page count for %s', source_id, exc_info=True)
        source = self.repository.create({
            "source_id": source_id,
            "filename": input_path.name,
            "normalized_filename": normalize_filename(input_path.name),
            "file_hash": file_hash,
            "document_type": document_type,
            "mime_type": mime_type,
            "file_size": source_path.stat().st_size,
            "page_count": page_count,
            "uploaded_by": source_platform_user or None,
            "local_pdf_path": str(source_path.resolve()),
            "local_source_path": str(source_path.resolve()),
            "local_md_path": str(md_path.resolve()),
            "local_extracted_md_path": str(extracted_md_path.resolve()),
        })
        return self.retry_failed(source.source_id, rewrite_enabled=rewrite_enabled, rewrite_prompt=rewrite_prompt, workflow_enabled=workflow_enabled, rewrite_review_required=rewrite_review_required, source_platform_user=source_platform_user), False

    @document_operation()
    def retry_failed(self, source_id: str, rewrite_enabled: bool = False, rewrite_prompt: str = "", workflow_enabled: bool = False, rewrite_review_required: bool = False, force_reparse: bool = False, source_platform_user: str = "sansuokb", revision_message: str = "", force_reindex: bool = False) -> Source:
        source = self.repository.get(source_id)
        errors: list[str] = []
        # Preserve legacy content and parser artifacts before any overwrite.
        if Path(source.local_md_path).is_file():
            self.repository.documents.backfill(source_id)
        revision_pending = bool(revision_message)
        parser_engine = ""
        # A rewrite or workflow request is an explicit request to rebuild the
        # derived Markdown even when the original file was already parsed.
        if source.parse_status != "success" or rewrite_enabled or workflow_enabled or force_reparse:
            try:
                with tracked_stage('parse'):
                    markdown, parser_metadata = document_to_markdown(
                        Path(source.source_path), source.document_type, self.ocr, self.mineru,
                        self.vision, Path(source.source_path).parent,
                    )
                    if not markdown.strip():
                        raise ValueError("文档中没有可索引的文本")
                extracted_path = Path(source.local_extracted_md_path or source.local_md_path).resolve()
                extracted_path.write_text(markdown, encoding="utf-8")
                final_markdown = markdown
                if workflow_enabled:
                    if self.workflow is None:
                        raise RuntimeError("已勾选 Dify Workflow，但当前知识库尚未配置可用 Workflow")
                    source = self.repository.update(source_id, workflow_status="processing", workflow_error=None)
                    workflow_markdown, workflow_result = self.workflow.run(
                        Path(source.source_path), markdown, source.filename, "", "", ""
                    )
                    workflow_path = extracted_path.parent / "workflow.md"
                    workflow_path.write_text(workflow_markdown, encoding="utf-8")
                    (extracted_path.parent / "workflow-result.json").write_text(json.dumps(workflow_result, ensure_ascii=False, indent=2), encoding="utf-8")
                    source = self.repository.update(source_id, workflow_status="success", workflow_output_path=str(workflow_path.resolve()))
                    final_markdown = workflow_markdown
                vision_is_final = parser_metadata.get("parser_engine") == "qwenvl"
                if rewrite_enabled and not vision_is_final:
                    if self.llm is None or not getattr(self.llm, "configured", False):
                        raise RuntimeError("已勾选 AI 改写，但 LLM 尚未配置")
                    source = self.repository.update(
                        source_id,
                        rewrite_status="processing",
                        rewrite_prompt=rewrite_prompt,
                        rewrite_review_required=rewrite_review_required,
                    )
                    final_markdown = self.llm.rewrite_markdown(source.filename, final_markdown, rewrite_prompt)
                    if not final_markdown.strip():
                        raise RuntimeError("AI 改写返回了空 Markdown")
                    if rewrite_review_required:
                        preview_path = extracted_path.parent / "preview.md"
                        preview_path.write_text(final_markdown, encoding="utf-8")
                        source = self.repository.update(
                            source_id,
                            rewrite_status="preview_ready",
                            rewrite_preview_path=str(preview_path.resolve()),
                        )
                    else:
                        Path(source.local_md_path).write_text(final_markdown, encoding="utf-8")
                        source = self.repository.update(source_id, rewrite_status="confirmed", rewrite_preview_path=None)
                else:
                    Path(source.local_md_path).write_text(final_markdown, encoding="utf-8")
                    source = self.repository.update(
                        source_id,
                        rewrite_status="not_requested" if not vision_is_final else "confirmed",
                        rewrite_prompt="",
                        rewrite_preview_path=None,
                        rewrite_review_required=False,
                    )
                with tracked_stage('metadata_extract'):
                    metadata = extract_metadata(source.filename, markdown)
                source = self.repository.update(source_id, parse_status="success", **metadata.as_dict())
                revision_pending = source.rewrite_status != "preview_ready"
                parser_engine = str(parser_metadata.get("parser_engine") or "")
                self._write_metadata(source, parser_metadata)
                if source.rewrite_status != "preview_ready":
                    self._extract_custom_metadata(source, final_markdown)
            except Exception as exc:
                errors.append(f"本地解析失败: {exc}")
                source = self.repository.update(
                    source_id,
                    parse_status="failed",
                    rewrite_status="failed" if rewrite_enabled else source.rewrite_status,
                )
                parser_error_metadata = getattr(exc, "metadata", None)
                if isinstance(parser_error_metadata, dict):
                    self._write_metadata(source, parser_error_metadata)
        source = self.repository.get(source_id)
        if source.rewrite_status == "preview_ready":
            source = self.repository.update(source_id, error_message=None)
            self._write_metadata(source)
            return source
        # Publish local facts before external requests: an index outage must not lose a version.
        version_published = False
        if source.parse_status == "success" and not revision_pending:
            # Recover a crash after the parsed file/status was saved but before publication.
            try:
                current_markdown = self.repository.documents.version(source_id).markdown
            except KeyError:
                current_markdown = None
            revision_pending = current_markdown != Path(source.local_md_path).read_text(encoding="utf-8")
        if revision_pending and source.parse_status == "success":
            self.repository.save_revision(source, Path(source.local_md_path).read_text(encoding="utf-8"), parser_engine,
                                          revision_message or ("重新解析" if force_reparse else "文档解析"),
                                          self.chunking_mode, self.chunk_max_tokens, self.embedding_model)
            revision_pending = False
            version_published = True
        needs_new_document = (
            source.dify_dataset_id != self.dify.dataset_id
            or not source.dify_document_id
        )
        # Rewriting/workflow processing changes document.md. Force the derived
        # Dify document to be updated even if its previous indexing succeeded.
        needs_dify_refresh = rewrite_enabled or workflow_enabled or force_reparse or force_reindex or version_published
        if needs_new_document or needs_dify_refresh or source.dify_status in {"pending", "failed"}:
            try:
                if source.parse_status != "success" or not Path(source.local_md_path).exists():
                    raise RuntimeError("本地 Markdown 尚未解析成功")
                dify_path = Path(source.local_md_path)
                dify_name = f"{Path(source.filename).stem}.md"
                with tracked_stage('dify_submit'):
                    if needs_new_document:
                        document_id = self.dify.upload_document(dify_path, dify_name)
                    else:
                        document_id = source.dify_document_id
                        reused = self.dify.retry_document(document_id, dify_path, dify_name)
                        if not reused:
                            document_id = self.dify.upload_document(dify_path, dify_name)
                source = self.repository.update(
                    source_id, dify_status="processing", dify_dataset_id=self.dify.dataset_id, dify_document_id=document_id
                )
            except Exception as exc:
                errors.append(f"Dify 上传失败: {exc}")
                source = self.repository.update(source_id, dify_status="failed")
        source = self.repository.get(source_id)
        self._sync_dify_metadata(source)
        if source.dify_document_id and source.dify_dataset_id == self.dify.dataset_id and source.dify_status != "failed":
            try:
                with tracked_stage('dify_status'):
                    info = self.dify.document_info(source.dify_document_id)
                indexing_status = str((info or {}).get("indexing_status", "unknown"))
                if indexing_status == "completed":
                    source = self.repository.update(source_id, dify_status="success")
                elif indexing_status == "error":
                    detail = str((info or {}).get("error") or "未知索引错误")
                    errors.append(f"Dify 索引失败: {detail}")
                    source = self.repository.update(source_id, dify_status="failed")
                else:
                    source = self.repository.update(source_id, dify_status="processing")
            except Exception as exc:
                errors.append(f"Dify 状态查询失败: {exc}")
        if not force_reindex and source.pdf_platform_status != "success":
            try:
                with tracked_stage('source_platform'):
                    try:
                        result = self.pdf_platform.upload(source.source_path, source.filename, source_platform_user)
                    except TypeError:
                        # Keep compatibility with adapters using the original two-argument interface.
                        result = self.pdf_platform.upload(source.source_path, source.filename)
                source = self.repository.update(
                    source_id, pdf_platform_status="success", pdf_platform_id=result.file_id, pdf_url=result.url
                )
            except Exception as exc:
                errors.append(f"文件平台上传失败: {exc}")
                source = self.repository.update(
                    source_id, pdf_platform_status="failed", pdf_url=f"/api/files/{source.source_id}"
                )
        source = self.repository.update(source_id, error_message=redact_error("; ".join(errors), self.secrets) or None)
        self._write_metadata(source)
        with self.repository.connect() as connection:
            self.repository.documents.sync_mapping(connection, source)
        return source

    @document_operation()
    def reindex_source(self, source_id: str) -> Source:
        source = self.repository.get(source_id)
        if source.parse_status != 'success' or source.rewrite_status == 'preview_ready' or not Path(source.local_md_path).is_file():
            raise ValueError('文档没有可用于索引的已确认解析内容')
        return self.retry_failed(source_id, force_reindex=True)

    @document_operation()
    def reparse_source(self, source_id: str, source_platform_user: str = "sansuokb") -> Source:
        source = self.repository.get(source_id)
        rewrite_enabled = source.rewrite_status in {"confirmed", "preview_ready"} and bool(source.rewrite_prompt.strip())
        workflow_enabled = source.workflow_status == "success"
        return self.retry_failed(
            source_id,
            rewrite_enabled=rewrite_enabled,
            rewrite_prompt=source.rewrite_prompt,
            workflow_enabled=workflow_enabled,
            rewrite_review_required=source.rewrite_review_required,
            force_reparse=True, source_platform_user=source_platform_user,
        )

    def get_rewrite_preview(self, source_id: str) -> dict[str, str]:
        source = self.repository.get(source_id)
        if source.rewrite_status != "preview_ready" or not source.rewrite_preview_path:
            raise ValueError("该文件没有待确认的 AI 改写预览")
        original_path = Path(source.local_extracted_md_path)
        preview_path = Path(source.rewrite_preview_path)
        if not original_path.exists() or not preview_path.exists():
            raise ValueError("改写预览文件不存在，请重新生成")
        return {
            "source_id": source.source_id,
            "filename": source.filename,
            "original_markdown": original_path.read_text(encoding="utf-8"),
            "preview_markdown": preview_path.read_text(encoding="utf-8"),
            "rewrite_prompt": source.rewrite_prompt,
            "status": source.rewrite_status,
        }

    @document_operation()
    def regenerate_rewrite_preview(self, source_id: str, rewrite_prompt: str) -> dict[str, str]:
        source = self.repository.get(source_id)
        if self.llm is None or not getattr(self.llm, "configured", False):
            raise RuntimeError("LLM 尚未配置")
        original_path = Path(source.local_extracted_md_path)
        if not original_path.exists():
            raise ValueError("原始 Markdown 不存在，无法重新改写")
        original = original_path.read_text(encoding="utf-8")
        self.repository.update(source_id, rewrite_status="processing", rewrite_prompt=rewrite_prompt)
        try:
            preview = self.llm.rewrite_markdown(source.filename, original, rewrite_prompt)
            if not preview.strip():
                raise RuntimeError("AI 改写返回了空 Markdown")
            preview_path = original_path.parent / "preview.md"
            preview_path.write_text(preview, encoding="utf-8")
            self.repository.update(
                source_id,
                rewrite_status="preview_ready",
                rewrite_preview_path=str(preview_path.resolve()),
                rewrite_review_required=True,
                error_message=None,
            )
        except Exception:
            self.repository.update(source_id, rewrite_status="failed")
            raise
        return self.get_rewrite_preview(source_id)

    @document_operation()
    def confirm_rewrite(self, source_id: str, source_platform_user: str = "sansuokb") -> Source:
        source = self.repository.get(source_id)
        if source.rewrite_status != "preview_ready" or not source.rewrite_preview_path:
            raise ValueError("该文件没有待确认的 AI 改写预览")
        preview_path = Path(source.rewrite_preview_path)
        if not preview_path.exists():
            raise ValueError("改写预览文件不存在，请重新生成")
        final_markdown = preview_path.read_text(encoding="utf-8")
        self.repository.documents.backfill(source_id)
        Path(source.local_md_path).write_text(final_markdown, encoding="utf-8")
        source = self.repository.update(
            source_id,
            rewrite_status="confirmed",
            rewrite_review_required=False,
            dify_status="pending",
            error_message=None,
        )
        self._extract_custom_metadata(source, final_markdown)
        return self.retry_failed(source_id, source_platform_user=source_platform_user, revision_message="确认改写")

    @document_operation()
    def use_original(self, source_id: str, source_platform_user: str = "sansuokb") -> Source:
        source = self.repository.get(source_id)
        original_path = Path(source.local_extracted_md_path)
        if not original_path.exists():
            raise ValueError("原始 Markdown 不存在")
        original = original_path.read_text(encoding="utf-8")
        self.repository.documents.backfill(source_id)
        Path(source.local_md_path).write_text(original, encoding="utf-8")
        source = self.repository.update(
            source_id,
            rewrite_status="not_requested",
            rewrite_review_required=False,
            rewrite_preview_path=None,
            dify_status="pending",
            error_message=None,
        )
        self._extract_custom_metadata(source, original)
        return self.retry_failed(source_id, source_platform_user=source_platform_user, revision_message="使用原始解析内容")

    def _sync_dify_metadata(self, source: Source) -> None:
        """Mirror local extracted fields to Dify when this Dify deployment supports it."""
        if not source.dify_document_id or not source.custom_metadata or not hasattr(self.dify, "update_document_metadata"):
            return
        metadata_list = []
        for key, item in source.custom_metadata.items():
            value = item.value if hasattr(item, "value") else item
            if value in (None, ""):
                continue
            if isinstance(value, list):
                value = ", ".join(str(part) for part in value)
            metadata_list.append({"name": key, "value": str(value)})
        if not metadata_list:
            return
        try:
            self.dify.update_document_metadata(source.dify_document_id, metadata_list)
        except Exception as exc:
            self.repository.update(source.source_id, metadata_error=redact_error(f"Dify 元数据同步失败: {exc}", self.secrets))

    @document_operation(exclusive=True)
    def delete_source(self, source_id: str) -> None:
        source = self.repository.get(source_id)
        remote_document = (
            source.dify_document_id
            and source.dify_dataset_id == getattr(self.dify, "dataset_id", None)
            and not str(source.dify_document_id).startswith("mock-")
        )
        if remote_document and hasattr(self.dify, "delete_document"):
            self.dify.delete_document(source.dify_document_id)
        source_dir = Path(source.source_path).resolve().parent
        files_dir = self.files_dir.resolve()
        if source_dir.parent != files_dir or source_dir.name != source.source_id:
            raise RuntimeError("拒绝删除知识库文件目录之外的路径")
        if source_dir.exists():
            shutil.rmtree(source_dir)
        self.repository.delete(source_id)

    def _extract_custom_metadata(self, source: Source, markdown: str) -> None:
        if not self.metadata_ai_enabled or not self.metadata_fields or self.llm is None:
            return
        try:
            values = self.llm.extract_metadata(source.filename, markdown, self.metadata_fields, self.metadata_prompt)
            self.repository.update(
                source.source_id,
                custom_metadata=values,
                metadata_status="success",
                metadata_error=None,
                metadata_extracted_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            )
        except Exception as exc:
            self.repository.update(source.source_id, metadata_status="failed", metadata_error=redact_error(str(exc), self.secrets))

    @document_operation()
    def refresh_dify_status(self, source_id: str) -> Source:
        source = self.repository.get(source_id)
        if not source.dify_document_id or source.dify_dataset_id != self.dify.dataset_id:
            return source
        try:
            with tracked_stage('dify_status'):
                info = self.dify.document_info(source.dify_document_id)
        except Exception as exc:
            message = redact_error(f"Dify 状态查询失败: {exc}", self.secrets)
            return self.repository.update(source_id, error_message=message)
        indexing_status = str((info or {}).get("indexing_status", "unknown"))
        if indexing_status == "completed":
            return self.repository.update(source_id, dify_status="success", error_message=None)
        if indexing_status == "error":
            detail = str((info or {}).get("error") or "未知索引错误")
            return self.repository.update(source_id, dify_status="failed", error_message=f"Dify 索引失败: {detail}")
        return self.repository.update(source_id, dify_status="processing")

    def _write_metadata(self, source: Source, parser_metadata: dict[str, object] | None = None) -> None:
        path = Path(source.source_path).parent / "metadata.json"
        data = source.model_dump()
        if parser_metadata is not None:
            data.update(parser_metadata)
        elif path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            source_fields = set(Source.model_fields)
            for key, value in previous.items():
                if key not in source_fields:
                    data[key] = value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
