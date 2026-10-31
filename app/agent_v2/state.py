from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages
from pydantic import Field
from app.retrieval_v2.models import StrictModel


class ExecutionBudget(StrictModel):
    iterations: int = Field(default=12, ge=1, le=60)
    tool_calls: int = Field(default=6, ge=1, le=30)
    retrieval_rounds: int = Field(default=3, ge=1, le=10)
    tool_seconds: float = Field(default=30, ge=.01, le=120)
    total_seconds: float = Field(default=180, ge=.05, le=1800)
    context_chars: int = Field(default=48000, ge=1000, le=200000)
    evidence_count: int = Field(default=12, ge=1, le=50)
    output_chars: int = Field(default=16000, ge=500, le=50000)
    consecutive_failures: int = Field(default=3, ge=1, le=10)


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    thread_id: str
    run_id: str
    principal: str
    agent_id: str
    scopes: list[dict]
    allowed_tools: list[str]
    budget: dict
    question: str
    evidence: dict
    approvals: dict
    blocked: dict
    iterations: int
    tool_call_count: int
    retrieval_rounds: int
    failures: int
    usage: dict
    stop_reason: str
    final_answer: dict
    elapsed_seconds: float
    calculations: list[dict]
    scope_checks: list[dict]
