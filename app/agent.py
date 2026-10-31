from __future__ import annotations

import re
import json
import inspect
import logging
import time
from contextvars import ContextVar
from collections.abc import Callable
from collections import defaultdict
from typing import Literal, TypedDict
from urllib.parse import urljoin, urlsplit

from langgraph.graph import END, START, StateGraph

from app.clients.llm import LLMClient
from app.retrieval import RetrievalTools
from app.schemas import (
    AskHistoryMessage, AgentTraceEvent, AskResponse, Evidence, EvidenceGrade,
    GlossaryDocument, MetadataFieldDefinition, RetrievalResponse, RetrievalSource, Source,
)


STOP_WORDS = {
    "什么", "哪些", "怎么", "应该", "要求", "文件", "标准", "内容", "一下", "是否", "关于",
    "比较", "区别", "差异", "最新", "提到", "所有", "其中", "对于", "以及", "这个", "那个",
    "是多少", "多少", "如何", "需要", "请问", "讲了", "介绍", "告诉", "的", "了",
    "列出", "各项",
}

class AgentState(TypedDict, total=False):
    question: str
    current_question: str
    query: str
    route: str
    is_complete: bool
    document_intent: str
    next_after_found: str
    pending_sort_direction: str
    locked_document: bool
    action: str
    action_arguments: dict[str, object]
    candidate_ids: list[str]
    scope_source_ids: list[str]
    scope_locked: bool
    pending_query: str
    pending_tool: str
    pending_intent: str
    evidences: list[Evidence]
    candidate_evidences: list[Evidence]
    process: list[str]
    tool_calls: int
    retrieval_round: int
    no_progress_rounds: int
    used_queries: list[str]
    grade: EvidenceGrade
    answer: str
    retry_needed: bool
    verification_round: int
    evidence_insufficient: bool
    answer_evidence_ids: list[str]
    conversation_id: str
    previous_source_ids: list[str]
    excluded_source_ids: list[str]
    is_follow_up: bool
    evidence_only: bool
    glossary_context: str
    glossary_search_terms: list[str]
    dictionary_matches: list[dict]


from app.glossary import GlossaryIndex

logger = logging.getLogger(__name__)

# Per-request values are propagated through LangGraph's execution context.
agent_execution_context: ContextVar[dict | None] = ContextVar("agent_execution_context", default=None)


class ToolApprovalRequired(RuntimeError):
    def __init__(self, approval_id: str, tool_name: str):
        super().__init__(f"工具 {tool_name} 等待人工审批")
        self.approval_id = approval_id
        self.tool_name = tool_name


class AgentService:
    def __init__(
        self,
        tools: RetrievalTools,
        llm: LLMClient,
        max_tool_calls: int = 6,
        max_retrieval_rounds: int = 3,
        max_evidence_for_answer: int = 12,
        agent_id: str = "default",
        agent_name: str = "默认助手",
        allowed_tools: list[str] | None = None,
        planner_prompt: str = "",
        metadata_fields: list[MetadataFieldDefinition] | None = None,
        knowledge_base_id: str = "default-kb",
        answer_prompt: str = "",
        glossary_documents: list[GlossaryDocument] | None = None,
        answer_llm: LLMClient | None = None,
        code_executor: Callable[[str], str] | None = None,
    ):
        self.tools = tools
        self.llm = llm
        self.answer_llm = answer_llm or llm
        self.max_tool_calls = max(1, max_tool_calls)
        self.max_retrieval_rounds = max(1, max_retrieval_rounds)
        self.max_evidence_for_answer = max(1, max_evidence_for_answer)
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.allowed_tools = set(allowed_tools or [
            "vector_search", "grep_files", "keyword_search", "find_files", "search_within_document", "metadata_search",
            "dify_metadata_search", "search_bm25", "search_hybrid",
            "read_full_document", "get_adjacent_chunks", "get_document_outline",
        ])
        if "grep_files" in self.allowed_tools:
            self.allowed_tools.add("keyword_search")
        self.planner_prompt = planner_prompt
        self.metadata_fields = metadata_fields or []
        self.knowledge_base_id = knowledge_base_id
        self.answer_prompt = answer_prompt or LLMClient.DEFAULT_ANSWER_PROMPT
        self.glossary_documents = glossary_documents or []
        self.glossary_index = GlossaryIndex(self.glossary_documents)
        self.code_executor = code_executor
        self._conversations: dict[str, dict[str, object]] = {}
        self.graph = self._build_graph()

    def ask(
        self,
        question: str,
        source_base_url: str = "",
        conversation_id: str = "default",
        history: list[AskHistoryMessage] | None = None,
    ) -> AskResponse:
        result = self._execute(question, conversation_id, history, evidence_only=False)
        process = result.get("process", [])
        evidences = result.get("evidences", [])
        if result.get("is_complete"):
            answer = self._format_file_list_answer(
                evidences, result.get("answer_evidence_ids", []), source_base_url
            )
        else:
            answer = self._append_source_links(
                result.get("answer", ""), evidences, result.get("answer_evidence_ids", []), source_base_url
            )
        if result.get("dictionary_matches"):
            notices = [" / ".join(item["names"]) for item in result["dictionary_matches"][:10]]
            answer = "> 字典命中（多义项由上下文判断）：" + "；".join(notices) + "\n\n" + answer
        cited_ids = re.findall(r"\[(ev_\d{3,})\]", result.get("answer", ""))
        selected_ids = set(cited_ids or result.get("answer_evidence_ids", []))
        sources: dict[str, RetrievalSource] = {}
        for item in evidences:
            if item.retrieval_type == "glossary":
                continue
            url = self._absolute_source_url(item.pdf_url, source_base_url)
            if item.evidence_id not in selected_ids or not url.startswith(("http://", "https://")):
                continue
            source = sources.setdefault(item.source_id, RetrievalSource(
                source_id=item.source_id, filename=item.filename, url=url,
            ))
            if item.page and item.page not in source.pages:
                source.pages.append(item.page)
            if item.location and item.location not in source.locations:
                source.locations.append(item.location)
        answer = self._strip_evidence_markers(answer)
        return AskResponse(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            answer=answer,
            evidences=evidences,
            sources=list(sources.values()),
            process=process,
            trace=self._build_trace(process),
            tool_calls=result.get("tool_calls", 0),
            evidence_insufficient=result.get("evidence_insufficient", not evidences),
        )

    @staticmethod
    def _format_file_list_answer(
        evidences: list[Evidence], answer_evidence_ids: list[str], source_base_url: str = ""
    ) -> str:
        selected = set(answer_evidence_ids)
        rows: list[str] = []
        seen: set[str] = set()
        for item in evidences:
            if item.retrieval_type == "glossary":
                continue
            if selected and item.evidence_id not in selected:
                continue
            if item.source_id in seen:
                continue
            seen.add(item.source_id)
            filename = str(item.filename).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            url = AgentService._absolute_source_url(item.pdf_url, source_base_url)
            rows.append(f"- [{filename}](<{url}>)" if url else f"- {filename}")
        if not rows:
            return "当前知识库中没有找到匹配文件。"
        return f"共找到 {len(rows)} 份相关文件：\n\n" + "\n".join(rows)

    def retrieve(
        self,
        question: str,
        source_base_url: str = "",
        conversation_id: str = "default",
        history: list[AskHistoryMessage] | None = None,
    ) -> RetrievalResponse:
        result = self._execute(question, conversation_id, history, evidence_only=True)
        process = result.get("process", [])
        evidences = [
            item.model_copy(update={"pdf_url": self._absolute_source_url(item.pdf_url, source_base_url)})
            for item in result.get("evidences", [])
        ]
        grouped: dict[str, RetrievalSource] = {}
        for item in evidences:
            source = grouped.setdefault(item.source_id, RetrievalSource(
                source_id=item.source_id, filename=item.filename, url=item.pdf_url or "",
            ))
            if item.page and item.page not in source.pages:
                source.pages.append(item.page)
            if item.location and item.location not in source.locations:
                source.locations.append(item.location)
        return RetrievalResponse(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            knowledge_base_id=self.knowledge_base_id,
            question=question,
            answer_prompt=self.answer_prompt,
            evidences=evidences,
            sources=list(grouped.values()),
            process=process,
            trace=self._build_trace(process),
            tool_calls=result.get("tool_calls", 0),
            evidence_insufficient=result.get("evidence_insufficient", not evidences),
        )

    def _execute(
        self,
        question: str,
        conversation_id: str,
        history: list[AskHistoryMessage] | None,
        evidence_only: bool,
    ) -> AgentState:
        memory = self._conversations.get(conversation_id, {})
        effective_question = question
        recent_history = list(history or [])[-8:]
        has_conversation_context = bool(recent_history or memory.get("last_question"))
        glossary_context = ""
        glossary_terms: list[str] = []
        glossary_evidences: list[Evidence] = []
        dictionary_matches = self.glossary_index.match(question)
        for index, item in enumerate(dictionary_matches[:30]):
            glossary_evidences.append(Evidence(
                source_id=f"glossary:{self.agent_id}:json:{index}", filename=item["source"],
                content=json.dumps(item, ensure_ascii=False)[:12000], retrieval_type="glossary",
            ))
        legacy_documents = [item for item in self.glossary_documents if item.enabled and item.markdown and not item.entries]
        if legacy_documents and hasattr(self.llm, "expand_query_with_glossary"):
            expansion = self.llm.expand_query_with_glossary(question, legacy_documents)
            if expansion:
                matches = expansion.get("matches") if isinstance(expansion.get("matches"), list) else []
                glossary_terms = [str(item).strip() for item in expansion.get("search_terms", []) if str(item).strip()]
                glossary_terms = list(dict.fromkeys(glossary_terms))[:20]
                notes = []
                for item in matches:
                    if not isinstance(item, dict):
                        continue
                    term = str(item.get("term") or "").strip()
                    canonical = str(item.get("canonical") or "").strip()
                    synonyms = [str(value).strip() for value in item.get("synonyms", []) if str(value).strip()]
                    related = [str(value).strip() for value in item.get("related_terms", []) if str(value).strip()]
                    if term:
                        source_name = str(item.get("source") or "行业术语字典").strip()
                        definition = str(item.get("definition") or "未提供").strip()
                        notes.append(
                            f"{term} 的规范词为 {canonical or term}；同义表达：{'、'.join(synonyms) or '无'}；"
                            f"相关词（不是同义词）：{'、'.join(related) or '无'}；定义：{definition}"
                        )
                        glossary_evidences.append(Evidence(
                            source_id=f"glossary:{self.agent_id}:{len(glossary_evidences) + 1}",
                            filename=source_name,
                            content=(f"术语={term}；规范词={canonical or term}；"
                                     f"同义表达={'、'.join(synonyms) or '无'}；"
                                     f"相关词（非同义）={'、'.join(related) or '无'}；定义={definition}"),
                            retrieval_type="glossary",
                        ))
                if notes:
                    glossary_context = "\n行业字典术语提示（仅用于扩展检索和解释，业务事实仍必须来自知识库证据）：\n" + "\n".join(notes)
        glossary_context += self.glossary_index.context(dictionary_matches)
        if recent_history:
            history_text = "\n".join(
                f"{'用户' if item.role == 'user' else '助手'}：{item.content[:6000]}"
                for item in recent_history
            )
            effective_question = (
                "下面是同一知识库会话的最近历史，仅用于理解当前问题的指代和上下文；"
                "检索和回答仍必须以知识库证据为准。\n"
                f"{history_text}\n当前问题：{question}"
            )
        elif memory.get("last_question"):
            effective_question = (
                f"上一轮问题：{memory['last_question']}\n当前问题：{question}\n"
                "请由 Planner 判断当前问题是否承接上一轮，不要仅因为存在会话历史就强制改变检索工具。"
            )
        result = self.graph.invoke({
            "question": effective_question,
            "current_question": question,
            "query": effective_question,
            "evidences": glossary_evidences,
            "candidate_evidences": glossary_evidences,
            "process": [],
            "tool_calls": 0,
            "retrieval_round": 0,
            "no_progress_rounds": 0,
            "used_queries": [],
            "verification_round": 0,
            "retry_needed": False,
            "conversation_id": conversation_id,
            "previous_source_ids": memory.get("source_ids", []),
            "excluded_source_ids": memory.get("returned_source_ids", []),
            "is_follow_up": has_conversation_context,
            "evidence_only": evidence_only,
            "glossary_search_terms": glossary_terms,
            "glossary_context": glossary_context,
            "dictionary_matches": dictionary_matches,
        })
        evidences = result.get("evidences", [])
        for evidence in evidences:
            evidence.agent_id = self.agent_id
        self._conversations[conversation_id] = {
            "last_question": question,
            "source_ids": list(result.get("candidate_ids") or []),
            "returned_source_ids": list(dict.fromkeys(item.source_id for item in evidences)),
        }
        return result

    @staticmethod
    def _append_source_links(
        answer: str, evidences: list[Evidence], answer_evidence_ids: list[str], source_base_url: str = ""
    ) -> str:
        if not answer:
            return answer
        # Source URLs are system-owned. Discard any source appendix invented by
        # the answer model and rebuild it from the selected repository evidence.
        answer = re.split(
            r"\n#{2,4}\s*(?:引用来源|来源文件|参考来源|资料来源|来源)\s*\n",
            answer,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].rstrip()
        cited_ids = re.findall(r"\[(ev_\d{3})\]", answer)
        selected_ids = list(dict.fromkeys(cited_ids or answer_evidence_ids))
        evidence_by_id = {item.evidence_id: item for item in evidences}
        selected = [evidence_by_id[evidence_id] for evidence_id in selected_ids if evidence_id in evidence_by_id]

        sources: dict[str, dict[str, object]] = {}
        for item in selected:
            url = AgentService._absolute_source_url(item.pdf_url, source_base_url)
            if not url:
                continue
            source = sources.setdefault(
                item.source_id, {"filename": item.filename, "url": url, "pages": [], "locations": []}
            )
            if item.page and item.page not in source["pages"]:
                source["pages"].append(item.page)
            if item.location and item.location not in source["locations"]:
                source["locations"].append(item.location)
        if not sources:
            return answer

        source_numbers = {source_id: index for index, source_id in enumerate(sources, start=1)}
        single_source = len(sources) == 1
        for evidence_id in selected_ids:
            item = evidence_by_id.get(evidence_id)
            source = sources.get(item.source_id) if item else None
            if not source:
                answer = re.sub(rf"\s*\[{re.escape(evidence_id)}\]", "", answer, flags=re.IGNORECASE)
                continue
            if single_source:
                answer = re.sub(rf"\s*\[{re.escape(evidence_id)}\]", "", answer, flags=re.IGNORECASE)
                continue
            number = source_numbers[item.source_id]
            answer = re.sub(
                rf"\[{re.escape(evidence_id)}\]",
                f"[\\[{number}\\]](<{source['url']}>)",
                answer,
                flags=re.IGNORECASE,
            )

        if single_source:
            # Some models invent [1] in addition to the requested ev_NNN marker.
            answer = re.sub(r"\s*\[1\]", "", answer)
            answer = re.sub(r"[ \t]+([，。；：！？,.!?;:])", r"\1", answer)
        else:
            # Multiple evidence chunks can belong to the same file. Keep repeated
            # citations when they support separate statements, but collapse an
            # adjacent citation cluster such as [ev_001]、[ev_002] -> [1].
            for source_id, source in sources.items():
                number = source_numbers[source_id]
                citation = f"[\\[{number}\\]](<{source['url']}>)"
                separator = r"\s*[、，,;；]?\s*"
                answer = re.sub(
                    rf"{re.escape(citation)}(?:{separator}{re.escape(citation)})+",
                    lambda _match, value=citation: value,
                    answer,
                )
                # The answer model is instructed to emit ev_NNN only, but some
                # models also invent a numeric marker before the system citation.
                answer = re.sub(
                    rf"\[{number}\]{separator}{re.escape(citation)}",
                    lambda _match, value=citation: value,
                    answer,
                )

            answer = AgentService._collapse_repeated_source_citations(answer, [
                f"[\\[{source_numbers[source_id]}\\]](<{source['url']}>)"
                for source_id, source in sources.items()
            ])

        rows = []
        for source_id, source in sources.items():
            filename = str(source["filename"]).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            pages = source["pages"]
            locations = source["locations"]
            position_label = (
                f"（第 {'、'.join(str(page) for page in pages)} 页）" if pages
                else f"（{'、'.join(str(location) for location in locations)}）" if locations else ""
            )
            number = source_numbers[source_id]
            if single_source:
                rows.append(f"- [{filename}{position_label}](<{source['url']}>)")
            else:
                rows.append(f"- [\\[{number}\\]](<{source['url']}>) [{filename}{position_label}](<{source['url']}>)")
        return answer.rstrip() + "\n\n### 引用来源\n" + "\n".join(rows)

    @staticmethod
    def _collapse_repeated_source_citations(answer: str, citations: list[str]) -> str:
        """Keep one citation per source in each Markdown paragraph or contiguous list block."""
        blocks = re.split(r"(\n\s*\n)", answer)
        for index in range(0, len(blocks), 2):
            block = blocks[index]
            for citation in citations:
                matches = list(re.finditer(re.escape(citation), block))
                if len(matches) < 2:
                    continue
                for match in reversed(matches[:-1]):
                    block = block[:match.start()] + block[match.end():]
            block = re.sub(r"[ \t]+([，,；;：:！？!?])", r"\1", block)
            block = re.sub(r"[ \t]+\n", "\n", block)
            blocks[index] = block
        return "".join(blocks)

    @staticmethod
    def _strip_evidence_markers(answer: str) -> str:
        """Keep evidence IDs internal after source links and verification are resolved."""
        cleaned = re.sub(r"\s*\[(?:ev_\d{3,}|EV_\d{3,})\]", "", answer)
        cleaned = re.sub(r"[ \t]+([，。；：！？,.!?;:])", r"\1", cleaned)
        return cleaned

    @staticmethod
    def _absolute_source_url(pdf_url: str | None, source_base_url: str) -> str:
        if not pdf_url:
            return ""
        parsed = urlsplit(pdf_url)
        if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
            return ""
        if not parsed.scheme:
            if not pdf_url.startswith("/"):
                return ""
            # source_base_url may include a reverse-proxy prefix such as /kb/.
            # A leading slash in pdf_url must not discard that public prefix.
            pdf_url = urljoin(source_base_url.rstrip("/") + "/", pdf_url.lstrip("/")) if source_base_url else pdf_url
        return pdf_url.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")

    @staticmethod
    def _build_trace(process: list[str]) -> list[AgentTraceEvent]:
        trace: list[AgentTraceEvent] = []
        for sequence, detail in enumerate(process, start=1):
            kind = "observation"
            title = "状态更新"
            status = "success"
            round_no = None
            tool_name = None
            query = None
            evidence_count = None

            decision = re.match(r"Agent 第 (\d+) 轮决定调用 ([a-z_]+)：(.+)", detail)
            tool_result = re.match(r"([a-z_]+) 返回 (\d+) 条新证据，累计 (\d+) 条", detail)
            candidate_result = re.match(r"([a-z_]+) 返回 (\d+) 条新候选结果，候选池累计 (\d+) 条", detail)
            context_result = re.match(r"上下文扩展：([a-z_]+) 返回 (\d+) 条候选证据", detail)
            file_result = re.match(r"find_files 定位到 (\d+) 份候选文件", detail)
            planned_read = re.match(r"Agent 根据文件定位结果继续调用 ([a-z_]+)：(.+)", detail)
            actual_call = re.match(r"实际调用 ([a-z_]+)：(.+)", detail)
            actual_result = re.match(r"实际返回 ([a-z_]+)：(\d+) 条", detail)
            if actual_call:
                kind, title = "tool", "工具实际执行参数"
                tool_name, query = actual_call.group(1), actual_call.group(2)
            elif actual_result:
                kind, title = "tool", "工具原始返回结果"
                tool_name, evidence_count = actual_result.group(1), int(actual_result.group(2))
            elif decision:
                kind, title = "decision", "Agent 选择检索工具"
                round_no, tool_name, query = int(decision.group(1)), decision.group(2), decision.group(3)
            elif tool_result:
                kind, title = "tool", "工具返回结果"
                tool_name, evidence_count = tool_result.group(1), int(tool_result.group(3))
            elif candidate_result:
                kind, title = "tool", "工具返回候选结果"
                tool_name, evidence_count = candidate_result.group(1), int(candidate_result.group(2))
            elif context_result:
                kind, title = "tool", "读取命中上下文"
                tool_name, evidence_count = context_result.group(1), int(context_result.group(2))
            elif file_result:
                kind, title = "tool", "定位候选文件"
                tool_name, evidence_count = "find_files", int(file_result.group(1))
            elif planned_read:
                kind, title = "decision", "Agent 继续读取已定位文件"
                tool_name, query = planned_read.group(1), planned_read.group(2)
            elif detail.startswith("证据评分："):
                kind, title = "evaluation", "证据充分性评分"
                status = "warning" if "不足" in detail else "success"
            elif detail == "已基于检索证据生成回答":
                kind, title = "generation", "生成证据化回答"
            elif detail.startswith("引用校验"):
                kind, title = "verification", "引用一致性校验"
                status = "warning" if "未通过" in detail else "success"
            elif detail.startswith("停止："):
                kind, title, status = "stop", "Agent 停止", "warning"
            elif "调用失败" in detail:
                kind, title, status = "tool", "工具调用失败", "error"
                tool_name = detail.split(" ", 1)[0]
            elif detail.startswith("已识别"):
                kind, title = "route", "问题路由"
            elif detail.startswith("回答证据选择："):
                kind, title = "evaluation", "筛选回答证据"

            trace.append(AgentTraceEvent(
                sequence=sequence,
                kind=kind,
                title=title,
                detail=detail,
                status=status,
                round=round_no,
                tool_name=tool_name,
                query=query,
                evidence_count=evidence_count,
            ))
        return trace

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("plan", self._plan)
        workflow.add_node("retrieve", self._retrieve)
        workflow.add_node("read_planned_document", self._read_planned_document)
        workflow.add_node("ready", lambda state: state)
        workflow.add_node("grade", self._grade)
        workflow.add_node("answer", self._generate)
        workflow.add_node("verify", self._verify)
        workflow.add_edge(START, "plan")
        workflow.add_edge("plan", "retrieve")
        workflow.add_conditional_edges(
            "retrieve", self._after_retrieve,
            {"read_document": "read_planned_document", "replan": "plan", "answer": "ready", "grade": "grade"},
        )
        workflow.add_edge("read_planned_document", "ready")
        workflow.add_conditional_edges("grade", self._after_grade, {"answer": "ready", "retry": "plan"})
        workflow.add_conditional_edges("ready", self._after_ready, {"answer": "answer", "end": END})
        workflow.add_edge("answer", "verify")
        workflow.add_conditional_edges("verify", self._after_verify, {"retry": "plan", "end": END})
        return workflow.compile()

    @staticmethod
    def _after_ready(state: AgentState) -> Literal["answer", "end"]:
        return "end" if state.get("evidence_only", False) else "answer"

    def _invoke_tool(self, state: AgentState, name: str, *args: object, **kwargs: object) -> object:
        context = agent_execution_context.get()
        if context and not context["is_enabled"](name):
            reason = '已被管理员禁用'
            state.setdefault("process", []).append(f"{name} {reason}")
            return []
        if name not in self.allowed_tools and name not in {"read_file_context", "get_source"}:
            state.setdefault("process", []).append(f"{name} 不在当前 Agent 的工具白名单中")
            return []
        if state.get("tool_calls", 0) >= self.max_tool_calls:
            return []
        state["tool_calls"] = state.get("tool_calls", 0) + 1
        started = time.monotonic()
        status = "failed"
        try:
            method = getattr(self.tools, name)
            bound = inspect.signature(method).bind(*args, **kwargs)
            bound.apply_defaults()
            parameters = dict(bound.arguments)
            if context and context.get('requires_approval', lambda _: False)(name):
                consume = context.get('consume_approval')
                if consume is None or not consume(name, parameters):
                    approval_id = context['request_approval'](name, parameters)
                    raise ToolApprovalRequired(approval_id, name)
            parameters = dict(parameters)
            if "source_ids" in parameters:
                scope = parameters["source_ids"]
                parameters["source_ids"] = "全库" if scope is None else {"数量": len(scope), "示例": scope[:5]}
            state.setdefault("process", []).append(
                f"实际调用 {name}：" + json.dumps(parameters, ensure_ascii=False, default=str)
            )
            result = method(*args, **kwargs)
            status = "success"
            count = len(result) if isinstance(result, list) else int(result is not None)
            state["process"].append(f"实际返回 {name}：{count} 条")
            return result
        except ToolApprovalRequired:
            raise
        except Exception:
            state.setdefault("process", []).append(f"{name} 调用失败，已将失败结果交回 Agent 决策")
            return []
        finally:
            if context and name in context["registered_tools"]:
                try:
                    context["record"](name, status, int((time.monotonic() - started) * 1000))
                except Exception:
                    logger.exception("Unable to record internal agent tool call")


    def _plan(self, state: AgentState) -> AgentState:
        query = state.get("query") or state["question"]
        available = [
            name for name in ["vector_search", "grep_files", "keyword_search", "find_files", "metadata_search", "dify_metadata_search"]
            if name in self.allowed_tools
        ]
        context = agent_execution_context.get()
        if context:
            available = [name for name in available if context["is_enabled"](name)]
        code_executor = context["code_executor"] if context else self.code_executor
        if code_executor is not None:
            available.append("execute_python")
        candidate_ids = state.get("candidate_ids") or []
        if candidate_ids:
            available.extend(name for name in ["search_within_document", "read_full_document", "get_document_outline"] if name in self.allowed_tools)
        segment_refs = [
            f"{item.source_id}:{item.segment_id}" for item in state.get("evidences", []) if item.segment_id
        ]
        if segment_refs:
            if "get_adjacent_chunks" in self.allowed_tools:
                available.append("get_adjacent_chunks")
        if context:
            available = [name for name in available if name == "execute_python" or context["is_enabled"](name)]
        pending_query = str(state.get("pending_query") or "").strip()
        if state.get("scope_locked") and pending_query:
            requested_tool = str(state.get("pending_tool") or "grep_files")
            action = requested_tool if requested_tool in available else "grep_files"
            intent = str(state.get("pending_intent") or "list_documents")
            arguments: dict[str, object] = {"query": pending_query}
            if action == "grep_files":
                arguments["intent"] = intent
            process = list(state.get("process", []))
            process.append(
                f"Agent 第 {state.get('retrieval_round', 0) + 1} 轮在已锁定范围内调用 {action}：{pending_query}"
            )
            return {
                "action": action,
                "action_arguments": arguments,
                "query": pending_query,
                "process": process,
                "document_intent": intent if action == "grep_files" else "",
                "next_after_found": "answer_file_list" if intent == "list_documents" else "replan",
                "pending_query": "",
            }
        context = []
        if candidate_ids:
            context.append(f"已知 source_id: {', '.join(candidate_ids[:10])}")
        if segment_refs:
            context.append(f"已有 source_id:segment_id: {', '.join(segment_refs[:10])}")
        if state.get("previous_source_ids"):
            context.append(f"上一轮涉及 source_id: {', '.join(state['previous_source_ids'][:20])}")
        if state.get("excluded_source_ids"):
            context.append(f"上一轮已返回 source_id: {', '.join(state['excluded_source_ids'][:20])}")
        if state.get("grade"):
            grade = state["grade"]
            context.append(
                f"上一轮证据评分：{grade.reason or '未说明原因'}；"
                f"缺少：{'、'.join(grade.missing_topics) or '未指定'}"
            )
        if state.get("used_queries"):
            context.append(f"已使用过的查询：{' | '.join(state['used_queries'][-5:])}")
        if state.get("glossary_context"):
            context.append(str(state["glossary_context"]))
            context.append(
                "行业字典给出的同义词必须与原词一起检索；related_terms 只是相关词，不能声称与原词含义相同。"
                "JSON 字典命中的 term 和 names 都是同义入口；keywords 每个独立概念只保留一个名称，"
                "系统自动扩展组内 OR，概念间 AND。若同名多义，先依据 data 判断；无法判断则不要合并不同义项。"
            )
        if state.get("pending_sort_direction"):
            direction_label = "降序取最新" if state["pending_sort_direction"] == "desc" else "升序取最早"
            context.append(
                f"已完成文件名、文本元数据和正文的候选搜索；下一步必须对这些候选 source_id 使用 "
                f"metadata_search 和 custom_sort（{direction_label}），不得再用主题词做 custom_filters"
            )
        execution_context = agent_execution_context.get()
        if self.metadata_fields and (not execution_context or execution_context['is_enabled']('metadata_search')):
            configured_parts = []
            sources = self.tools.repository.list(10000)
            for field in self.metadata_fields:
                populated = sum(
                    1 for source in sources
                    if source.custom_metadata.get(field.key) is not None
                    and getattr(source.custom_metadata.get(field.key), "value", source.custom_metadata.get(field.key)) not in (None, "")
                )
                configured_parts.append(f"{field.key}（{field.label}/{field.value_type}，已抽取 {populated} 份）")
            context.append(
                f"当前知识库已配置自定义元数据字段（只能使用这些字段）：{'、'.join(configured_parts)}。"
                "只有用户明确表达按其中某个字段筛选或排序时才能使用 metadata_search；正文主题词不得猜成元数据值"
            )
        else:
            context.append("当前知识库未配置自定义元数据字段，不要臆造 custom_filters")
        planning_input = query + (("\n" + "\n".join(context)) if context else "")
        planned = None
        if self.llm.configured:
            parameters = inspect.signature(self.llm.plan).parameters
            planned = (
                self.llm.plan(planning_input, available, self.planner_prompt)
                if len(parameters) >= 3
                else self.llm.plan(planning_input, available)
            )
        fallback = "vector_search" if not available or "vector_search" in available else available[0]
        action = planned.name if planned and planned.name in set(available) else fallback
        arguments: dict[str, object] = dict(planned.arguments) if planned else {"query": query}
        if planned:
            query = str(planned.arguments.get("query") or query)
        process_notes: list[str] = []
        glossary_terms = [str(item) for item in state.get("glossary_search_terms", []) if str(item).strip()]
        if glossary_terms and action in {"grep_files", "keyword_search"}:
            arguments["glossary_terms"] = glossary_terms
            process_notes = [*process_notes, f"行业字典扩展检索词：{'、'.join(glossary_terms)}"]
        elif glossary_terms and action == "vector_search":
            query = "；".join(dict.fromkeys([query, *glossary_terms]))
            arguments["query"] = query
            process_notes = [*process_notes, f"行业字典扩展语义检索词：{'、'.join(glossary_terms)}"]
        if action in {"grep_files", "keyword_search"}:
            keywords = arguments.get("keywords")
            arguments["keywords"] = list(dict.fromkeys(
                str(item).strip() for item in keywords if str(item).strip()
            ))[:20] if isinstance(keywords, list) else [query]
            scope = str(arguments.get("search_scope") or "all").casefold()
            arguments["search_scope"] = scope if scope in {"all", "filename", "title", "body"} else "all"
            mode = str(arguments.get("match_mode") or "all").casefold()
            arguments["match_mode"] = mode if mode in {"all", "any"} else "all"
            sort_direction = str(arguments.get("sort_direction") or "none").casefold()
            arguments["sort_direction"] = sort_direction if sort_direction in {"none", "asc", "desc"} else "none"
            arguments["recency_requested"] = arguments.get("recency_requested") is True
            arguments["requested_count"] = self._planner_requested_count(arguments)
            arguments["count_explicit"] = arguments.get("count_explicit") is True
            if state.get("dictionary_matches"):
                arguments["keyword_groups"] = self.glossary_index.groups(arguments["keywords"], state["dictionary_matches"])
            if action == "keyword_search":
                direction = arguments["sort_direction"]
                arguments["direction"] = direction if direction in {"asc", "desc"} else "desc"
                arguments["limit"] = self._planner_list_limit(arguments) or 100000
                process_notes.append(
                    "Planner 文件清单意图："
                    f"关键词={' AND '.join(arguments['keywords']) if arguments['match_mode'] == 'all' else ' OR '.join(arguments['keywords'])}；"
                    f"范围={arguments['search_scope']}；排序={arguments['sort_direction']}；"
                    f"时间极值={arguments['recency_requested']}；"
                    f"数量={arguments['limit'] if arguments['limit'] != 100000 else '全部'}"
                )
        if action == "metadata_search":
            if candidate_ids and not arguments.get("source_ids"):
                arguments["source_ids"] = candidate_ids
            if state.get("pending_sort_direction") and not (
                isinstance(arguments.get("custom_sort"), dict) and arguments["custom_sort"].get("field")
            ):
                default_sort_field = self._default_metadata_sort_field()
                if default_sort_field:
                    arguments["custom_sort"] = {
                        "field": default_sort_field,
                        "direction": state["pending_sort_direction"],
                    }
                    process_notes.append(
                        f"metadata_search 自动补全候选排序字段：{default_sort_field} "
                        f"{state['pending_sort_direction']}"
                    )
            arguments, rejected_fields = self._sanitize_metadata_arguments(arguments)
            if rejected_fields:
                process_notes.append(
                    f"metadata_search 忽略未配置的元数据字段：{', '.join(rejected_fields)}"
                )
            if not self._has_metadata_constraints(arguments):
                action = fallback
                arguments = {"query": query}
                process_notes.append("metadata_search 未提供有效的元数据筛选或排序条件，已回退 vector_search")
        process = list(state.get("process", []))
        process.extend(process_notes)
        process.append(f"Agent 第 {state.get('retrieval_round', 0) + 1} 轮决定调用 {action}：{query}")
        document_intent = str(arguments.get("intent") or "locate_document") if action in {"find_files", "grep_files"} else ""
        next_after_found = str(arguments.get("next_after_found") or "replan") if action in {"find_files", "grep_files"} else ""
        if action == "find_files":
            process.append(f"Planner 文件意图：{document_intent}；定位后：{next_after_found}")
        return {
            "action": action, "action_arguments": arguments, "query": query,
            "process": process,
            "document_intent": document_intent, "next_after_found": next_after_found,
        }

    def _default_metadata_sort_field(self) -> str | None:
        date_fields = [field.key for field in self.metadata_fields if field.value_type == "date"]
        if len(date_fields) == 1:
            return date_fields[0]
        named_date_fields = [
            field.key for field in self.metadata_fields
            if re.search(r"日期|时间|date|time", f"{field.key} {field.label}", re.IGNORECASE)
        ]
        return named_date_fields[0] if len(named_date_fields) == 1 else None

    @staticmethod
    def _has_metadata_constraints(arguments: dict[str, object]) -> bool:
        custom_filters = arguments.get("custom_filters")
        custom_date_ranges = arguments.get("custom_date_ranges")
        custom_sort = arguments.get("custom_sort")
        fixed = any(arguments.get(key) not in (None, "") for key in (
            "year", "status", "publish_from", "publish_to", "effective_from", "effective_to"
        ))
        return fixed or bool(isinstance(custom_filters, dict) and custom_filters) or bool(
            isinstance(custom_date_ranges, dict) and custom_date_ranges
        ) or bool(
            isinstance(custom_sort, dict) and custom_sort.get("field")
        )

    def _sanitize_metadata_arguments(
        self, arguments: dict[str, object]
    ) -> tuple[dict[str, object], list[str]]:
        sanitized = dict(arguments)
        allowed = {field.key for field in self.metadata_fields}
        rejected: list[str] = []

        filters = arguments.get("custom_filters")
        if isinstance(filters, dict):
            valid_filters = {key: value for key, value in filters.items() if key in allowed}
            rejected.extend(str(key) for key in filters if key not in allowed)
            if valid_filters:
                sanitized["custom_filters"] = valid_filters
            else:
                sanitized.pop("custom_filters", None)

        date_ranges = arguments.get("custom_date_ranges")
        if isinstance(date_ranges, dict):
            date_fields = {
                field.key for field in self.metadata_fields
                if field.value_type == "date"
                or re.search(r"日期|时间|date|time", f"{field.key} {field.label}", re.IGNORECASE)
            }
            valid_ranges = {
                key: value for key, value in date_ranges.items()
                if key in date_fields and isinstance(value, dict)
                and (value.get("from") or value.get("to"))
            }
            rejected.extend(str(key) for key in date_ranges if key not in date_fields)
            if valid_ranges:
                sanitized["custom_date_ranges"] = valid_ranges
            else:
                sanitized.pop("custom_date_ranges", None)

        custom_sort = arguments.get("custom_sort")
        if isinstance(custom_sort, dict) and custom_sort.get("field") not in allowed:
            if custom_sort.get("field"):
                rejected.append(str(custom_sort["field"]))
            sanitized.pop("custom_sort", None)

        return sanitized, list(dict.fromkeys(rejected))

    def _retrieve(self, state: AgentState) -> AgentState:
        pool = list(state.get("candidate_evidences") or state.get("evidences") or [])
        state = {
            **state,
            "process": list(state.get("process", [])),
            "evidences": list(pool),
            "candidate_evidences": list(pool),
        }
        before = len(self._dedupe_and_number(list(pool)))
        query, action = state["query"], state["action"]
        arguments = state.get("action_arguments", {})
        if action == "grep_files":
            list_or_rank = state.get("document_intent") in {"list_documents", "rank_documents"}
            grep_keywords = list(dict.fromkeys([
                *[str(item) for item in arguments.get("keywords", [query]) if str(item).strip()],
                *[str(item) for item in arguments.get("glossary_terms", []) if str(item).strip()],
            ]))[:20]
            for index in range(len(state["process"]) - 1, -1, -1):
                if "决定调用 grep_files：" in state["process"][index]:
                    relation = " OR " if arguments.get("glossary_terms") or not list_or_rank else " AND "
                    state["process"][index] += f"（实际关键词：{relation.join(grep_keywords)}）"
                    break
            result = self._invoke_tool(
                state,
                "grep_files",
                grep_keywords,
                self._scope_ids(state),
                True,
                False,
                100000 if list_or_rank else 40,
                0,
                list_or_rank,
                "any" if arguments.get("glossary_terms") and not arguments.get("keyword_groups") else (
                    "all" if str(arguments.get("match_mode") or ("all" if list_or_rank else "any")).casefold() == "all" else "any"
                ),
                str(arguments.get("search_scope") or "all").casefold(),
                arguments.get("keyword_groups"),
            )
            if isinstance(result, list) and list_or_rank:
                direction = str(arguments.get("sort_direction") or "none")
                effective_direction = direction if direction in {"asc", "desc"} else "desc"
                result = self._sort_document_evidences(result, effective_direction)
                requested_limit = self._planner_list_limit(arguments)
                if requested_limit is not None:
                    result = result[:requested_limit]
                state["process"].append(
                    f"清单排序截取：字段={self._default_metadata_sort_field() or '入库时间'}；方向={direction}；"
                    f"数量要求={requested_limit if requested_limit is not None else '全部'}；保留={len(result)} 份"
                )
            excluded = set(state.get("excluded_source_ids") or []) if arguments.get("exclude_previous_results") is True else set()
            state["evidences"].extend(item for item in result if item.source_id not in excluded)
            state["candidate_ids"] = list(dict.fromkeys(item.source_id for item in result))
            if state.get("document_intent") == "list_documents":
                state["is_complete"] = True
            elif state.get("document_intent") == "rank_documents":
                state["pending_sort_direction"] = (
                    "asc" if state.get("next_after_found") == "sort_earliest" else "desc"
                )
        elif action == "keyword_search":
            sort_field = str(arguments.get("sort_field") or self._default_metadata_sort_field() or "")
            direction = str(arguments.get("direction") or "desc")
            query_terms = [str(item) for item in arguments.get("keywords", [query]) if str(item).strip()]
            glossary_terms = arguments.get("glossary_terms") if isinstance(arguments.get("glossary_terms"), list) else []
            keyword_terms = list(dict.fromkeys([*query_terms, *[str(item) for item in glossary_terms if str(item).strip()]]))
            result = self._invoke_tool(
                state, "keyword_search", query, sort_field, direction,
                self._bounded_int(arguments.get("limit"), 100000, 1, 100000),
                self._scope_ids(state),
                keyword_terms,
                "any" if glossary_terms and not arguments.get("keyword_groups") else str(arguments.get("match_mode") or "all"),
                str(arguments.get("search_scope") or "all").casefold(),
                arguments.get("keyword_groups"),
            )
            state["evidences"].extend(result if isinstance(result, list) else [])
            state["candidate_ids"] = list(dict.fromkeys(item.source_id for item in result)) if isinstance(result, list) else []
            state["is_complete"] = True
        elif action == "find_files":
            sources = self._invoke_tool(state, "find_files", query, 5000, self._scope_ids(state))
            state["candidate_ids"] = [item.source_id for item in sources]
            state["process"].append(f"find_files 定位到 {len(sources)} 份候选文件")
            if state.get("document_intent") == "list_documents":
                state["is_complete"] = True
                state["evidences"].extend(self._source_evidences(sources, "filename"))
            elif not (
                state.get("document_intent") == "read_document"
                and state.get("next_after_found") == "read_full_document"
                and len(sources) == 1
            ):
                state["evidences"].extend(self._source_evidences(sources, "filename"))
        elif action == "metadata_search":
            next_query = str(arguments.get("next_query") or "").strip()
            sources = self._invoke_tool(
                state, "metadata_search", str(arguments.get("query") or ""),
                self._optional_int(arguments.get("year")), self._optional_str(arguments.get("status")),
                self._optional_str(arguments.get("publish_from")), self._optional_str(arguments.get("publish_to")),
                self._optional_str(arguments.get("effective_from")), self._optional_str(arguments.get("effective_to")),
                arguments.get("custom_filters") if isinstance(arguments.get("custom_filters"), dict) else {},
                arguments.get("custom_date_ranges") if isinstance(arguments.get("custom_date_ranges"), dict) else {},
                arguments.get("custom_sort") if isinstance(arguments.get("custom_sort"), dict) else {},
                5000 if next_query else self._bounded_int(arguments.get("limit"), 20, 1, 100),
                arguments.get("source_ids") if isinstance(arguments.get("source_ids"), list) else None,
            )
            state["candidate_ids"] = [item.source_id for item in sources]
            if next_query:
                state["scope_source_ids"] = list(state["candidate_ids"])
                state["scope_locked"] = True
                state["pending_query"] = next_query if sources else ""
                state["pending_tool"] = str(arguments.get("next_tool") or "grep_files")
                state["pending_intent"] = str(arguments.get("next_intent") or "list_documents")
                state["process"].append(
                    f"元数据范围已锁定 {len(sources)} 份文件；后续检索必须与该范围取 AND 交集"
                )
                if not sources:
                    state["is_complete"] = True
            else:
                state["evidences"].extend(self._source_evidences(sources, "metadata_search"))
            if state.get("pending_sort_direction"):
                state["pending_sort_direction"] = ""
        elif action == "dify_metadata_search":
            conditions = arguments.get("metadata_filtering_conditions")
            result = self._invoke_tool(
                state, "dify_metadata_search", str(arguments.get("query") or query),
                conditions if isinstance(conditions, dict) else {},
                self._bounded_int(arguments.get("top_k"), 10, 1, 50),
                self._scope_ids(state),
            )
            excluded = set(state.get("excluded_source_ids") or [])
            state["evidences"].extend(item for item in result if item.source_id not in excluded)
        elif action == "search_within_document":
            source_id = self._source_id(arguments, state)
            result = self._invoke_tool(
                state, "search_within_document", source_id, query,
                self._bounded_int(arguments.get("limit"), 30, 1, 100),
            ) if source_id else []
            state["evidences"].extend(result)
        elif action == "read_full_document":
            source_id = self._source_id(arguments, state)
            result = self._invoke_tool(
                state, "read_full_document", source_id,
                self._bounded_int(arguments.get("max_chars"), 30000, 1000, 100000),
            ) if source_id else []
            if isinstance(result, Evidence):
                state["evidences"].append(result)
        elif action == "get_document_outline":
            source_id = self._source_id(arguments, state)
            result = self._invoke_tool(
                state, "get_document_outline", source_id,
                self._bounded_int(arguments.get("max_items"), 100, 1, 300),
            ) if source_id else []
            state["evidences"].extend(result)
        elif action == "get_adjacent_chunks":
            source_id = self._source_id(arguments, state)
            segment_id = self._optional_str(arguments.get("segment_id"))
            if not segment_id:
                segment_id = next((item.segment_id for item in state["evidences"] if item.segment_id and item.source_id == source_id), None)
            result = self._invoke_tool(
                state, "get_adjacent_chunks", source_id, segment_id,
                self._bounded_int(arguments.get("before"), 1, 0, 5),
                self._bounded_int(arguments.get("after"), 1, 0, 5),
            ) if source_id and segment_id else []
            state["evidences"].extend(result)
        elif action == "execute_python":
            code = str(arguments.get("code") or query).strip()
            context = agent_execution_context.get()
            code_executor = context["code_executor"] if context else self.code_executor
            if code_executor is None:
                state["process"].append("execute_python 未获得当前请求的 Sandbox 授权")
            else:
                state["process"].append("Agent 请求执行受控 Python Sandbox")
                output = code_executor(code)
                state["process"].append("Sandbox 执行结果：" + output[:4000])
                state["is_complete"] = True
        else:
            result = self._invoke_tool(state, "vector_search", query, 10, None, self._scope_ids(state))
            state["evidences"].extend(result)
            state["candidate_ids"] = list(dict.fromkeys(item.source_id for item in result))
        pool_limit = 5000 if state.get("is_complete") and state.get("document_intent") == "list_documents" else 60
        pool = self._dedupe_and_number(state["evidences"])[:pool_limit]
        state["candidate_evidences"] = pool
        state["evidences"] = pool
        added = max(0, len(pool) - before)
        state["retrieval_round"] = state.get("retrieval_round", 0) + 1
        state["no_progress_rounds"] = 0 if added else state.get("no_progress_rounds", 0) + 1
        state["used_queries"] = [*state.get("used_queries", []), query]
        state["process"].append(
            f"{action} 返回 {added} 条新候选结果，候选池累计 {len(pool)} 条（最终回答将另行筛选）"
        )
        if arguments.get("exclude_previous_results") is True and not added:
            state["process"].append("本轮没有找到未重复的新结果，请确认关键词、文件名或筛选条件是否正确")
        return state

    @staticmethod
    def _after_retrieve(state: AgentState) -> Literal["read_document", "replan", "answer", "grade"]:
        if state.get("action") == "metadata_search" and state.get("pending_query"):
            return "replan"
        if (
            state.get("action") == "find_files"
            and state.get("document_intent") == "read_document"
            and state.get("next_after_found") == "read_full_document"
            and len(state.get("candidate_ids") or []) == 1
        ):
            return "read_document"
        if state.get("action") == "grep_files" and state.get("document_intent") == "rank_documents":
            return "replan"
        if state.get("is_complete"):
            return "answer"
        return "grade"

    def _read_planned_document(self, state: AgentState) -> AgentState:
        pool = list(state.get("candidate_evidences") or state.get("evidences") or [])
        state = {
            **state,
            "process": list(state.get("process", [])),
            "evidences": list(pool),
            "candidate_evidences": list(pool),
        }
        source_id = (state.get("candidate_ids") or [None])[0]
        if not source_id:
            return state
        state["process"].append(f"Agent 根据文件定位结果继续调用 read_full_document：{source_id}")
        result = self._invoke_tool(state, "read_full_document", source_id, 100000)
        if isinstance(result, Evidence):
            pool = self._dedupe_and_number([*state["candidate_evidences"], result])[:60]
            state["candidate_evidences"] = pool
            state["evidences"] = pool
            state["locked_document"] = True
            state["process"].append(
                f"read_full_document 返回 1 条全文证据，候选池累计 {len(pool)} 条，已锁定目标文件"
            )
        else:
            state["process"].append("read_full_document 未返回可用全文")
        return state

    def _grade(self, state: AgentState) -> AgentState:
        evidences = self._dedupe_and_number(
            list(state.get("candidate_evidences") or state.get("evidences", []))
        )[:60]
        current_question = state.get("current_question") or state["question"]
        grade = self.llm.grade_evidence(current_question, evidences) if self.llm.configured else None
        if grade is None:
            grade = EvidenceGrade(
                relevant=bool(evidences), sufficient=bool(evidences),
                reason="未配置模型评分器，使用确定性证据检查" if evidences else "未检索到证据",
                next_query=None,
            )
        process = list(state.get("process", []))
        state_insufficient = not grade.sufficient
        process.append(f"证据评分：{'充分' if grade.sufficient else '不足'}（{grade.reason or '无详细原因'}）")
        if not grade.sufficient:
            exhausted = state.get("retrieval_round", 0) >= self.max_retrieval_rounds or state.get("tool_calls", 0) >= self.max_tool_calls
            no_progress = state.get("no_progress_rounds", 0) >= 2
            if exhausted:
                process.append("停止：已达到最大检索轮数或工具调用上限")
            elif no_progress:
                process.append("停止：连续两轮没有获得新证据")
        return {
            "evidences": evidences,
            "candidate_evidences": evidences,
            "grade": grade,
            "evidence_insufficient": state_insufficient,
            "process": process,
        }

    def _after_grade(self, state: AgentState) -> Literal["answer", "retry"]:
        if state["grade"].sufficient:
            return "answer"
        exhausted = state.get("retrieval_round", 0) >= self.max_retrieval_rounds or state.get("tool_calls", 0) >= self.max_tool_calls
        no_progress = state.get("no_progress_rounds", 0) >= 2
        return "answer" if exhausted or no_progress else "retry"

    def _generate(self, state: AgentState) -> AgentState:
        evidence_limit = 5000 if state.get("is_complete") else 60
        evidences = self._dedupe_and_number(
            list(state.get("candidate_evidences") or state.get("evidences", []))
        )[:evidence_limit]
        current_question = state.get("current_question") or state["question"]
        selected = evidences if state.get("is_complete") else self._select_evidence(
            current_question, evidences, self.max_evidence_for_answer
        )
        answer = self._answer(
            current_question + str(state.get("glossary_context") or ""), selected, state.get("is_complete", False),
        )
        if state.get("glossary_context") and not state.get("dictionary_matches"):
            glossary_notice = str(state["glossary_context"]).split("：\n", 1)[-1].strip()
            answer = f"> 术语说明：{glossary_notice.replace(chr(10), '；')}\n\n{answer}"
        process = list(state.get("process", []))
        process.append(f"回答证据选择：从 {len(evidences)} 条中选出 {len(selected)} 条")
        process.append("已基于检索证据生成回答")
        return {
            "answer": answer, "evidences": evidences, "candidate_evidences": evidences,
            "answer_evidence_ids": [item.evidence_id for item in selected],
            "process": process, "evidence_insufficient": bool(state.get("evidence_insufficient")) or not evidences,
        }

    def _verify(self, state: AgentState) -> AgentState:
        process = list(state.get("process", []))
        if state.get("is_complete"):
            process.append("文件清单已由确定性文件检索结果确认")
            return {"retry_needed": False, "process": process}
        evidences = state.get("evidences", [])
        answer_ids = set(state.get("answer_evidence_ids", []))
        answer_evidences = [item for item in evidences if item.evidence_id in answer_ids]
        # A citation is valid only when it points to evidence selected for the
        # answer, not merely to an unrelated item left in the candidate pool.
        valid_ids = answer_ids
        cited_ids = set(re.findall(r"\[(ev_\d{3})\]", state.get("answer", "")))
        static_supported = bool(evidences) and bool(cited_ids) and cited_ids <= valid_ids
        check = self.answer_llm.verify_answer(
            state.get("current_question") or state["question"], state.get("answer", ""), answer_evidences
        ) if self.answer_llm.configured and static_supported else None
        supported = static_supported and (check is None or check.supported)
        if supported:
            if state.get("evidence_insufficient"):
                process.append("引用格式校验通过，但证据评分仍不足")
            else:
                process.append("引用校验通过")
            return {"retry_needed": False, "process": process}
        can_retry = (
            bool(evidences) and state.get("verification_round", 0) < 1
            and state.get("retrieval_round", 0) < self.max_retrieval_rounds
            and state.get("tool_calls", 0) < self.max_tool_calls
            and not state.get("locked_document")
        )
        reason = check.reason if check else "回答缺少有效证据引用"
        process.append(f"引用校验未通过：{reason}")
        if can_retry:
            grade = EvidenceGrade(
                relevant=True, sufficient=False, reason=reason,
                missing_topics=check.unsupported_claims if check else [],
                next_query=check.next_query if check else state["question"],
            )
            return {"retry_needed": True, "verification_round": state.get("verification_round", 0) + 1, "grade": grade, "process": process}
        return {"retry_needed": False, "process": process}

    @staticmethod
    def _after_verify(state: AgentState) -> Literal["retry", "end"]:
        return "retry" if state.get("retry_needed", False) else "end"

    def _answer(self, question: str, evidences: list[Evidence], complete: bool) -> str:
        if not evidences:
            return "当前知识库中没有找到足以回答该问题的证据。请补充标准号、文件名或更具体的关键词。"
        if complete:
            grouped: dict[str, list[Evidence]] = defaultdict(list)
            for item in evidences:
                if item.retrieval_type == "glossary":
                    continue
                grouped[item.source_id].append(item)
            if not grouped:
                return "行业字典提供了术语关系，但当前业务知识库中没有找到匹配文件。"
            rows = [
                f"- {items[0].filename}：命中 {len(items)} 处 [{items[0].evidence_id}]"
                for items in grouped.values()
            ]
            return f"在当前知识库中共找到 {len(grouped)} 份匹配文件：\n" + "\n".join(rows)
        if self.answer_llm.configured:
            try:
                parameters = inspect.signature(self.answer_llm.answer).parameters
                return (
                    self.answer_llm.answer(question, evidences[:self.max_evidence_for_answer], self.answer_prompt)
                    if len(parameters) >= 3
                    else self.answer_llm.answer(question, evidences[:self.max_evidence_for_answer])
                )
            except Exception:
                pass
        selected = evidences[:8]
        return "根据检索到的文件内容：\n" + "\n".join(f"- {item.content[:500]} [{item.evidence_id}]" for item in selected)

    @staticmethod
    def _source_evidences(sources: list[Source], retrieval_type: str) -> list[Evidence]:
        rows: list[Evidence] = []
        for item in sources:
            custom = "; ".join(
                f"{key}={getattr(value, 'value', value.get('value') if isinstance(value, dict) else value)}"
                for key, value in item.custom_metadata.items()
            ) or "无"
            rows.append(Evidence(source_id=item.source_id, filename=item.filename, standard_no=item.standard_no, content=(
                f"标准号={item.standard_no or '未知'}；标题={item.standard_title or '未知'}；年份={item.standard_year or '未知'}；"
                f"发布日期={item.publish_date or '未知'}；实施日期={item.effective_date or '未知'}；状态={item.status or '未知'}；"
                f"代替={item.replaces or '无记录'}；被替代={item.replaced_by or '无记录'}；自定义元数据={custom}"
            ), retrieval_type=retrieval_type, pdf_url=item.pdf_url))
        return rows

    def _sort_document_evidences(self, items: list[Evidence], direction: str = "desc") -> list[Evidence]:
        date_field = self._default_metadata_sort_field()

        def value(item: Evidence) -> tuple[int, str]:
            try:
                source = self.tools.repository.get(item.source_id)
            except KeyError:
                return (0, "")
            raw = (source.custom_metadata or {}).get(date_field) if date_field else None
            raw = getattr(raw, "value", raw.get("value") if isinstance(raw, dict) else raw)
            text = str(raw or "")
            match = re.search(r"(\d{4})\D?(\d{1,2})\D?(\d{1,2})", text)
            if match:
                return (2, f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}")
            fallback = str(source.created_at or "")
            return (1, fallback) if fallback else (0, "")

        return sorted(items, key=value, reverse=str(direction).casefold() != "asc")

    @staticmethod
    def _planner_requested_count(arguments: dict[str, object]) -> int | None:
        if arguments.get("count_explicit") is not True:
            return None
        value = arguments.get("requested_count")
        if isinstance(value, bool):
            return None
        try:
            return min(max(int(value), 1), 100000) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _planner_list_limit(cls, arguments: dict[str, object]) -> int | None:
        explicit = cls._planner_requested_count(arguments)
        if explicit is not None:
            return explicit
        return 3 if arguments.get("recency_requested") is True else None

    @staticmethod
    def _dedupe_and_number(items: list[Evidence]) -> list[Evidence]:
        unique: list[Evidence] = []
        seen: set[tuple[str, int | None, str]] = set()
        for item in items:
            key = (item.source_id, item.line_start, item.content[:200])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        for index, item in enumerate(unique, start=1):
            item.evidence_id = f"ev_{index:03d}"
        return unique

    @classmethod
    def _select_evidence(cls, question: str, items: list[Evidence], limit: int) -> list[Evidence]:
        contexts = [item for item in items if item.retrieval_type in {"local_context", "vector_adjacent"}]
        filtered: list[Evidence] = []
        for item in items:
            if item.retrieval_type == "local_grep" and any(
                context.source_id == item.source_id and item.content.strip() in context.content
                for context in contexts
            ):
                continue
            filtered.append(item)

        metadata_question = bool(re.search(r"元数据|年份|状态|发布|实施|版本|替代|最新|最近|最早|日期|时间", question))
        keywords = cls._keywords(question)
        type_priority = {
            "local_context": 0,
            "vector_adjacent": 1,
            "document_outline": 2,
            "vector": 3,
            "metadata_comparison": 4 if metadata_question else 7,
            "metadata_search": 4 if metadata_question else 7,
            "latest_metadata": 4 if metadata_question else 7,
            "local_grep": 5,
            "filename": 8,
        }
        original_order = {id(item): index for index, item in enumerate(items)}
        ranked = sorted(
            filtered,
            key=lambda item: (
                type_priority.get(item.retrieval_type, 6),
                -sum(1 for keyword in keywords if keyword.casefold() in item.content.casefold()),
                -(item.score or 0),
                original_order[id(item)],
            ),
        )
        selected: list[Evidence] = []
        used_chars = 0
        for item in ranked:
            if len(selected) >= max(1, limit):
                break
            if selected and used_chars + len(item.content) > 32000:
                continue
            selected.append(item)
            used_chars += len(item.content)
        return selected

    @staticmethod
    def _keywords(question: str, standards: list[str] | None = None) -> list[str]:
        cleaned = question
        for standard in standards or []:
            cleaned = cleaned.replace(standard, " ")
        for word in sorted(STOP_WORDS, key=len, reverse=True):
            cleaned = cleaned.replace(word, " ")
        tokens = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z0-9]+", cleaned)
        useful: list[str] = []
        for token in tokens:
            if len(token) < 2 or (token.isascii() and token.isalpha() and len(token) < 3):
                continue
            useful.append(token)
            if len(token) >= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", token):
                useful.extend(
                    token[index:index + 2]
                    for index in range(0, len(token), 2)
                    if len(token[index:index + 2]) == 2
                )
        return list(dict.fromkeys(useful))[:12] or [question]

    @staticmethod
    def _grep_keywords(query: str) -> list[str]:
        quoted = re.findall(r'["“”]([^"“”]+)["“”]', query)
        unquoted = re.sub(r'["“”][^"“”]+["“”]', " ", query)
        tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9][A-Za-z0-9_./%+\-]*", unquoted)
        cleaned_tokens: list[str] = []
        for token in tokens:
            if re.fullmatch(r"[一-鿿]+", token):
                token = re.sub(r"(?:相关)?(?:的)?(?:会议纪要|纪要|会议|文件|资料|有哪些|有什么|列表|列出|找出)+$", "", token)
            if token:
                cleaned_tokens.append(token)
        useful = [
            token for token in [*quoted, *cleaned_tokens]
            if token.strip() and token.strip() not in STOP_WORDS
        ]
        return list(dict.fromkeys(token.strip() for token in useful))[:20] or [query.strip()]

    @staticmethod
    def _source_id(arguments: dict[str, object], state: AgentState) -> str | None:
        requested = arguments.get("source_id")
        candidates = state.get("candidate_ids") or []
        if requested and (not candidates or str(requested) in candidates):
            return str(requested)
        return candidates[0] if candidates else None

    @staticmethod
    def _scope_ids(state: AgentState) -> list[str] | None:
        if state.get("scope_locked"):
            return list(state.get("scope_source_ids") or [])
        candidates = state.get("candidate_ids") or []
        return list(candidates) if candidates else None

    @staticmethod
    def _optional_str(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(max(parsed, minimum), maximum)
