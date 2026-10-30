from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.clients.dify import MockDifyClient
from app.db import SourceRepository
from app.ingestion import IngestionService
from app.retrieval import RetrievalTools
from app.schemas import VectorHit


def create_text_source(repository: SourceRepository, tmp_path: Path, name: str, text: str, **metadata):
    source_id = f"src_{uuid4().hex}"
    directory = tmp_path / source_id
    directory.mkdir()
    md_path = directory / "document.md"
    pdf_path = directory / "source.pdf"
    md_path.write_text(text, encoding="utf-8")
    pdf_path.write_bytes(b"pdf")
    return repository.create({
        "source_id": source_id,
        "filename": name,
        "normalized_filename": name.casefold(),
        "file_hash": uuid4().hex,
        "local_pdf_path": str(pdf_path),
        "local_md_path": str(md_path),
        "parse_status": "completed",
        "dify_status": "completed",
        **metadata,
    })


def test_grep_and_read_context(sample_pdf: Path, ingestion: IngestionService, repository: SourceRepository) -> None:
    source, _ = ingestion.ingest(sample_pdf)
    tools = RetrievalTools(repository, MockDifyClient())
    hits = tools.grep_files(["Temperature"], [source.source_id])
    assert len(hits) == 1
    assert hits[0].page == 1
    assert "Pipeline standard" in hits[0].content
    assert "Publish: 2024-01-02" in hits[0].content
    context = tools.read_file_context(source.source_id, page=1)
    assert "-20 C to 50 C" in context.content
    assert context.pdf_url == source.pdf_url


def test_python_search_fallback(sample_pdf: Path, ingestion: IngestionService, repository: SourceRepository, monkeypatch) -> None:
    source, _ = ingestion.ingest(sample_pdf)
    monkeypatch.setattr("app.retrieval.shutil.which", lambda _: None)
    hits = RetrievalTools(repository, MockDifyClient()).grep_files(["pipeline"], [source.source_id])
    assert hits and hits[0].source_id == source.source_id


def test_balanced_matches_prioritize_exact_english_word() -> None:
    matches = [("a.md", 1, "Risk Measurement"), ("a.md", 2, "Measure"), ("a.md", 3, "MEASURE applies")]
    balanced = RetrievalTools._balance_matches(matches, ["Measure"], True, False)
    assert [item[1] for item in balanced] == [2, 3, 1]


def test_grep_returns_twelve_lines_of_context_and_merges_overlaps(
    repository: SourceRepository, tmp_path: Path
) -> None:
    lines = [f"第 {index} 行" for index in range(1, 41)]
    lines[19] = "第 20 行包含 GPR"
    lines[21] = "第 22 行再次出现 GPR"
    source = create_text_source(repository, tmp_path, "gpr.pdf", "\n".join(lines))

    hits = RetrievalTools(repository, MockDifyClient()).grep_files(["GPR"], [source.source_id])

    assert len(hits) == 1
    assert hits[0].line_start == 8
    assert hits[0].line_end == 32
    assert "第 8 行" in hits[0].content
    assert "第 32 行" in hits[0].content


def test_search_within_document_is_scoped(repository: SourceRepository, tmp_path: Path) -> None:
    first = create_text_source(repository, tmp_path, "first.pdf", "<!-- page: 1 -->\n独有词 alpha")
    second = create_text_source(repository, tmp_path, "second.pdf", "<!-- page: 1 -->\n独有词 beta")

    hits = RetrievalTools(repository, MockDifyClient()).search_within_document(first.source_id, "独有词")

    assert hits and {item.source_id for item in hits} == {first.source_id}
    assert second.source_id not in {item.source_id for item in hits}


def test_find_files_matches_exact_identifier_inside_filename(repository: SourceRepository, tmp_path: Path) -> None:
    for index in range(1, 41):
        create_text_source(repository, tmp_path, f"MTR-NS-{index:03d}.pdf", f"文件编号 MTR-NS-{index:03d}")

    tools = RetrievalTools(repository, MockDifyClient())
    exact = tools.find_files("MTR-NS-029", 100)
    family = tools.find_files("MTR-NS", 100)

    assert [item.filename for item in exact] == ["MTR-NS-029.pdf"]
    assert len(family) == 40


def test_metadata_search_filters_year_status_and_dates(repository: SourceRepository, tmp_path: Path) -> None:
    expected = create_text_source(
        repository, tmp_path, "active-2024.pdf", "text", standard_year=2024, status="active",
        publish_date="2024-03-01", effective_date="2024-06-01",
    )
    create_text_source(
        repository, tmp_path, "old.pdf", "text", standard_year=2020, status="abolished",
        publish_date="2020-01-01", effective_date="2020-06-01",
    )

    found = RetrievalTools(repository, MockDifyClient()).metadata_search(
        year=2024, status="active", publish_from="2024-01-01", publish_to="2024-12-31"
    )

    assert [item.source_id for item in found] == [expected.source_id]


def test_document_candidate_search_merges_filename_metadata_and_body(
    repository: SourceRepository, tmp_path: Path
) -> None:
    filename_hit = create_text_source(repository, tmp_path, "管嘴专题会议.pdf", "没有正文关键词")
    metadata_hit = create_text_source(
        repository, tmp_path, "设计协调会.pdf", "没有正文关键词",
        custom_metadata={"meeting_topic": {"value": "管嘴设计协调"}},
    )
    body_hit = create_text_source(repository, tmp_path, "项目周会.pdf", "本次讨论管嘴材料问题")
    create_text_source(repository, tmp_path, "无关会议.pdf", "本次讨论进度安排")

    found = RetrievalTools(repository, MockDifyClient()).grep_files(
        ["管嘴"], include_document_fields=True
    )

    assert {item.source_id for item in found} == {
        filename_hit.source_id, metadata_hit.source_id, body_hit.source_id,
    }
    by_source = {item.source_id: item.content for item in found}
    assert "文件名" in by_source[filename_hit.source_id]
    assert "元数据:meeting_topic" in by_source[metadata_hit.source_id]
    assert "正文" in by_source[body_hit.source_id]


def test_document_candidate_search_requires_document_level_keyword_intersection(
    repository: SourceRepository, tmp_path: Path
) -> None:
    both = create_text_source(repository, tmp_path, "RNS专题会.pdf", "本次会议讨论主泵。")
    create_text_source(repository, tmp_path, "RNS其他会议.pdf", "本次会议讨论管道。")
    create_text_source(repository, tmp_path, "泵组会议.pdf", "本次会议不涉及 RNS。".replace(" RNS", ""))

    found = RetrievalTools(repository, MockDifyClient()).grep_files(
        ["RNS", "泵"], include_document_fields=True, match_mode="all", search_scope="all"
    )

    assert [item.source_id for item in found] == [both.source_id]


def test_document_candidate_filename_scope_does_not_match_body_only(
    repository: SourceRepository, tmp_path: Path
) -> None:
    filename_hit = create_text_source(repository, tmp_path, "泵专题会.pdf", "其他内容")
    create_text_source(repository, tmp_path, "工艺会.pdf", "正文讨论了泵。")

    found = RetrievalTools(repository, MockDifyClient()).grep_files(
        ["泵"], include_document_fields=True, match_mode="all", search_scope="filename"
    )

    assert [item.source_id for item in found] == [filename_hit.source_id]


def test_metadata_search_sorts_only_candidate_sources_by_text_date(
    repository: SourceRepository, tmp_path: Path
) -> None:
    older = create_text_source(
        repository, tmp_path, "旧会议.pdf", "管嘴",
        custom_metadata={"meeting_date": {"value": "2026-9-30"}},
    )
    newer = create_text_source(
        repository, tmp_path, "新会议.pdf", "管嘴",
        custom_metadata={"meeting_date": {"value": "2026年10月1日"}},
    )
    create_text_source(
        repository, tmp_path, "全库但非候选.pdf", "无关",
        custom_metadata={"meeting_date": {"value": "2026-12-31"}},
    )

    found = RetrievalTools(repository, MockDifyClient()).metadata_search(
        custom_sort={"field": "meeting_date", "direction": "desc"},
        limit=1,
        source_ids=[older.source_id, newer.source_id],
    )

    assert [item.source_id for item in found] == [newer.source_id]


def test_metadata_search_filters_custom_date_range(
    repository: SourceRepository, tmp_path: Path
) -> None:
    expected = create_text_source(
        repository, tmp_path, "2026年会议.pdf", "管道等级表",
        custom_metadata={"meeting_date": {"value": "2026年9月1日"}},
    )
    create_text_source(
        repository, tmp_path, "2025年会议.pdf", "管道等级表",
        custom_metadata={"meeting_date": {"value": "2025-12-31"}},
    )
    create_text_source(
        repository, tmp_path, "无日期会议.pdf", "管道等级表",
        custom_metadata={},
    )

    found = RetrievalTools(repository, MockDifyClient()).metadata_search(
        custom_date_ranges={
            "meeting_date": {"from": "2026-01-01", "to": "2026-12-31"},
        }
    )

    assert [item.source_id for item in found] == [expected.source_id]


def test_keyword_and_filename_tools_obey_empty_or_populated_scope(
    repository: SourceRepository, tmp_path: Path
) -> None:
    allowed = create_text_source(repository, tmp_path, "管道等级表-2026.pdf", "管道等级表")
    create_text_source(repository, tmp_path, "管道等级表-2025.pdf", "管道等级表")
    tools = RetrievalTools(repository, MockDifyClient())

    assert tools.find_files("管道等级表", 20, []) == []
    assert tools.keyword_search("管道等级表", source_ids=[]) == []
    assert [item.source_id for item in tools.find_files(
        "管道等级表", 20, [allowed.source_id]
    )] == [allowed.source_id]


def test_keyword_search_combines_filename_metadata_body_and_sorts(
    repository: SourceRepository, tmp_path: Path
) -> None:
    old = create_text_source(
        repository, tmp_path, "旧管道等级表会议.pdf", "讨论管道等级表",
        custom_metadata={"meeting_date": {"value": "2026-09-01"}},
    )
    new = create_text_source(
        repository, tmp_path, "新会议.pdf", "讨论其他事项",
        custom_metadata={
            "meeting_topic": {"value": "管道等级表"},
            "meeting_date": {"value": "2026年10月15日"},
        },
    )
    unrelated = create_text_source(repository, tmp_path, "普通管道会议.pdf", "讨论管道布置")

    found = RetrievalTools(repository, MockDifyClient()).keyword_search(
        "管道等级表", "meeting_date", "desc", 20
    )

    assert [item.source_id for item in found] == [new.source_id, old.source_id]
    assert unrelated.source_id not in {item.source_id for item in found}
    assert all(item.retrieval_type == "keyword_ranked" for item in found)


def test_read_pages_and_document_outline(repository: SourceRepository, tmp_path: Path) -> None:
    source = create_text_source(
        repository, tmp_path, "plan.pdf",
        "<!-- page: 1 -->\n一、活动名称\n篮球赛\n二、活动目的\n交流\n"
        "<!-- page: 2 -->\n三、预算概算\n合计 9000 元\n",
    )
    tools = RetrievalTools(repository, MockDifyClient())

    pages = tools.read_pages(source.source_id, [1, 2])
    outline = tools.get_document_outline(source.source_id)

    assert [item.page for item in pages] == [1, 2]
    assert "篮球赛" in pages[0].content and "9000" in pages[1].content
    assert outline and "一、活动名称（PDF 第 1 页）" in outline[0].content
    assert "三、预算概算（PDF 第 2 页）" in outline[0].content


def test_get_adjacent_chunks_around_vector_hit(repository: SourceRepository, tmp_path: Path) -> None:
    source = create_text_source(repository, tmp_path, "chunks.pdf", "text", dify_document_id="doc-1")

    class FakeDify:
        def retrieve(self, query, top_k):
            return [VectorHit(document_id="doc-1", content="middle", segment_id="seg-2", score=0.9)]

        def get_segments(self, document_id, keyword="", page=1, limit=20):
            assert document_id == "doc-1"
            return [
                {"id": "seg-1", "content": "before"},
                {"id": "seg-2", "content": "middle"},
                {"id": "seg-3", "content": "after"},
            ]

    tools = RetrievalTools(repository, FakeDify())
    vector_hit = tools.vector_search("middle")[0]
    adjacent = tools.get_adjacent_chunks(source.source_id, vector_hit.segment_id or "")

    assert vector_hit.segment_id == "seg-2"
    assert [item.segment_id for item in adjacent] == ["seg-1", "seg-2", "seg-3"]
    assert [item.content for item in adjacent] == ["before", "middle", "after"]


def test_compare_document_metadata(repository: SourceRepository, tmp_path: Path) -> None:
    first = create_text_source(
        repository, tmp_path, "first.pdf", "text", standard_no="GB/T 1-2020", standard_year=2020,
        status="abolished", replaced_by="GB/T 1-2024",
    )
    second = create_text_source(
        repository, tmp_path, "second.pdf", "text", standard_no="GB/T 1-2024", standard_year=2024,
        status="active", replaces="GB/T 1-2020",
    )

    compared = RetrievalTools(repository, MockDifyClient()).compare_document_metadata(
        [first.source_id, second.source_id]
    )

    assert len(compared) == 2
    assert "GB/T 1-2020" in compared[0].content and "被替代=GB/T 1-2024" in compared[0].content
    assert "代替=GB/T 1-2020" in compared[1].content and "Dify状态=completed" in compared[1].content
