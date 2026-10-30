from __future__ import annotations

import json
from pathlib import Path

from app.ingestion import IngestionService
from app.metadata import extract_metadata
from app.clients.pdf_platform import MockPdfPlatformClient
from app.db import SourceRepository
from app.retrieval import RetrievalTools
from app.utils import normalize_filename, normalize_identifier


def test_normalization() -> None:
    assert normalize_identifier(" gb／t  1234—2024 ") == "GB/T1234-2024"
    assert normalize_filename("Ａ B—文 件.PDF") == "ab-文件.pdf"
    assert extract_metadata("JU2000-31231-08 使用说明书.pdf", "").standard_no == "JU2000-31231-08"
    assert extract_metadata("notes.pdf", "Enhanced 17 capabilities").standard_no is None
    assert extract_metadata("NIST.AI.100-1.pdf", "NIST AI 100-1").standard_no == "NIST AI 100-1"


def test_ingest_extracts_pages_and_deduplicates(sample_pdf: Path, ingestion: IngestionService) -> None:
    first, duplicate = ingestion.ingest(sample_pdf)
    assert duplicate is False
    assert first.parse_status == "success"
    assert first.dify_status == "success"
    assert first.pdf_platform_status == "success"
    assert first.standard_no == "GB/T 1234-2024"
    markdown = Path(first.local_md_path).read_text(encoding="utf-8")
    assert "<!-- page: 1 -->" in markdown
    assert "<!-- page: 2 -->" in markdown
    assert "尚未配置 OCR" in markdown
    metadata = json.loads((Path(first.local_pdf_path).parent / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["pages_without_text"] == [2]

    second, duplicate = ingestion.ingest(sample_pdf)
    assert duplicate is True
    assert second.source_id == first.source_id


def test_reuploads_when_dataset_changes(sample_pdf: Path, ingestion: IngestionService) -> None:
    source, _ = ingestion.ingest(sample_pdf)

    class NewDatasetDify:
        dataset_id = "real-dataset"

        def upload_document(self, file_path, name):
            return "real-document"

        def document_info(self, document_id):
            return {"id": document_id, "indexing_status": "completed"}

    ingestion.dify = NewDatasetDify()
    updated = ingestion.retry_failed(source.source_id)
    assert updated.dify_dataset_id == "real-dataset"
    assert updated.dify_document_id == "real-document"


def test_reparse_rebuilds_markdown_and_refreshes_dify(sample_pdf: Path, tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "reparse.db")
    dify = RecordingDify()
    service = IngestionService(repository, tmp_path / "files", dify, RecordingPlatform())
    source, _ = service.ingest(sample_pdf)

    class FakeMinerU:
        fallback_on_error = True

        def parse(self, path, artifact_dir):
            markdown = "<table><tr><td>MinerU会议</td></tr></table>\n"
            (artifact_dir / "mineru-original.md").write_text(markdown, encoding="utf-8")
            return markdown, {"parser_engine": "mineru", "parser_requested": "mineru", "mineru_response_field": "$.md_content"}

    service.mineru = FakeMinerU()
    updated = service.reparse_source(source.source_id)

    assert updated.parse_status == "success"
    assert Path(updated.local_extracted_md_path).read_text(encoding="utf-8").startswith("<table>")
    assert dify.uploads[-1][2].startswith("<table>")
    metadata = json.loads((Path(updated.source_path).parent / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["parser_engine"] == "mineru"


class RecordingDify:
    dataset_id = "recording-dataset"

    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str, str]] = []

    def upload_document(self, file_path, name):
        self.uploads.append((Path(file_path), name, Path(file_path).read_text(encoding="utf-8")))
        return f"doc-{len(self.uploads)}"

    def document_info(self, document_id):
        return {"id": document_id, "indexing_status": "completed"}

    def retry_document(self, document_id, file_path, name):
        self.uploads.append((Path(file_path), name, Path(file_path).read_text(encoding="utf-8")))
        return True


class RecordingPlatform(MockPdfPlatformClient):
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str | None]] = []

    def upload(self, file_path: str, filename: str | None = None):
        self.uploads.append((file_path, filename))
        return super().upload(file_path, filename)


class RewriteLLM:
    configured = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def rewrite_markdown(self, filename: str, markdown: str, user_prompt: str = "") -> str:
        self.calls.append((filename, user_prompt))
        return f"# AI 改写结果\n\n提示词：{user_prompt}\n\n{markdown}"


def test_docx_and_txt_are_normalized_to_markdown_before_dify(
    tmp_path: Path, sample_docx: Path, sample_txt: Path
) -> None:
    repository = SourceRepository(tmp_path / "documents.db")
    dify = RecordingDify()
    platform = RecordingPlatform()
    service = IngestionService(repository, tmp_path / "files", dify, platform)

    docx_source, _ = service.ingest(sample_docx)
    txt_source, _ = service.ingest(sample_txt)

    assert docx_source.document_type == "docx"
    assert docx_source.mime_type.endswith("wordprocessingml.document")
    assert "# 活动预算方案" in Path(docx_source.local_md_path).read_text(encoding="utf-8")
    assert "| 场地 | 5000 元 |" in Path(docx_source.local_md_path).read_text(encoding="utf-8")
    assert txt_source.document_type == "txt"
    assert "<!-- line: 2 -->" in Path(txt_source.local_md_path).read_text(encoding="utf-8")
    assert "交通费用：800 元" in Path(txt_source.local_md_path).read_text(encoding="utf-8")

    assert [path.suffix for path, _, _ in dify.uploads] == [".md", ".md"]
    assert [name for _, name, _ in dify.uploads] == ["活动预算方案.md", "预算说明.md"]
    assert [Path(path).suffix for path, _ in platform.uploads] == [".docx", ".txt"]
    assert [name for _, name in platform.uploads] == ["活动预算方案.docx", "预算说明.txt"]

    tools = RetrievalTools(repository, dify)
    assert tools.grep_files(["12000"], [docx_source.source_id])[0].location == "第 2 段"
    assert tools.grep_files(["交通费用"], [txt_source.source_id])[0].location == "第 2 行"


def test_ai_rewrite_updates_local_markdown_and_dify_for_new_and_duplicate_upload(
    sample_txt: Path, tmp_path: Path
) -> None:
    repository = SourceRepository(tmp_path / "rewrite.db")
    dify = RecordingDify()
    llm = RewriteLLM()
    service = IngestionService(
        repository, tmp_path / "rewrite-files", dify, RecordingPlatform(), llm=llm
    )

    first, duplicate = service.ingest(sample_txt, rewrite_enabled=True, rewrite_prompt="保留全部事实")
    assert duplicate is False
    rewritten = Path(first.local_md_path).read_text(encoding="utf-8")
    assert "AI 改写结果" in rewritten
    assert "保留全部事实" in rewritten
    assert "AI 改写结果" in dify.uploads[0][2]
    assert len(llm.calls) == 1

    second, duplicate = service.ingest(sample_txt, rewrite_enabled=True, rewrite_prompt="再次整理")
    assert duplicate is True
    rewritten_again = Path(second.local_md_path).read_text(encoding="utf-8")
    assert "再次整理" in rewritten_again
    assert "再次整理" in dify.uploads[-1][2]
    assert len(llm.calls) == 2


def test_ai_rewrite_preview_waits_for_confirmation_before_upload(sample_txt: Path, tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "preview.db")
    dify = RecordingDify()
    platform = RecordingPlatform()
    service = IngestionService(
        repository, tmp_path / "preview-files", dify, platform, llm=RewriteLLM()
    )

    source, duplicate = service.ingest(
        sample_txt,
        rewrite_enabled=True,
        rewrite_prompt="按章节整理",
        rewrite_review_required=True,
    )

    assert duplicate is False
    assert source.rewrite_status == "preview_ready"
    assert Path(source.rewrite_preview_path).exists()
    assert not Path(source.local_md_path).exists()
    assert dify.uploads == []
    assert platform.uploads == []
    assert source.dify_document_id is None

    preview = service.get_rewrite_preview(source.source_id)
    assert "交通费用" in preview["original_markdown"]
    assert "AI 改写结果" in preview["preview_markdown"]

    confirmed = service.confirm_rewrite(source.source_id)
    assert confirmed.rewrite_status == "confirmed"
    assert confirmed.dify_status == "success"
    assert confirmed.pdf_platform_status == "success"
    assert len(dify.uploads) == 1
    assert "AI 改写结果" in dify.uploads[0][2]
    assert len(platform.uploads) == 1


def test_preview_can_publish_original_without_uploading_rewrite(sample_txt: Path, tmp_path: Path) -> None:
    repository = SourceRepository(tmp_path / "original.db")
    dify = RecordingDify()
    service = IngestionService(
        repository, tmp_path / "original-files", dify, RecordingPlatform(), llm=RewriteLLM()
    )
    source, _ = service.ingest(sample_txt, rewrite_enabled=True, rewrite_review_required=True)

    published = service.use_original(source.source_id)

    assert published.rewrite_status == "not_requested"
    assert len(dify.uploads) == 1
    assert "AI 改写结果" not in dify.uploads[0][2]


def test_legacy_doc_is_converted_for_parsing_but_original_is_uploaded(
    tmp_path: Path, sample_doc: Path
) -> None:
    repository = SourceRepository(tmp_path / "legacy.db")
    dify = RecordingDify()
    platform = RecordingPlatform()
    source, _ = IngestionService(repository, tmp_path / "legacy-files", dify, platform).ingest(sample_doc)

    assert source.document_type == "doc"
    assert "活动预算方案" in dify.uploads[0][2]
    assert dify.uploads[0][0].suffix == ".md"
    assert Path(platform.uploads[0][0]).suffix == ".doc"
