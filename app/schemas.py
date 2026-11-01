from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class PdfUploadResult(BaseModel):
    file_id: str
    url: str


class Source(BaseModel):
    source_id: str
    filename: str
    normalized_filename: str
    file_hash: str
    document_type: str = "pdf"
    mime_type: str = "application/pdf"
    file_size: int | None = None
    page_count: int | None = None
    uploaded_by: str | None = None
    standard_no: str | None = None
    normalized_standard_no: str | None = None
    standard_title: str | None = None
    standard_year: int | None = None
    publish_date: str | None = None
    effective_date: str | None = None
    status: str | None = None
    replaces: str | None = None
    replaced_by: str | None = None
    local_pdf_path: str
    local_source_path: str = ""
    local_md_path: str
    local_extracted_md_path: str = ""
    dify_dataset_id: str | None = None
    dify_document_id: str | None = None
    pdf_platform_id: str | None = None
    pdf_url: str | None = None
    parse_status: str = "pending"
    dify_status: str = "pending"
    pdf_platform_status: str = "pending"
    created_at: str
    updated_at: str
    error_message: str | None = None
    custom_metadata: dict[str, "MetadataValue"] = Field(default_factory=dict)
    metadata_status: str = "pending"
    metadata_error: str | None = None
    metadata_extracted_at: str | None = None
    workflow_status: str = "disabled"
    workflow_error: str | None = None
    workflow_output_path: str | None = None
    rewrite_status: Literal["not_requested", "processing", "preview_ready", "confirmed", "failed"] = "not_requested"
    rewrite_prompt: str = ""
    rewrite_preview_path: str | None = None
    rewrite_review_required: bool = False

    @property
    def source_path(self) -> str:
        return self.local_source_path or self.local_pdf_path


class Evidence(BaseModel):
    evidence_id: str = ""
    source_id: str
    filename: str
    standard_no: str | None = None
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    location: str | None = None
    content: str
    retrieval_type: str
    score: float | None = None
    segment_id: str | None = None
    pdf_url: str | None = None
    agent_id: str | None = None


class AskHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(default="default", min_length=1, max_length=120)
    history: list[AskHistoryMessage] = Field(default_factory=list, max_length=12)
    runtime_mode: Literal['legacy','react_v2'] | None = None


class RetrievalTestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    search_method: Literal["full_text_search", "semantic_search", "hybrid_search"] = "semantic_search"
    top_k: int = Field(default=10, ge=1, le=50)
    metadata_filtering_conditions: dict[str, Any] | None = None


class AgentTraceEvent(BaseModel):
    sequence: int
    kind: Literal["route", "decision", "tool", "evaluation", "rewrite", "generation", "verification", "stop", "observation"]
    title: str
    detail: str = ""
    status: Literal["running", "success", "warning", "error"] = "success"
    round: int | None = None
    tool_name: str | None = None
    query: str | None = None
    evidence_count: int | None = None


class RetrievalSource(BaseModel):
    source_id: str
    filename: str
    url: str = ""
    pages: list[int] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    run_id: str | None = None
    agent_id: str = "default"
    agent_name: str = "默认助手"
    answer: str
    evidences: list[Evidence]
    process: list[str]
    trace: list[AgentTraceEvent] = Field(default_factory=list)
    tool_calls: int
    evidence_insufficient: bool = False
    sources: list[RetrievalSource] = Field(default_factory=list)
    runtime_mode: str = 'legacy'
    thread_id: str | None = None
    citation_validation: str = 'not_assessed'
    answer_correctness: str = 'not_assessed'
    answer_validation: str = 'not_assessed'
    answer_displayable: bool = True
    trusted_evidence_count: int = 0
    citation_metadata: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)


class RetrievalResponse(BaseModel):
    run_id: str | None = None
    agent_id: str
    agent_name: str
    knowledge_base_id: str
    question: str
    answer_prompt: str
    evidences: list[Evidence]
    sources: list[RetrievalSource]
    process: list[str]
    trace: list[AgentTraceEvent]
    tool_calls: int
    evidence_insufficient: bool = False


class AgentSummary(BaseModel):
    agent_id: str
    name: str
    description: str = ""
    is_default: bool = False
    allowed_tools: list[str] = Field(default_factory=list)


class KnowledgeBaseInput(BaseModel):
    knowledge_base_id: str
    tenant_key: str = Field(default="", max_length=80)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    answer_prompt: str = Field(default="", max_length=20000)
    base_url: str = "https://api.dify.ai"
    dataset_id: str = ""
    api_key: str | None = Field(default=None, max_length=500)
    search_method: Literal["semantic_search"] = "semantic_search"
    chunking_mode: Literal["automatic", "custom"] = "automatic"
    chunk_separator: str = "\n\n"
    chunk_max_tokens: int = Field(default=800, ge=1, le=10000)
    embedding_model: str = Field(default="", max_length=200)
    pdf_parser: Literal["default", "local", "mineru", "qwenvl"] = "default"
    qwenvl_base_url: str = ""
    qwenvl_api_key: str | None = Field(default=None, max_length=500)
    qwenvl_model: str = "qwen-vl-max"
    qwenvl_short_prompt: str = Field(default="", max_length=20000)
    qwenvl_long_prompt: str = Field(default="", max_length=20000)
    qwenvl_page_threshold: int = Field(default=10, ge=1, le=100)
    qwenvl_batch_pages: int = Field(default=3, ge=1, le=20)
    qwenvl_dpi: int = Field(default=200, ge=72, le=400)
    source_platform_mode: Literal["inherit", "local", "http", "command"] = "inherit"
    source_platform_base_url: str = ""
    source_platform_api_key: str | None = Field(default=None, max_length=500)
    source_platform_command: list[str] = Field(default_factory=list, max_length=20)
    source_platform_url_template: str = ""
    enabled: bool = True
    use_legacy_data: bool = False
    metadata_ai_enabled: bool = True
    metadata_extraction_prompt: str = Field(default="", max_length=12000)
    metadata_fields: list["MetadataFieldDefinition"] = Field(default_factory=list, max_length=50)

    @field_validator('metadata_fields')
    @classmethod
    def unique_metadata_keys(cls, fields: list["MetadataFieldDefinition"]) -> list["MetadataFieldDefinition"]:
        keys = [field.key for field in fields]
        if len(keys) != len(set(keys)):
            raise ValueError('元数据字段标识不能重复')
        return fields

    workflow_enabled: bool = False
    workflow_base_url: str = ""
    workflow_api_key: str | None = Field(default=None, max_length=500)
    workflow_app_id: str = ""
    workflow_markdown_input: str = "markdown"
    workflow_file_input: str = "file"
    workflow_output_field: str = "markdown"


class KnowledgeBaseView(BaseModel):
    knowledge_base_id: str
    tenant_key: str = ""
    owner_user_id: str = "local:sansuokb"
    owner_username: str = "sansuokb"
    name: str
    description: str = ""
    answer_prompt: str = ""
    answer_prompt_custom: bool = False
    base_url: str
    dataset_id: str
    search_method: str
    chunking_mode: str = "automatic"
    chunk_separator: str = "\n\n"
    chunk_max_tokens: int = 800
    embedding_model: str = ""
    pdf_parser: str = "default"
    qwenvl_base_url: str = ""
    qwenvl_api_key_configured: bool = False
    qwenvl_model: str = "qwen-vl-max"
    qwenvl_short_prompt: str = ""
    qwenvl_long_prompt: str = ""
    qwenvl_page_threshold: int = 10
    qwenvl_batch_pages: int = 3
    qwenvl_dpi: int = 200
    source_platform_mode: str = "inherit"
    source_platform_base_url: str = ""
    source_platform_api_key_configured: bool = False
    source_platform_command: list[str] = Field(default_factory=list)
    source_platform_url_template: str = ""
    enabled: bool
    use_legacy_data: bool
    api_key_configured: bool
    api_key_hint: str = ""
    metadata_ai_enabled: bool = True
    metadata_extraction_prompt: str = ""
    metadata_fields: list["MetadataFieldDefinition"] = Field(default_factory=list)
    workflow_enabled: bool = False
    workflow_base_url: str = ""
    workflow_app_id: str = ""
    workflow_api_key_configured: bool = False
    workflow_markdown_input: str = "markdown"
    workflow_file_input: str = "file"
    workflow_output_field: str = "markdown"


class MetadataFieldDefinition(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    value_type: Literal["string", "number", "date", "boolean", "list"] = "string"
    description: str = Field(default="", max_length=500)


class MetadataValue(BaseModel):
    value: Any = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=200)
    extraction_method: Literal["rule", "ai", "manual"] = "ai"


class SourceMetadataUpdate(BaseModel):
    standard_no: str | None = None
    standard_title: str | None = None
    standard_year: int | None = None
    publish_date: str | None = None
    effective_date: str | None = None
    status: str | None = None
    replaces: str | None = None
    replaced_by: str | None = None
    custom_metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class MetadataBatchRequest(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=500)


class SourcePage(BaseModel):
    items: list[Source]
    page: int
    page_size: int
    total: int
    total_pages: int


class MarkdownPreviewResponse(BaseModel):
    source_id: str
    filename: str
    version: str
    markdown: str
    available_versions: list[str]
    stats: dict[str, Any] = Field(default_factory=dict)


class SourceVersion(BaseModel):
    version_id: str
    source_id: str
    version_no: int
    filename: str
    content_hash: str
    parser_engine: str
    parse_status: str
    dify_status: str
    created_at: str
    message: str = ""
    chunking_mode: str = ""
    chunk_max_tokens: int | None = None
    embedding_model: str = ""


class GlossaryEntry(BaseModel):
    term: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(default_factory=list)
    data: Any = None


class GlossaryDocument(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    markdown: str = Field(default="", max_length=100000)
    entries: list[GlossaryEntry] = Field(default_factory=list)
    enabled: bool = True


class ModelConnectionInput(BaseModel):
    model_connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_style: Literal["chat_completions", "responses"] = "chat_completions"
    timeout_seconds: float = Field(default=30, ge=1, le=300)
    priority: int = Field(default=100, ge=1, le=10000)
    enabled: bool = True


class ModelConnectionView(BaseModel):
    model_connection_id: str
    name: str
    base_url: str
    model: str
    api_style: str
    timeout_seconds: float
    priority: int
    enabled: bool
    api_key_configured: bool = False


class ModelConnectionTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None


class AgentConfigInput(BaseModel):
    agent_id: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    knowledge_base_id: str
    agent_type: Literal["knowledge", "research", "data_analysis", "coding", "supervisor"] = "knowledge"
    runtime_mode: Literal['legacy','react_v2'] = 'legacy'
    react_tools: list[str] = Field(default_factory=lambda: [
        'v2.search_knowledge','v2.find_document','v2.read_document','v2.read_document_section',
        'v2.read_document_table','v2.find_exact_text','v2.expand_evidence','v2.calculate'], max_length=18)

    @field_validator('react_tools')
    @classmethod
    def validate_react_tools(cls, values):
        from app.retrieval_v2.tools import DESCRIPTIONS
        if set(values)-set(DESCRIPTIONS): raise ValueError('Unknown V2 tool')
        return list(dict.fromkeys(values))
    member_agent_ids: list[str] = Field(default_factory=list, max_length=10)
    supervisor_routing: Literal["all", "auto"] = "all"
    supervisor_synthesis: Literal["separate", "combined"] = "separate"
    planner_prompt: str = Field(default="", max_length=20000)
    planner_model_ids: list[str] = Field(default_factory=list, max_length=20)
    answer_model_ids: list[str] = Field(default_factory=list, max_length=20)
    glossary_documents: list[GlossaryDocument] = Field(default_factory=list, max_length=20)
    allowed_tools: list[str]
    max_tool_calls: int = Field(default=6, ge=1, le=30)
    max_retrieval_rounds: int = Field(default=3, ge=1, le=10)
    max_evidence_for_answer: int = Field(default=12, ge=1, le=50)
    enabled: bool = True
    is_default: bool = False


class AgentConfigView(AgentConfigInput):
    planner_prompt_custom: bool = False


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    document_count: int | None = None


class VectorHit(BaseModel):
    document_id: str | None = None
    document_name: str | None = None
    content: str
    score: float | None = None
    segment_id: str | None = None


class JobItem(BaseModel):
    item_id: str = ""
    position: int = 0
    filename: str
    expected_size: int | None = None
    source_id: str | None = None
    status: Literal["pending", "uploaded", "queued", "running", "retrying", "success", "duplicate", "preview_ready", "failed", "cancelled"] = "pending"
    message: str = ""
    attempts: int = 0


class IngestJob(BaseModel):
    job_id: str
    agent_id: str = ""
    status: Literal["uploading", "pending", "running", "paused", "completed", "cancelling", "cancelled"] = "pending"
    total: int
    uploaded: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    preview_ready: int = 0
    items: list[JobItem]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None


class DeleteAllSourcesRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=100)


class DeleteJobItem(BaseModel):
    item_id: str
    position: int
    source_id: str
    filename: str
    status: Literal["pending", "running", "success", "failed", "cancelled", "unknown"] = "pending"
    message: str = ""


class DeleteJob(BaseModel):
    job_id: str
    agent_id: str
    knowledge_base_id: str
    knowledge_base_name: str
    status: Literal["running", "completed", "cancelling", "cancelled"] = "running"
    total: int
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    items: list[DeleteJobItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None


class TaskSummary(BaseModel):
    task_id: str
    task_type: Literal["document_ingest", "document_reparse", "document_reindex", "document_delete"]
    agent_id: str
    title: str
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    raw_status: str
    total: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    review_required: int = 0
    created_at: datetime
    updated_at: datetime
    can_pause: bool = False
    can_resume: bool = False
    can_retry: bool = False
    can_cancel: bool = False


class TaskPage(BaseModel):
    items: list[TaskSummary]
    page: int
    page_size: int
    total: int
    total_pages: int


class BatchIngestCreate(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=2000)
    file_sizes: list[int] | None = Field(default=None, max_length=2000)
    rewrite_enabled: bool = False
    rewrite_prompt: str = Field(default="", max_length=12000)
    rewrite_review_required: bool = False
    workflow_enabled: bool = False


class BatchReparseCreate(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=2000)


class RewritePreviewRequest(BaseModel):
    rewrite_prompt: str = Field(default="", max_length=12000)


class RewritePreviewResponse(BaseModel):
    source_id: str
    filename: str
    original_markdown: str
    preview_markdown: str
    rewrite_prompt: str = ""
    status: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvidenceGrade(BaseModel):
    relevant: bool
    sufficient: bool
    reason: str = ""
    missing_topics: list[str] = Field(default_factory=list)
    next_query: str | None = None


class CitationCheck(BaseModel):
    supported: bool
    reason: str = ""
    unsupported_claims: list[str] = Field(default_factory=list)
    next_query: str | None = None
