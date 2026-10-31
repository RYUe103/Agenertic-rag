from __future__ import annotations

import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent import AgentService
from app.schemas import AgentTraceEvent, AskHistoryMessage, AskResponse, RetrievalResponse, RetrievalSource


class SupervisorState(TypedDict):
    question: str
    source_base_url: str
    conversation_id: str
    history: list[AskHistoryMessage]
    operation: str
    index: int
    results: list[AskResponse | RetrievalResponse]
    selected: list[int]
    routing_status: str


class SupervisorService:
    """Execute a bounded sequence of configured specialist graphs in one knowledge base."""

    def __init__(self, base: AgentService, specialists: list[AgentService], candidates: list[dict[str, str]],
                 routing: str = "all", synthesis: str = "separate"):
        self.base = base
        self.specialists = specialists
        self.candidates = candidates
        self.routing = routing
        self.synthesis = synthesis
        self.llm = base.llm
        self.answer_llm = base.answer_llm
        self.allowed_tools = base.allowed_tools
        graph = StateGraph(SupervisorState)
        graph.add_node("route", self._route)
        graph.add_node("delegate", self._delegate)
        graph.add_edge(START, "route")
        graph.add_edge("route", "delegate")
        graph.add_conditional_edges("delegate", lambda state: "delegate" if state["index"] < len(state["selected"]) else END)
        self.graph = graph.compile()

    def _route(self, state: SupervisorState) -> dict:
        all_indices = list(range(len(self.specialists)))
        if self.routing != "auto":
            return {"selected": all_indices, "routing_status": "ordered"}
        if not self.llm.configured:
            return {"selected": all_indices, "routing_status": "fallback"}
        try:
            chosen = self.llm.route_specialists(state["question"], self.candidates)
            known = {item["id"] for item in self.candidates}
            if not chosen or not set(chosen) <= known:
                return {"selected": all_indices, "routing_status": "fallback"}
            return {"selected": [index for index, item in enumerate(self.candidates) if item["id"] in chosen], "routing_status": "selected"}
        except Exception:
            return {"selected": all_indices, "routing_status": "fallback"}

    def _delegate(self, state: SupervisorState) -> dict:
        specialist = self.specialists[state["selected"][state["index"]]]
        method = specialist.ask if state["operation"] == "ask" else specialist.retrieve
        result = method(state["question"], state["source_base_url"], state["conversation_id"], state["history"])
        return {"index": state["index"] + 1, "results": [*state["results"], result]}

    def _run(self, question: str, source_base_url: str, conversation_id: str,
             history: list[AskHistoryMessage] | None, operation: str) -> SupervisorState:
        result = self.graph.invoke({"question": question, "source_base_url": source_base_url,
                                    "conversation_id": conversation_id, "history": history or [],
                                    "operation": operation, "index": 0, "results": [], "selected": [], "routing_status": ""},
                                   {"recursion_limit": 100})
        return result

    def _combine(self, state: SupervisorState) -> tuple[list, list, list, list, int, bool]:
        results = state["results"]
        evidence, sources, process, trace = [], {}, [], []
        if state["routing_status"] != "ordered":
            fallback = state["routing_status"] == "fallback"
            title = "路由不可用，执行全部成员" if fallback else "已选择专业 Agent"
            trace.append(AgentTraceEvent(sequence=1, kind="route", title=title, status="warning" if fallback else "success"))
            process.append(title)
        for index, result in zip(state["selected"], results):
            specialist = self.specialists[index]
            process.append(f"Supervisor 调用 {specialist.agent_name}")
            trace.append(AgentTraceEvent(sequence=len(trace) + 1, kind="route", title=f"调用 {specialist.agent_name}"))
            for item in result.evidences:
                evidence.append(item.model_copy(update={"evidence_id": f"ev_{len(evidence) + 1:03d}", "agent_id": specialist.agent_id}))
            for item in result.sources:
                existing = sources.get(item.source_id)
                if existing is None:
                    sources[item.source_id] = item.model_copy(deep=True)
                else:
                    existing.pages = list(dict.fromkeys([*existing.pages, *item.pages]))
                    existing.locations = list(dict.fromkeys([*existing.locations, *item.locations]))
            process.extend(result.process)
            offset = len(trace)
            trace.extend(event.model_copy(update={"sequence": offset + index + 1}) for index, event in enumerate(result.trace))
        return evidence, list(sources.values()), process, trace, sum(result.tool_calls for result in results), all(result.evidence_insufficient for result in results)

    def ask(self, question: str, source_base_url: str = "", conversation_id: str = "default",
            history: list[AskHistoryMessage] | None = None) -> AskResponse:
        state = self._run(question, source_base_url, conversation_id, history, "ask")
        evidence, sources, process, trace, calls, insufficient = self._combine(state)
        answer = "\n\n".join(f"### {self.specialists[index].agent_name}\n\n{result.answer}" for index, result in zip(state["selected"], state["results"]) if isinstance(result, AskResponse))
        if self.synthesis == "combined":
            combined = self._synthesize(question, evidence, source_base_url)
            if combined:
                answer, sources = combined
                trace.append(AgentTraceEvent(sequence=len(trace) + 1, kind="generation", title="综合回答与引用校验通过"))
                process.append("综合回答与引用校验通过")
            else:
                trace.append(AgentTraceEvent(sequence=len(trace) + 1, kind="generation", title="综合回答不可用，保留成员结果", status="warning"))
                process.append("综合回答不可用，保留成员结果")
        return AskResponse(agent_id=self.base.agent_id, agent_name=self.base.agent_name, answer=answer,
                           evidences=evidence, sources=sources, process=process, trace=trace,
                           tool_calls=calls, evidence_insufficient=insufficient)

    def retrieve(self, question: str, source_base_url: str = "", conversation_id: str = "default",
                 history: list[AskHistoryMessage] | None = None) -> RetrievalResponse:
        state = self._run(question, source_base_url, conversation_id, history, "retrieve")
        evidence, sources, process, trace, calls, insufficient = self._combine(state)
        return RetrievalResponse(agent_id=self.base.agent_id, agent_name=self.base.agent_name,
                                 knowledge_base_id=self.base.knowledge_base_id, question=question,
                                 answer_prompt=self.base.answer_prompt, evidences=evidence, sources=sources,
                                 process=process, trace=trace, tool_calls=calls, evidence_insufficient=insufficient)

    def _synthesize(self, question: str, evidence: list, source_base_url: str) -> tuple[str, list[RetrievalSource]] | None:
        if not evidence or not self.answer_llm.configured:
            return None
        try:
            answer = self.answer_llm.answer(question, evidence, self.base.answer_prompt)
            cited = set(re.findall(r"\[(ev_\d{3,})\]", answer or ""))
            by_id = {item.evidence_id: item for item in evidence}
            if not cited or not cited <= set(by_id) or len(cited) > 12:
                return None
            selected = [item for item in evidence if item.evidence_id in cited]
            check = self.answer_llm.verify_answer(question, answer, selected)
            if check is None or not check.supported:
                return None
            sources: dict[str, RetrievalSource] = {}
            for item in selected:
                url = AgentService._absolute_source_url(item.pdf_url, source_base_url)
                if not url.startswith(("http://", "https://")):
                    continue
                source = sources.setdefault(item.source_id, RetrievalSource(source_id=item.source_id, filename=item.filename, url=url))
                if item.page and item.page not in source.pages:
                    source.pages.append(item.page)
                if item.location and item.location not in source.locations:
                    source.locations.append(item.location)
            formatted = AgentService._append_source_links(answer, selected, list(cited), source_base_url)
            return AgentService._strip_evidence_markers(formatted), list(sources.values())
        except Exception:
            return None
