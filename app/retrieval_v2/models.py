from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, validate_default=True)


class Scope(StrictModel):
    knowledge_base_id: str = Field(min_length=1, max_length=200)
    document_ids: list[str] | None = Field(default=None, max_length=1000)
    versions: dict[str, str] = Field(default_factory=dict, max_length=1000)


class Filters(StrictModel):
    document_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=30)
    page: int | None = Field(default=None, ge=1)
    section_id: str | None = None
    table_id: str | None = None


class Budget(StrictModel):
    timeout_seconds: float = Field(default=5, ge=.05, le=30)
    backend_candidates: int = Field(default=40, ge=1, le=100)
    total_candidates: int = Field(default=200, ge=1, le=500)
    context_chars: int = Field(default=24000, ge=1, le=100000)
    item_chars: int = Field(default=6000, ge=1, le=12000)
    exact_scan_chars: int = Field(default=2_000_000, ge=1, le=20_000_000)
    rerank_candidates: int = Field(default=30, ge=1, le=100)
    rerank_item_chars: int = Field(default=2000, ge=1, le=6000)
    rerank_seconds: float = Field(default=1, ge=.01, le=5)


class FusionConfig(StrictModel):
    rrf_k: float = Field(default=60, ge=1, le=1000)
    lexical_weight: float = Field(default=1, ge=0, le=10)
    vector_weight: float = Field(default=1, ge=0, le=10)
    exact_weight: float = Field(default=1.2, ge=0, le=10)
    max_chunks_per_document: int = Field(default=3, ge=1, le=100)


class RetrievalRequest(StrictModel):
    document_start: int = Field(default=0,ge=0,le=10000,description='find_document 候选导航起始序号，不影响检索排名')
    query: str = Field(min_length=1, max_length=512)
    scopes: list[Scope] = Field(min_length=1, max_length=8)
    mode: Literal['auto','hybrid','lexical','vector','exact'] = 'auto'
    top_k: int = Field(default=10, ge=1, le=100)
    filters: Filters = Field(default_factory=Filters)
    budget: Budget = Field(default_factory=Budget)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    exact_query: str | None = Field(default=None, min_length=1, max_length=256)
    regex_mode: bool = False
    case_sensitive: bool = True
    continuations: dict[str, str] = Field(default_factory=dict, max_length=8)
    expand_neighbors: int = Field(default=0, ge=0, le=2)

    @model_validator(mode='after')
    def validate_scopes(self):
        if not self.query.strip():
            raise ValueError('Empty query')
        if len({s.knowledge_base_id for s in self.scopes}) != len(self.scopes):
            raise ValueError('Knowledge base scopes must be unique')
        if self.regex_mode and self.mode != 'exact':
            raise ValueError('Regex requires explicit exact mode')
        if self.mode == 'exact' and len(self.exact_query or self.query)>256:
            raise ValueError('Exact query exceeds 256 characters')
        for scope in self.scopes:
            if scope.document_ids is not None and set(scope.versions)-set(scope.document_ids):
                raise ValueError('Version scope must be inside document scope')
        return self


class Evidence(StrictModel):
    knowledge_base_id: str
    document_id: str
    version_id: str
    document_name: str = ''
    chunk_id: str | None = None
    source_adapter: str
    source_rank: int
    source_score: float | None = None
    fused_score: float = 0
    rerank_score: float | None = None
    content: str
    content_truncated: bool = False
    content_is_projection: bool = False
    locator: dict = Field(default_factory=dict)
    page_numbers: list[int] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)
    matched_ranges: list[dict] = Field(default_factory=list)
    index_generation: str | None = None
    stale: bool = False
    verification_status: Literal['verified_current','verified_historical','unverified_external','stale','unavailable']
    provenance: list[dict] = Field(default_factory=list)

    def key(self):
        return (self.knowledge_base_id, self.document_id, self.version_id,
                self.chunk_id or f"range:{self.locator.get('start')}:{self.locator.get('end')}")


class RetrievalResponse(StrictModel):
    version: str = '2'
    trace_id: str
    results: list[Evidence]
    documents: list[dict] = Field(default_factory=list)
    expansions: list[Evidence] = Field(default_factory=list)
    exact: dict = Field(default_factory=dict)
    plan: dict
    diagnostics: dict
    status: Literal['ok','degraded','empty','insufficient_evidence']
