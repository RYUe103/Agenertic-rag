"""Version-scoped source models. Offsets are Unicode character offsets, end-exclusive."""
from typing import Any
from pydantic import BaseModel, Field


class Document(BaseModel):
    id: str
    knowledge_base_id: str
    current_version_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentVersion(BaseModel):
    id: str
    document_id: str
    version_no: int
    markdown: str
    content_hash: str
    builder_version: str = 'structure-v1'
    provenance: dict[str, Any] = Field(default_factory=dict)


class EvidenceLocator(BaseModel):
    version_id: str
    block_ids: list[str] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)
    bbox: list[float] | None = None
    coordinate_system: str | None = None
    start: int | None = None
    end: int | None = None


class Page(BaseModel):
    id: str
    version_id: str
    number: int
    block_ids: list[str] = Field(default_factory=list)


class Section(BaseModel):
    id: str
    version_id: str
    title: str
    level: int
    parent_id: str | None = None
    block_ids: list[str] = Field(default_factory=list)


class Block(BaseModel):
    id: str
    version_id: str
    ordinal: int
    kind: str
    content: str
    locator: EvidenceLocator
    attributes: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    id: str
    version_id: str
    ordinal: int
    content: str
    locator: EvidenceLocator
    builder_version: str = 'chars-1800-v1'
    section_titles: list[str] = Field(default_factory=list)
    table_id: str | None = None
    cell_ids: list[str] = Field(default_factory=list)
    table_rows: list[int] = Field(default_factory=list)
    table_caption: str | None = None
    content_is_projection: bool = False


class TableCell(BaseModel):
    id: str
    row: int
    column: int
    content: str
    row_span: int = 1
    column_span: int = 1
    is_header: bool = False
    bbox: list[float] | None = None
    unit: str | None = None


class Table(BaseModel):
    id: str
    version_id: str
    block_id: str
    raw: str
    format: str
    cells: list[TableCell] = Field(default_factory=list)
    unit: str | None = None


class ExternalIndexMapping(BaseModel):
    id: str
    version_id: str
    adapter: str
    index_version: str
    status: str
    external_document_id: str | None = None
    external_segment_id: str | None = None
    chunk_id: str | None = None
    dataset_id: str | None = None
    builder_version: str | None = None
