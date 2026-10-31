from __future__ import annotations

import inspect
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import get_type_hints

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.retrieval import RetrievalTools


TOOL_DESCRIPTIONS = {
    "upload_document": "上传文档并创建异步入库任务",
    "delete_document": "删除指定文档及其索引",
    "parse_document": "将指定文档重新提交解析任务",
    "rebuild_document_index": "使用已确认内容重建文档索引",
    "search_knowledge": "在知识库中执行混合检索",
    "find_files": "按文件名或编号查找文件",
    "grep_files": "在文档中查找关键词",
    "keyword_search": "在文件名、元数据和正文中查找文档",
    "vector_search": "通过 Dify 进行向量检索",
    "search_bm25": "通过 Dify 进行全文检索",
    "search_hybrid": "通过 Dify 进行混合检索",
    "dify_metadata_search": "按 Dify 元数据约束向量检索",
    "metadata_search": "按本地元数据筛选文档",
    "read_full_document": "读取文档正文",
    "get_document": "读取文档正文",
    "read_document_section": "按章节标题读取文档内容",
    "get_document_page": "读取指定页内容",
    "get_document_table": "提取文档中的 Markdown 表格",
    "extract_table": "提取文档中的 Markdown 表格",
    "export_csv": "将结构化数据导出为 CSV",
    "export_excel": "将结构化数据导出为 Excel",
    "analyze_data": "分析结构化数据并返回基础统计",
    "get_document_status": "读取文档处理和索引状态",
    "search_within_document": "在指定文档中检索",
    "get_adjacent_chunks": "读取相邻的 Dify 文本片段",
    "get_document_outline": "读取文档目录",
}

TOOL_CATEGORIES = {
    "upload_document": "文档",
    "delete_document": "文档", "parse_document": "文档", "rebuild_document_index": "文档",
    "search_knowledge": "知识检索",
    "find_files": "知识检索", "grep_files": "知识检索", "keyword_search": "知识检索",
    "vector_search": "知识检索", "search_bm25": "知识检索", "search_hybrid": "知识检索", "dify_metadata_search": "知识检索", "metadata_search": "知识检索",
    "read_full_document": "文档", "get_document": "文档", "read_document_section": "文档", "get_document_page": "文档", "get_document_table": "文档", "get_document_status": "文档", "search_within_document": "文档", "get_adjacent_chunks": "文档",
    "get_document_outline": "文档",
    "extract_table": "数据", "export_csv": "数据", "export_excel": "数据", "analyze_data": "数据",
}

TOOL_READ_ONLY = {name: True for name in TOOL_DESCRIPTIONS}
for _name in ("upload_document", "delete_document", "parse_document", "rebuild_document_index"):
    TOOL_READ_ONLY[_name] = False
WRITE_TOOLS = frozenset(_name for _name, _readonly in TOOL_READ_ONLY.items() if not _readonly)


class DeleteDocumentInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)


class ParseDocumentInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)


class RebuildDocumentIndexInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)


class UploadDocumentInput(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(min_length=1, max_length=35_000_000)
    mime_type: str = Field(default="application/octet-stream", max_length=200)


class ExportCsvInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[str] = Field(min_length=1, max_length=100)
    rows: list[list[str | int | float | bool | None]] = Field(max_length=10000)


class ExportExcelInput(ExportCsvInput):
    filename: str = Field(default="export.xlsx", min_length=1, max_length=200)


class AnalyzeDataInput(ExportCsvInput):
    pass


class ToolConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    requires_approval: bool | None = None


class ToolApprovalStore:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_approvals (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, arguments TEXT NOT NULL, arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                decided_by TEXT, decided_at TEXT)''')

    @staticmethod
    def encode(arguments: dict) -> str:
        values = dict(arguments)
        content = values.pop('content_base64', None)
        if content is not None:
            values['content_base64_sha256'] = hashlib.sha256(str(content).encode('utf-8')).hexdigest()
            values['content_base64_length'] = len(content)
        return json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

    def create(self, actor: str, kb: str, name: str, arguments: dict) -> dict:
        encoded = self.encode(arguments)
        if len(encoded.encode('utf-8')) > 32768:
            raise ValueError('审批参数过长')
        now = datetime.now(timezone.utc)
        ident = 'approval_' + uuid.uuid4().hex
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('INSERT INTO tool_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, actor, kb, name, encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                        'pending', now.isoformat(), (now + timedelta(minutes=15)).isoformat(), None, None))
        return self.get(ident)

    def get(self, ident: str) -> dict:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM tool_approvals WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        result = dict(row)
        result.pop('arguments_hash')
        result['arguments'] = json.loads(result['arguments'])
        if result['status'] in ('pending', 'approved') and result['expires_at'] <= datetime.now(timezone.utc).isoformat():
            result['status'] = 'expired'
        return result

    def list(self, actor: str | None = None) -> list[dict]:
        self.cleanup_expired()
        with sqlite3.connect(self.path, timeout=15) as db:
            rows = db.execute('SELECT id FROM tool_approvals' + (' WHERE actor=?' if actor else '') +
                              ' ORDER BY created_at DESC LIMIT 200', (actor,) if actor else ()).fetchall()
        return [self.get(row[0]) for row in rows]

    def cleanup_expired(self, before: str | None = None) -> int:
        cutoff = before or datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=15) as db:
            result = db.execute("DELETE FROM tool_approvals WHERE expires_at<?", (cutoff,))
            return int(result.rowcount)

    def decide(self, ident: str, actor: str, approve: bool) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=15) as db:
            return bool(db.execute("UPDATE tool_approvals SET status=?,decided_by=?,decided_at=? WHERE id=? AND status='pending' AND expires_at>?",
                                   ('approved' if approve else 'rejected', actor, now, ident, now)).rowcount)

    def consume(self, ident: str, actor: str, kb: str, name: str, arguments: dict) -> bool:
        digest = hashlib.sha256(self.encode(arguments).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            return bool(db.execute("UPDATE tool_approvals SET status='consumed' WHERE id=? AND actor=? AND knowledge_base_id=? AND tool_name=? AND arguments_hash=? AND status='approved' AND expires_at>?",
                                   (ident, actor, kb, name, digest, now)).rowcount)


class ToolCallStore:
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, knowledge_base_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, actor TEXT NOT NULL, channel TEXT NOT NULL,
                status TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_tool_calls_kb ON tool_calls(knowledge_base_id, tool_name, id DESC)')

    def record(self, kb_id: str, name: str, actor: str, channel: str, status: str, duration_ms: int) -> None:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('INSERT INTO tool_calls(knowledge_base_id,tool_name,actor,channel,status,duration_ms,created_at) VALUES(?,?,?,?,?,?,?)',
                       (kb_id, name, actor, channel, status, duration_ms, datetime.now(timezone.utc).isoformat()))

    def stats(self, kb_id: str) -> dict[str, dict]:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('''SELECT tool_name,COUNT(*) AS calls,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                MAX(created_at) AS last_called_at FROM tool_calls WHERE knowledge_base_id=? GROUP BY tool_name''', (kb_id,)).fetchall()
        return {row['tool_name']: dict(row) for row in rows}

    def summary(self) -> dict:
        with sqlite3.connect(self.path, timeout=15) as db:
            row = db.execute("""SELECT COUNT(*) AS calls,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                AVG(duration_ms) AS avg_duration_ms FROM tool_calls""").fetchone()
        calls, succeeded, avg = row
        return {'calls': calls or 0, 'succeeded': succeeded or 0, 'failed': (calls or 0) - (succeeded or 0),
                'avg_duration_ms': float(avg or 0)}

    def recent(self, kb_id: str, name: str) -> list[dict]:
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('''SELECT actor,channel,status,duration_ms,created_at FROM tool_calls
                WHERE knowledge_base_id=? AND tool_name=? ORDER BY id DESC LIMIT 50''', (kb_id, name)).fetchall()
        return [dict(row) for row in rows]

    def recent_for_knowledge_bases(self, kb_ids: list[str], limit: int = 6) -> list[dict]:
        if not kb_ids:
            return []
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            placeholders = ','.join('?' for _ in kb_ids)
            rows = db.execute(f'''SELECT id,knowledge_base_id,tool_name,channel,status,duration_ms,created_at
                FROM tool_calls WHERE knowledge_base_id IN ({placeholders}) ORDER BY id DESC LIMIT ?''',
                [*kb_ids, limit]).fetchall()
        return [dict(row) for row in rows]


class ToolConfigStore:
    """Persistent governance switches for tools that are actually implemented."""
    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_configs(
                tool_name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                requires_approval INTEGER NOT NULL DEFAULT 0, permission TEXT NOT NULL DEFAULT 'tool.invoke',
                updated_at TEXT NOT NULL)''')
            now = datetime.now(timezone.utc).isoformat()
            for name in TOOL_DESCRIPTIONS:
                db.execute("INSERT OR IGNORE INTO tool_configs(tool_name,updated_at) VALUES(?,?)", (name, now))

    def list(self) -> list[dict[str, object]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM tool_configs ORDER BY tool_name").fetchall()
        return [dict(row) | {"enabled": bool(row["enabled"]), "requires_approval": bool(row["requires_approval"])} for row in rows]

    def get(self, name: str) -> dict[str, object]:
        for item in self.list():
            if item["tool_name"] == name:
                return item
        raise KeyError(name)

    def update(self, name: str, enabled: bool, requires_approval: bool, permission: str) -> dict[str, object]:
        if name not in TOOL_DESCRIPTIONS:
            raise KeyError(name)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tool_configs SET enabled=?,requires_approval=?,permission=?,updated_at=? WHERE tool_name=?",
                       (int(enabled), int(requires_approval), permission, datetime.now(timezone.utc).isoformat(), name))
        return self.get(name)


@lru_cache
def input_model(name: str):
    if name not in TOOL_DESCRIPTIONS:
        raise KeyError(name)
    static = {"export_csv": ExportCsvInput, "export_excel": ExportExcelInput, "analyze_data": AnalyzeDataInput, "upload_document": UploadDocumentInput, "delete_document": DeleteDocumentInput, "parse_document": ParseDocumentInput,
              "rebuild_document_index": RebuildDocumentIndexInput}.get(name)
    if static is not None:
        return static
    method = getattr(RetrievalTools, name)
    hints = get_type_hints(method)
    fields = {}
    for parameter in inspect.signature(method).parameters.values():
        if parameter.name == "self":
            continue
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (hints.get(parameter.name, object), default)
    return create_model(f"ToolInput_{name}", __config__=ConfigDict(extra="forbid"), **fields)


def describe_tools(allowed: set[str], configs: dict[str, dict[str, object]] | None = None) -> list[dict[str, object]]:
    configs = configs or {}
    return [{"name": name, "description": description, "category": TOOL_CATEGORIES.get(name, "知识检索"), "read_only": TOOL_READ_ONLY.get(name, True),
             "input_schema": input_model(name).model_json_schema(), "output_schema": ({"type": "object", "properties": {"filename": {"type": "string"}, "content_type": {"type": "string"}, "content": {"type": "string"}, "content_base64": {"type": "string"}, "count": {"type": "integer"}}} if name in {"export_csv", "export_excel"} else {"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}}),
             "required_permission": str(configs.get(name, {}).get("permission", "tool.invoke")),
             "requires_approval": name in WRITE_TOOLS or bool(configs.get(name, {}).get("requires_approval", False)), "enabled": bool(configs.get(name, {}).get("enabled", True))}
            for name, description in TOOL_DESCRIPTIONS.items() if name in allowed]
