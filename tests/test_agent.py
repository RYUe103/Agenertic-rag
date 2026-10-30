from __future__ import annotations

from pathlib import Path
import json
from uuid import uuid4

from app.agent import AgentService
from app.clients.dify import MockDifyClient
from app.clients.llm import LLMClient
from app.db import SourceRepository
from app.ingestion import IngestionService
from app.retrieval import RetrievalTools
from app.schemas import AskHistoryMessage, CitationCheck, EvidenceGrade, GlossaryDocument, MetadataFieldDefinition, ToolCall


def agent(repository: SourceRepository, max_calls: int = 6, llm=None) -> AgentService:
    tools = RetrievalTools(repository, MockDifyClient())
    return AgentService(tools, llm or LLMClient("", "", ""), max_calls)


class FileIntentLLM:
    configured = True

    def __init__(self) -> None:
        self.answer_calls = 0
        self.received_answer_prompt = ""

    def plan(self, question, available_tools, planner_prompt=""):
        if "GB/T 1234-2024" in question:
            return ToolCall(name="find_files", arguments={
                "query": "GB/T 1234-2024", "intent": "read_document",
                "next_after_found": "read_full_document",
            })
        if "MTR-NS-029说的什么" in question:
            return ToolCall(name="find_files", arguments={
                "query": "MTR-NS-029", "intent": "read_document",
                "next_after_found": "read_full_document",
            })
        if "MTR-NS 这些文件有哪些？" in question:
            return ToolCall(name="find_files", arguments={
                "query": "MTR-NS", "intent": "list_documents",
                "next_after_found": "answer_file_list",
            })
        if "哪些文件提到了 Temperature？" in question:
            return ToolCall(name="grep_files", arguments={
                "query": "Temperature", "intent": "list_documents",
            })
        return ToolCall(name="vector_search", arguments={"query": question})

    def grade_evidence(self, question, evidences):
        return EvidenceGrade(relevant=bool(evidences), sufficient=bool(evidences), reason="测试证据")

    def rewrite_query(self, question, current_query, reason, missing_topics):
        return current_query

    def answer(self, question, evidences, answer_prompt=""):
        self.answer_calls += 1
        self.received_answer_prompt = answer_prompt
        return f"文件全文显示：{evidences[0].content} [{evidences[0].evidence_id}]"

    def verify_answer(self, question, answer, evidences):
        return CitationCheck(supported=True, reason="引用一致")


def create_named_source(repository: SourceRepository, tmp_path: Path, filename: str, content: str):
    source_id = f"src_{uuid4().hex}"
    directory = tmp_path / source_id
    directory.mkdir()
    md_path = directory / "document.md"
    md_path.write_text(content, encoding="utf-8")
    return repository.create({
        "source_id": source_id,
        "filename": filename,
        "normalized_filename": filename.casefold(),
        "file_hash": uuid4().hex,
        "local_pdf_path": str(md_path),
        "local_md_path": str(md_path),
        "parse_status": "success",
        "dify_status": "success",
        "pdf_platform_status": "success",
    })


def test_explicit_standard_route(sample_pdf, ingestion: IngestionService, repository: SourceRepository) -> None:
    ingestion.ingest(sample_pdf)
    result = agent(repository, llm=FileIntentLLM()).ask("GB/T 1234-2024 对 Temperature 有什么要求？")
    assert result.evidences
    assert result.evidences[0].evidence_id == "ev_001"
    assert result.tool_calls <= 6
    assert "[ev_" not in result.answer
    assert "### 引用来源" in result.answer
    assert "](</api/files/" in result.answer


def test_ask_returns_only_cited_http_sources(repository: SourceRepository, monkeypatch) -> None:
    from app.schemas import Evidence

    service = agent(repository)
    evidences = [
        Evidence(evidence_id="ev_001", source_id="safe", filename="原文.pdf", content="证据",
                 retrieval_type="local_context", page=2, pdf_url="https://pdf.example/files/safe"),
        Evidence(evidence_id="ev_002", source_id="unused", filename="未引用.pdf", content="证据",
                 retrieval_type="local_context", pdf_url="https://pdf.example/files/unused"),
        Evidence(evidence_id="ev_003", source_id="unsafe", filename="危险.pdf", content="证据",
                 retrieval_type="local_context", pdf_url="javascript:alert(1)"),
    ]
    monkeypatch.setattr(service, "_execute", lambda *args, **kwargs: {
        "answer": "结论 [ev_001] [ev_003]", "evidences": evidences,
    })
    result = service.ask("问题")
    assert len(result.sources) == 1
    assert result.sources[0].source_id == "safe"
    assert result.sources[0].url == "https://pdf.example/files/safe"
    assert result.sources[0].pages == [2]
    assert "[ev_" not in result.answer


def test_source_appendix_deduplicates_files_and_rejects_unsafe_urls() -> None:
    from app.schemas import Evidence

    evidences = [
        Evidence(
            evidence_id="ev_001", source_id="safe", filename="预算 [终版].pdf", page=2,
            content="预算", retrieval_type="local_context", pdf_url="/api/files/file one",
        ),
        Evidence(
            evidence_id="ev_002", source_id="safe", filename="预算 [终版].pdf", page=3,
            content="明细", retrieval_type="local_context", pdf_url="/api/files/file one",
        ),
        Evidence(
            evidence_id="ev_003", source_id="unsafe", filename="unsafe.pdf",
            content="忽略", retrieval_type="vector", pdf_url="javascript:alert(1)",
        ),
    ]

    answer = AgentService._append_source_links(
        "结论 [ev_001]、[ev_002]、[ev_003]", evidences,
        ["ev_001", "ev_002", "ev_003"], "https://rag.example/base/",
    )

    assert answer.count("预算 \\[终版\\].pdf") == 1
    assert "第 2、3 页" in answer
    assert "https://rag.example/base/api/files/file%20one" in answer
    assert "javascript:" not in answer
    assert "[\\[1\\]]" not in answer
    assert "[ev_" not in AgentService._strip_evidence_markers(answer)
    assert "[预算 \\[终版\\].pdf（第 2、3 页）](<https://rag.example/base/api/files/file%20one>)" in answer


def test_source_appendix_collapses_model_numeric_and_adjacent_same_file_citations() -> None:
    from app.schemas import Evidence

    evidences = [
        Evidence(
            evidence_id="ev_001", source_id="same", filename="同一文件.pdf",
            content="证据一", retrieval_type="local_context", pdf_url="https://pdf.example/same",
        ),
        Evidence(
            evidence_id="ev_002", source_id="same", filename="同一文件.pdf",
            content="证据二", retrieval_type="local_context", pdf_url="https://pdf.example/same",
        ),
    ]

    answer = AgentService._append_source_links(
        "结论 [1]、[ev_001]、[ev_002]", evidences, ["ev_001", "ev_002"],
    )

    assert "[\\[1\\]]" not in answer
    assert "[1]" not in answer
    assert "[同一文件.pdf](<https://pdf.example/same>)" in answer


def test_source_appendix_replaces_model_invented_source_section() -> None:
    from app.schemas import Evidence

    evidences = [Evidence(
        evidence_id="ev_001", source_id="real-source", filename="真实原文.pdf",
        content="真实证据", retrieval_type="local_context", pdf_url="/api/files/real-source",
    )]
    answer = AgentService._append_source_links(
        "结论 [ev_001]\n\n### 引用来源\n- [错误文件](https://wrong.example/fake.pdf)",
        evidences,
        ["ev_001"],
        "http://10.2.108.71:1030/kb/",
    )

    assert "wrong.example" not in answer
    assert "错误文件" not in answer
    assert "http://10.2.108.71:1030/api/files/real-source" not in answer
    assert "[真实原文.pdf](<http://10.2.108.71:1030/kb/api/files/real-source>)" in answer


def test_source_appendix_keeps_one_same_source_citation_per_markdown_block() -> None:
    from app.schemas import Evidence

    evidences = [
        Evidence(
            evidence_id="ev_001", source_id="same", filename="同一文件.pdf",
            content="证据一", retrieval_type="local_context", pdf_url="https://pdf.example/same",
        ),
        Evidence(
            evidence_id="ev_002", source_id="same", filename="同一文件.pdf",
            content="证据二", retrieval_type="local_context", pdf_url="https://pdf.example/same",
        ),
    ]

    answer = AgentService._append_source_links(
        "第一句 [ev_001]，第二句 [ev_002]。\n\n"
        "新段落 [ev_001]。\n\n"
        "- 条目一 [ev_001]\n- 条目二 [ev_002]",
        evidences,
        ["ev_001", "ev_002"],
    )

    body = answer.split("\n\n### 引用来源\n", 1)[0]
    assert "[\\[1\\]]" not in answer
    assert "[ev_" not in body
    assert "[同一文件.pdf](<https://pdf.example/same>)" in answer


def test_glossary_expands_synonyms_for_exact_search(repository: SourceRepository, tmp_path) -> None:
    create_named_source(repository, tmp_path, "设备接口会议.pdf", "会议讨论了 Nozzle 的接口布置。")

    class GlossaryLLM(FileIntentLLM):
        def expand_query_with_glossary(self, question, documents):
            assert documents[0].name == "行业字典.md"
            return {
                "matches": [{
                    "term": "管嘴", "canonical": "管口", "synonyms": ["Nozzle"],
                    "related_terms": ["法兰"], "definition": "设备连接接口", "source": "行业字典.md",
                }],
                "search_terms": ["管嘴", "管口", "Nozzle"],
            }

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="grep_files", arguments={"query": "管嘴", "intent": "list_documents"})

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), GlossaryLLM(),
        glossary_documents=[GlossaryDocument(name="行业字典.md", markdown="# 管嘴\n同义词：Nozzle")],
    )
    result = service.ask("关于管嘴的会议有哪些？")

    assert "设备接口会议.pdf" in result.answer
    assert any("行业字典扩展检索词：管嘴、管口、Nozzle" in step for step in result.process)
    assert all(item.filename != "行业字典.md" or item.retrieval_type == "glossary" for item in result.evidences)


def test_visible_answer_strips_internal_evidence_markers() -> None:
    answer = AgentService._strip_evidence_markers("## 结论\n\n结果正确 [ev_001]，另见 [EV_002]。")
    assert answer == "## 结论\n\n结果正确，另见。"


def test_complete_list_uses_full_grep(sample_pdf, ingestion: IngestionService, repository: SourceRepository) -> None:
    ingestion.ingest(sample_pdf)
    result = agent(repository, llm=FileIntentLLM()).ask("哪些文件提到了 Temperature？")
    assert "共找到 1 份" in result.answer
    assert any("grep_files 返回" in step for step in result.process)


def test_exact_filename_route_reads_one_document_not_twenty_candidates(
    repository: SourceRepository, tmp_path
) -> None:
    for index in range(1, 41):
        create_named_source(repository, tmp_path, f"MTR-NS-{index:03d}.pdf", f"# MTR-NS-{index:03d}\n唯一内容 {index}")
    result = agent(repository, llm=FileIntentLLM()).ask("MTR-NS-029说的什么")
    assert any("find_files 定位到 1 份候选文件" in step for step in result.process)
    assert any("继续调用 read_full_document" in step for step in result.process)
    assert len(result.evidences) == 1
    assert result.evidences[0].retrieval_type == "local_full_document"
    assert result.tool_calls == 2
    assert not any("调用 vector_search" in step for step in result.process)


def test_family_filename_route_lists_all_matching_documents(repository: SourceRepository, tmp_path) -> None:
    for index in range(1, 41):
        create_named_source(repository, tmp_path, f"MTR-NS-{index:03d}.pdf", f"唯一内容 {index}")
    result = agent(repository, llm=FileIntentLLM()).ask("MTR-NS 这些文件有哪些？")
    assert "共找到 40 份" in result.answer
    assert any("find_files 定位到 40 份候选文件" in step for step in result.process)


def test_retrieve_returns_evidence_without_calling_answer_model(repository: SourceRepository, tmp_path) -> None:
    create_named_source(repository, tmp_path, "MTR-NS-029.pdf", "# MTR-NS-029\n唯一内容 29")
    llm = FileIntentLLM()
    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), llm,
        knowledge_base_id="meeting-kb", answer_prompt="你是会议纪要助手。",
    )

    result = service.retrieve("MTR-NS-029说的什么", "http://kb.internal/")

    assert llm.answer_calls == 0
    assert result.knowledge_base_id == "meeting-kb"
    assert result.answer_prompt == "你是会议纪要助手。"
    assert len(result.evidences) == 1
    assert result.evidences[0].retrieval_type == "local_full_document"


def test_answer_uses_knowledge_base_prompt(repository: SourceRepository, tmp_path) -> None:
    create_named_source(repository, tmp_path, "MTR-NS-029.pdf", "# MTR-NS-029\n唯一内容 29")
    llm = FileIntentLLM()
    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), llm,
        answer_prompt="你是会议纪要助手。",
    )

    service.ask("MTR-NS-029说的什么")

    assert llm.answer_calls == 1
    assert llm.received_answer_prompt == "你是会议纪要助手。"


def test_chinese_question_keyword_segmentation() -> None:
    keywords = AgentService._keywords("篮球比赛的预算总额是多少？请列出各项费用。")
    assert "篮球" in keywords
    assert "预算" in keywords
    assert "费用" in keywords
    assert "预算" in AgentService._keywords("篮球比赛预算总额是多少？")


def test_vector_failure_falls_back_to_local(sample_pdf, ingestion: IngestionService, repository: SourceRepository) -> None:
    ingestion.ingest(sample_pdf)
    service = agent(repository, llm=LoopLLM())

    class FailingDify:
        def retrieve(self, query, top_k):
            raise RuntimeError("remote unavailable")

    service.tools.dify = FailingDify()
    result = service.ask("What is the Temperature requirement?")
    assert result.evidences
    assert any("vector_search 调用失败" in step for step in result.process)


def test_history_is_included_for_follow_up_planning(repository: SourceRepository) -> None:
    class HistoryAwareLLM:
        configured = True

        def __init__(self) -> None:
            self.planning_question = ""

        def plan(self, question, available_tools, planner_prompt=""):
            self.planning_question = question
            return ToolCall(name="vector_search", arguments={"query": "适用范围"})

        def grade_evidence(self, question, evidences):
            return EvidenceGrade(relevant=False, sufficient=False, reason="无证据")

        def rewrite_query(self, question, current_query, reason, missing_topics):
            return current_query

        def answer(self, question, evidences):
            return "没有找到足够证据。"

        def verify_answer(self, question, answer, evidences):
            return CitationCheck(supported=True, reason="无引用")

    llm = HistoryAwareLLM()
    service = AgentService(
        RetrievalTools(repository, MockDifyClient()),
        llm,
        max_tool_calls=1,
        allowed_tools=["vector_search"],
    )

    service.ask(
        "它的适用范围呢？",
        conversation_id="conversation-a",
        history=[
            AskHistoryMessage(role="user", content="GB/T 1234-2024 有哪些要求？"),
            AskHistoryMessage(role="assistant", content="资料中列出了温度要求。"),
        ],
    )

    assert "GB/T 1234-2024 有哪些要求？" in llm.planning_question
    assert "资料中列出了温度要求。" in llm.planning_question
    assert "当前问题：它的适用范围呢？" in llm.planning_question


class LoopLLM:
    configured = True

    def __init__(self) -> None:
        self.grade_calls = 0

    def plan(self, question, available_tools):
        if "上一轮证据评分" in question:
            return ToolCall(name="grep_files", arguments={
                "query": "Temperature", "intent": "search_content",
            })
        return ToolCall(name="vector_search", arguments={"query": question})

    def grade_evidence(self, question, evidences):
        self.grade_calls += 1
        return EvidenceGrade(
            relevant=bool(evidences),
            sufficient=bool(evidences),
            reason="需要更多证据" if not evidences else "已覆盖问题",
            next_query="Temperature requirement",
        )

    def rewrite_query(self, question, current_query, reason, missing_topics):
        raise AssertionError("问答检索不应再调用独立查询改写模型")

    def answer(self, question, evidences):
        return f"Temperature requirement is supported by the file. [{evidences[0].evidence_id}]"

    def verify_answer(self, question, answer, evidences):
        return CitationCheck(supported=True, reason="citation matches")


def test_langgraph_replans_after_empty_vector_result(
    sample_pdf, ingestion: IngestionService, repository: SourceRepository
) -> None:
    ingestion.ingest(sample_pdf)
    llm = LoopLLM()
    service = AgentService(RetrievalTools(repository, MockDifyClient()), llm, max_tool_calls=6, max_retrieval_rounds=3)

    result = service.ask("What is the Temperature requirement?")

    assert result.evidences
    assert llm.grade_calls == 2
    assert not any("Agent 根据评分改写检索" in step for step in result.process)
    assert any("grep_files 返回" in step for step in result.process)
    assert result.process[-1] == "引用校验通过"
    assert [item.tool_name for item in result.trace if item.kind == "decision"] == ["vector_search", "grep_files"]
    assert any(item.title == "工具实际执行参数" for item in result.trace)
    assert any(item.title == "工具原始返回结果" for item in result.trace)
    assert result.trace[0].tool_name == "vector_search"


def test_langgraph_stops_after_bounded_no_progress(repository: SourceRepository) -> None:
    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), LLMClient("", "", ""),
        max_tool_calls=6, max_retrieval_rounds=3,
    )

    result = service.ask("完全不存在的概念")

    assert result.evidence_insufficient is True
    assert result.tool_calls <= 3
    assert sum("第" in step and "轮决定" in step for step in result.process) == 2
    assert result.trace[-1].kind in {"generation", "verification"}
    assert any(item.kind == "stop" for item in result.trace)


def test_latest_meeting_uses_configured_date_metadata(repository: SourceRepository) -> None:
    class LatestPlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="metadata_search", arguments={
                "query": "", "custom_sort": {"field": "meeting_time", "direction": "desc"}, "limit": 20,
            })

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), LatestPlanner(),
        metadata_fields=[MetadataFieldDefinition(key="meeting_time", label="会议时间", value_type="date")],
    )
    planned = service._plan({
        "question": "最新的会议纪要是什么？", "query": "最新的会议纪要是什么？",
        "process": [], "evidences": [], "candidate_ids": [], "retrieval_round": 0,
    })
    assert planned["action"] == "metadata_search"
    assert planned["action_arguments"] == {
        "query": "", "custom_sort": {"field": "meeting_time", "direction": "desc"}, "limit": 20,
    }


def test_metadata_search_without_constraints_falls_back_to_vector(repository: SourceRepository) -> None:
    class MetadataOnlyLLM:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="metadata_search", arguments={"query": "会议纪要"})

    service = AgentService(RetrievalTools(repository, MockDifyClient()), MetadataOnlyLLM())
    planned = service._plan({
        "question": "会议纪要有哪些？", "query": "会议纪要有哪些？",
        "process": [], "evidences": [], "candidate_ids": [], "retrieval_round": 0,
    })
    assert planned["action"] == "vector_search"


def test_metadata_search_rejects_unconfigured_custom_field(repository: SourceRepository) -> None:
    class InventedMetadataLLM:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="metadata_search", arguments={
                "query": "", "custom_filters": {"meeting_topic": "管嘴"},
            })

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), InventedMetadataLLM(),
        metadata_fields=[MetadataFieldDefinition(key="meeting_time", label="会议时间", value_type="date")],
    )
    planned = service._plan({
        "question": "管嘴的会议有哪些？", "query": "管嘴的会议有哪些？",
        "process": [], "evidences": [], "candidate_ids": [], "retrieval_round": 0,
    })
    assert planned["action"] == "vector_search"
    assert any("忽略未配置的元数据字段：meeting_topic" in step for step in planned["process"])


def test_latest_topic_meeting_searches_candidates_then_sorts(
    repository: SourceRepository, tmp_path: Path
) -> None:
    older = create_named_source(repository, tmp_path, "旧会议.pdf", "讨论管嘴材料")
    newer = create_named_source(repository, tmp_path, "新会议.pdf", "讨论管嘴布置")
    repository.update(older.source_id, custom_metadata={"meeting_date": {"value": "2026-09-01"}})
    repository.update(newer.source_id, custom_metadata={"meeting_date": {"value": "2026-10-15"}})

    class LatestTopicPlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            if "已完成文件名、文本元数据和正文的候选搜索" in question:
                return ToolCall(name="metadata_search", arguments={"query": "", "limit": 1})
            return ToolCall(name="grep_files", arguments={
                "query": "管嘴", "intent": "rank_documents", "next_after_found": "sort_latest",
            })

        def grade_evidence(self, question, evidences):
            return EvidenceGrade(relevant=True, sufficient=True, reason="已找到并排序候选会议")

        def answer(self, question, evidences, answer_prompt=""):
            latest = next(item for item in evidences if item.retrieval_type == "metadata_search")
            return f"最新会议是 {latest.filename} [{latest.evidence_id}]"

        def verify_answer(self, question, answer, evidences):
            return CitationCheck(supported=True, reason="引用一致")

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), LatestTopicPlanner(),
        metadata_fields=[MetadataFieldDefinition(
            key="meeting_date", label="会议日期", value_type="string",
        )],
    )
    result = service.ask("关于管嘴最新的会议是什么？")

    assert "新会议.pdf" in result.answer
    assert any("决定调用 grep_files：管嘴" in step for step in result.process)
    assert any("自动补全候选排序字段：meeting_date desc" in step for step in result.process)
    assert any("决定调用 metadata_search" in step for step in result.process)
    assert result.tool_calls == 2


def test_compound_term_list_does_not_expand_to_broad_terms(
    repository: SourceRepository, tmp_path: Path
) -> None:
    exact = create_named_source(repository, tmp_path, "等级表会议.pdf", "会议审议了管道等级表")
    create_named_source(repository, tmp_path, "普通管道会议.pdf", "会议讨论了管道布置")

    class CompoundTermPlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="grep_files", arguments={
                "query": "管道等级表", "intent": "list_documents",
                "next_after_found": "answer_file_list",
            })

    service = AgentService(RetrievalTools(repository, MockDifyClient()), CompoundTermPlanner())
    result = service.ask("管道等级表的会议有哪些？")

    assert {item.source_id for item in result.evidences} == {exact.source_id}
    assert any("实际关键词：管道等级表" in step for step in result.process)
    assert "普通管道会议.pdf" not in result.answer


def test_compound_chinese_keyword_is_preserved() -> None:
    assert AgentService._grep_keywords("管道等级表") == ["管道等级表"]


def test_document_list_query_extracts_independent_terms() -> None:
    assert AgentService._grep_keywords("RNS 泵相关纪要") == ["RNS", "泵"]


def test_planner_count_parameters_control_document_limit() -> None:
    assert AgentService._planner_list_limit({
        "sort_direction": "desc", "recency_requested": True, "requested_count": None, "count_explicit": False,
    }) == 3
    assert AgentService._planner_list_limit({
        "sort_direction": "desc", "recency_requested": True, "requested_count": 1, "count_explicit": True,
    }) == 1
    assert AgentService._planner_list_limit({
        "sort_direction": "none", "recency_requested": False, "requested_count": 100, "count_explicit": True,
    }) == 100
    assert AgentService._planner_list_limit({
        "sort_direction": "desc", "recency_requested": False, "requested_count": None, "count_explicit": False,
    }) is None


def test_ordinary_related_minutes_return_all_matches(
    repository: SourceRepository, tmp_path: Path
) -> None:
    expected = []
    for index in range(1, 6):
        source = create_named_source(repository, tmp_path, f"RNS泵会议{index}.pdf", "RNS系统泵设计讨论")
        repository.update(source.source_id, custom_metadata={"meeting_date": {"value": f"2026-10-{index:02d}"}})
        expected.append(source.source_id)

    class OrdinaryListPlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="grep_files", arguments={
                "query": "RNS 泵", "keywords": ["RNS", "泵"], "intent": "list_documents",
                "next_after_found": "answer_file_list", "search_scope": "all", "match_mode": "all",
                "sort_direction": "desc", "recency_requested": False,
                "requested_count": None, "count_explicit": False,
            })

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), OrdinaryListPlanner(),
        metadata_fields=[MetadataFieldDefinition(key="meeting_date", label="会议日期", value_type="date")],
    )
    result = service.ask("RNS泵相关纪要")

    assert {item.source_id for item in result.evidences} == set(expected)
    assert len(result.evidences) == 5
    assert "共找到 5 份相关文件" in result.answer
    assert any("数量要求=全部" in step for step in result.process)


def test_term_definition_is_answered_from_content_not_as_file_list(
    repository: SourceRepository, tmp_path: Path
) -> None:
    create_named_source(repository, tmp_path, "GPR技术说明.pdf", "GPR是一种用于地下探测的雷达方法。")

    class DefinitionPlanner(FileIntentLLM):
        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="grep_files", arguments={
                "query": "GPR", "keywords": ["GPR"], "intent": "search_content",
                "search_scope": "body", "match_mode": "all", "sort_direction": "none",
                "recency_requested": False, "requested_count": None, "count_explicit": False,
            })

    llm = DefinitionPlanner()
    result = AgentService(RetrievalTools(repository, MockDifyClient()), llm).ask("GPR是什么？")

    assert llm.answer_calls == 1
    assert "GPR是一种用于地下探测的雷达方法" in result.answer
    assert "共找到" not in result.answer
    assert not any("文件清单已由" in step for step in result.process)


def test_bare_domain_term_returns_matching_file_list(
    repository: SourceRepository, tmp_path: Path
) -> None:
    filename_hit = create_named_source(repository, tmp_path, "管道等级表专题会.pdf", "其他内容")
    body_hit = create_named_source(repository, tmp_path, "设计协调会.pdf", "会议审议管道等级表")
    create_named_source(repository, tmp_path, "普通管道会.pdf", "会议讨论管道布置")

    class BareTermPlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="grep_files", arguments={
                "query": "管道等级表", "intent": "list_documents",
            })

    service = AgentService(RetrievalTools(repository, MockDifyClient()), BareTermPlanner())
    result = service.ask("管道等级表")

    assert {item.source_id for item in result.evidences} == {filename_hit.source_id, body_hit.source_id}
    assert "管道等级表专题会.pdf" in result.answer
    assert "设计协调会.pdf" in result.answer
    assert "普通管道会.pdf" not in result.answer
    assert result.tool_calls == 1
    assert result.process[-1] == "文件清单已由确定性文件检索结果确认"


def test_latest_keyword_search_without_explicit_count_defaults_to_three(repository: SourceRepository) -> None:
    class OverBroadLatestPlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="keyword_search", arguments={
                "query": "管道等级表", "keywords": ["管道等级表"],
                    "search_scope": "all", "match_mode": "all", "sort_direction": "desc",
                    "recency_requested": True, "requested_count": None, "count_explicit": False,
            })

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), OverBroadLatestPlanner(),
        metadata_fields=[MetadataFieldDefinition(key="meeting_date", label="会议日期", value_type="string")],
    )
    planned = service._plan({
        "question": "最新的管道等级表", "current_question": "最新的管道等级表",
        "query": "最新的管道等级表", "process": [], "evidences": [],
        "candidate_ids": [], "retrieval_round": 0,
    })

    assert planned["action"] == "keyword_search"
    assert planned["action_arguments"]["direction"] == "desc"
    assert planned["action_arguments"]["limit"] == 3
    assert any("数量=3" in step for step in planned["process"])


def test_latest_chinese_count_and_trace_match_execution(repository: SourceRepository, tmp_path: Path) -> None:
    for index in range(1, 6):
        source = create_named_source(repository, tmp_path, f"泵会议{index}.pdf", "讨论泵")
        repository.update(source.source_id, custom_metadata={"meeting_date": {"value": f"2026-10-{index:02d}"}})

    class Planner(FileIntentLLM):
        def plan(self, question, available_tools, planner_prompt=""):
            count = 1 if "一份" in question else None
            return ToolCall(name="keyword_search", arguments={
                "query": "泵", "keywords": ["泵"], "search_scope": "all", "match_mode": "all",
                "sort_direction": "desc", "recency_requested": True,
                "requested_count": count, "count_explicit": count is not None,
            })

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()), Planner(),
        metadata_fields=[MetadataFieldDefinition(key="meeting_date", label="会议日期", value_type="date")],
    )
    one = service.ask("最新的一份泵相关纪要", conversation_id="one")
    assert len(one.sources) == 0 or len(one.sources) == 1
    assert len(one.evidences) == 1
    assert "泵会议5.pdf" in one.answer
    calls = [item for item in one.trace if item.title == "工具实际执行参数"]
    assert len(calls) == 1
    assert json.loads(calls[0].query)["limit"] == 1
    three = service.ask("最新的泵相关纪要", conversation_id="three")
    assert [item.filename for item in three.evidences] == ["泵会议5.pdf", "泵会议4.pdf", "泵会议3.pdf"]


def test_time_filter_then_topic_search_is_strict_and_scope(
    repository: SourceRepository, tmp_path: Path
) -> None:
    expected = create_named_source(repository, tmp_path, "2026年工艺会.pdf", "本次讨论管道等级表")
    repository.update(
        expected.source_id,
        custom_metadata={"meeting_date": {"value": "2026-09-01"}},
    )
    wrong_year = create_named_source(repository, tmp_path, "2025年工艺会.pdf", "本次讨论管道等级表")
    repository.update(
        wrong_year.source_id,
        custom_metadata={"meeting_date": {"value": "2025-09-01"}},
    )
    unrelated = create_named_source(repository, tmp_path, "2026年普通会.pdf", "本次讨论进度安排")
    repository.update(
        unrelated.source_id,
        custom_metadata={"meeting_date": {"value": "2026-10-01"}},
    )

    class TimeScopePlanner:
        configured = True

        def plan(self, question, available_tools, planner_prompt=""):
            return ToolCall(name="metadata_search", arguments={
                "custom_date_ranges": {
                    "meeting_date": {"from": "2026-01-01", "to": "2026-12-31"},
                },
                "next_query": "管道等级表",
                "next_tool": "grep_files",
                "next_intent": "list_documents",
            })

    service = AgentService(
        RetrievalTools(repository, MockDifyClient()),
        TimeScopePlanner(),
        metadata_fields=[
            MetadataFieldDefinition(key="meeting_date", label="会议日期", value_type="string"),
        ],
    )

    result = service.ask("2026年的管道等级表")

    assert {item.source_id for item in result.evidences} == {expected.source_id}
    assert wrong_year.source_id not in {item.source_id for item in result.evidences}
    assert unrelated.source_id not in {item.source_id for item in result.evidences}
    assert result.tool_calls == 2
    assert any("范围已锁定 2 份文件" in step for step in result.process)
    assert any("已锁定范围内调用 grep_files：管道等级表" in step for step in result.process)
    assert not any("实际关键词：2026" in step for step in result.process)


class MetadataLLM(LoopLLM):
    def plan(self, question, available_tools):
        assert "metadata_search" in available_tools
        return ToolCall(name="metadata_search", arguments={"year": 2024, "status": "active"})


def test_agent_plans_and_executes_metadata_search(
    sample_pdf, ingestion: IngestionService, repository: SourceRepository
) -> None:
    source, _ = ingestion.ingest(sample_pdf)
    repository.update(source.source_id, standard_year=2024, status="active")
    service = AgentService(RetrievalTools(repository, MockDifyClient()), MetadataLLM())

    result = service.ask("列出 2024 年有效的资料")

    assert any("决定调用 metadata_search" in step for step in result.process)
    assert any(item.retrieval_type == "metadata_search" for item in result.evidences)
    assert any(item.tool_name == "metadata_search" for item in result.trace)


def test_evidence_selection_prefers_complete_context() -> None:
    from app.schemas import Evidence

    items = [
        Evidence(source_id="s1", filename="a.pdf", content=f"零散费用 {index}", retrieval_type="local_grep", line_start=index)
        for index in range(20)
    ]
    items.append(Evidence(
        source_id="s1", filename="a.pdf",
        content="预算明细：场地 3000 元；裁判 1500 元；奖品 2500 元；合计 9000 元",
        retrieval_type="local_context", line_start=1,
    ))
    AgentService._dedupe_and_number(items)

    selected = AgentService._select_evidence("预算明细和合计是多少", items, 12)

    assert selected[0].retrieval_type == "local_context"
    assert selected[0].evidence_id == "ev_021"
