from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol

from app.schemas import (
    AgentConfigInput, AgentConfigView, KnowledgeBaseInput, KnowledgeBaseView,
    ModelConnectionInput, ModelConnectionView,
)


CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bases (
  knowledge_base_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL DEFAULT 'local:sansuokb',
  owner_username TEXT NOT NULL DEFAULT 'sansuokb',
  tenant_key TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  answer_prompt TEXT NOT NULL DEFAULT '',
  base_url TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  api_key_env TEXT,
  search_method TEXT NOT NULL DEFAULT 'semantic_search',
  chunking_mode TEXT NOT NULL DEFAULT 'automatic',
  chunk_separator TEXT NOT NULL DEFAULT '\n\n',
  chunk_max_tokens INTEGER NOT NULL DEFAULT 800,
  embedding_model TEXT NOT NULL DEFAULT '',
  pdf_parser TEXT NOT NULL DEFAULT 'default',
  qwenvl_base_url TEXT NOT NULL DEFAULT '',
  qwenvl_model TEXT NOT NULL DEFAULT 'qwen-vl-max',
  qwenvl_short_prompt TEXT NOT NULL DEFAULT '',
  qwenvl_long_prompt TEXT NOT NULL DEFAULT '',
  qwenvl_page_threshold INTEGER NOT NULL DEFAULT 10,
  qwenvl_batch_pages INTEGER NOT NULL DEFAULT 3,
  qwenvl_dpi INTEGER NOT NULL DEFAULT 200,
  source_platform_mode TEXT NOT NULL DEFAULT 'inherit',
  source_platform_base_url TEXT NOT NULL DEFAULT '',
  source_platform_command TEXT NOT NULL DEFAULT '[]',
  source_platform_url_template TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  use_legacy_data INTEGER NOT NULL DEFAULT 0,
  metadata_ai_enabled INTEGER NOT NULL DEFAULT 1,
  metadata_extraction_prompt TEXT NOT NULL DEFAULT '',
  metadata_fields TEXT NOT NULL DEFAULT '[]',
  workflow_enabled INTEGER NOT NULL DEFAULT 0,
  workflow_base_url TEXT NOT NULL DEFAULT '',
  workflow_app_id TEXT NOT NULL DEFAULT '',
  workflow_markdown_input TEXT NOT NULL DEFAULT 'markdown',
  workflow_file_input TEXT NOT NULL DEFAULT 'file',
  workflow_output_field TEXT NOT NULL DEFAULT 'markdown'
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  knowledge_base_id TEXT NOT NULL,
  planner_prompt TEXT NOT NULL DEFAULT '',
  planner_prompt_custom INTEGER NOT NULL DEFAULT 0,
  planner_model_ids TEXT NOT NULL DEFAULT '[]',
  answer_model_ids TEXT NOT NULL DEFAULT '[]',
  glossary_documents TEXT NOT NULL DEFAULT '[]',
  allowed_tools TEXT NOT NULL,
  max_tool_calls INTEGER NOT NULL DEFAULT 6,
  max_retrieval_rounds INTEGER NOT NULL DEFAULT 3,
  max_evidence_for_answer INTEGER NOT NULL DEFAULT 12,
  enabled INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
);
CREATE TABLE IF NOT EXISTS model_connections (
  model_connection_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  model TEXT NOT NULL,
  api_style TEXT NOT NULL DEFAULT 'chat_completions',
  timeout_seconds REAL NOT NULL DEFAULT 30,
  priority INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 1,
  api_key_env TEXT NOT NULL DEFAULT ''
);
"""


class SecretStore(Protocol):
    def get(self, key: str) -> str: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FileSecretStore:
    """Small local secret store for offline Linux deployments."""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(key): str(secret) for key, secret in value.items()} if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, values: dict[str, str]) -> None:
        self.path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path.chmod(0o600)

    def get(self, key: str) -> str:
        return self._read().get(key, "")

    def set(self, key: str, value: str) -> None:
        values = self._read()
        values[key] = value
        self._write(values)

    def delete(self, key: str) -> None:
        values = self._read()
        if key in values:
            values.pop(key)
            self._write(values)


class KeychainSecretStore:
    service = "agentic-rag-dify"

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["security", *args], capture_output=True, text=True, timeout=10, check=check
        )

    def get(self, key: str) -> str:
        if sys.platform != "darwin":
            return ""
        result = self._run(["find-generic-password", "-s", self.service, "-a", key, "-w"], False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def set(self, key: str, value: str) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("当前系统不支持 macOS Keychain")
        self._run(["add-generic-password", "-U", "-s", self.service, "-a", key, "-w", value])

    def delete(self, key: str) -> None:
        if sys.platform == "darwin":
            self._run(["delete-generic-password", "-s", self.service, "-a", key], False)


class ConfigStore:
    def __init__(self, path: Path, secrets: SecretStore):
        self.path = path
        self.secrets = secrets
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(CONTROL_SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(knowledge_bases)").fetchall()}
            agent_columns = {row[1] for row in connection.execute("PRAGMA table_info(agents)").fetchall()}
            if 'runtime_mode' not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN runtime_mode TEXT NOT NULL DEFAULT 'legacy'")
            if 'react_tools' not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN react_tools TEXT")
            if "planner_prompt_custom" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN planner_prompt_custom INTEGER NOT NULL DEFAULT 0")
            if "glossary_documents" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN glossary_documents TEXT NOT NULL DEFAULT '[]'")
            if "planner_model_ids" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN planner_model_ids TEXT NOT NULL DEFAULT '[]'")
            if "answer_model_ids" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN answer_model_ids TEXT NOT NULL DEFAULT '[]'")
            if "agent_type" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'knowledge'")
            if "member_agent_ids" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN member_agent_ids TEXT NOT NULL DEFAULT '[]'")
            if "supervisor_routing" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN supervisor_routing TEXT NOT NULL DEFAULT 'all'")
            if "supervisor_synthesis" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN supervisor_synthesis TEXT NOT NULL DEFAULT 'separate'")
            migrations = {
                "owner_user_id": "TEXT NOT NULL DEFAULT 'local:sansuokb'",
                "owner_username": "TEXT NOT NULL DEFAULT 'sansuokb'",
                "tenant_key": "TEXT NOT NULL DEFAULT ''",
                "answer_prompt": "TEXT NOT NULL DEFAULT ''",
                "metadata_ai_enabled": "INTEGER NOT NULL DEFAULT 1",
                "chunking_mode": "TEXT NOT NULL DEFAULT 'automatic'",
                "chunk_separator": "TEXT NOT NULL DEFAULT '\n\n'",
                "chunk_max_tokens": "INTEGER NOT NULL DEFAULT 800",
                "embedding_model": "TEXT NOT NULL DEFAULT ''",
                "pdf_parser": "TEXT NOT NULL DEFAULT 'default'",
                "qwenvl_base_url": "TEXT NOT NULL DEFAULT ''",
                "qwenvl_model": "TEXT NOT NULL DEFAULT 'qwen-vl-max'",
                "qwenvl_short_prompt": "TEXT NOT NULL DEFAULT ''",
                "qwenvl_long_prompt": "TEXT NOT NULL DEFAULT ''",
                "qwenvl_page_threshold": "INTEGER NOT NULL DEFAULT 10",
                "qwenvl_batch_pages": "INTEGER NOT NULL DEFAULT 3",
                "qwenvl_dpi": "INTEGER NOT NULL DEFAULT 200",
                "source_platform_mode": "TEXT NOT NULL DEFAULT 'inherit'",
                "source_platform_base_url": "TEXT NOT NULL DEFAULT ''",
                "source_platform_command": "TEXT NOT NULL DEFAULT '[]'",
                "source_platform_url_template": "TEXT NOT NULL DEFAULT ''",
                "metadata_extraction_prompt": "TEXT NOT NULL DEFAULT ''",
                "metadata_fields": "TEXT NOT NULL DEFAULT '[]'",
                "workflow_enabled": "INTEGER NOT NULL DEFAULT 0",
                "workflow_base_url": "TEXT NOT NULL DEFAULT ''",
                "workflow_app_id": "TEXT NOT NULL DEFAULT ''",
                "workflow_markdown_input": "TEXT NOT NULL DEFAULT 'markdown'",
                "workflow_file_input": "TEXT NOT NULL DEFAULT 'file'",
                "workflow_output_field": "TEXT NOT NULL DEFAULT 'markdown'",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE knowledge_bases ADD COLUMN {name} {definition}")
            connection.execute("UPDATE knowledge_bases SET owner_user_id='local:sansuokb', owner_username='sansuokb' WHERE owner_user_id IS NULL OR owner_user_id='' ")
            connection.execute('''CREATE TABLE IF NOT EXISTS legacy_chunk_configuration(
                knowledge_base_id TEXT PRIMARY KEY, chunking_mode TEXT, chunk_separator TEXT, chunk_max_tokens INTEGER)''')
            connection.execute('''INSERT OR IGNORE INTO legacy_chunk_configuration
                SELECT knowledge_base_id,chunking_mode,chunk_separator,chunk_max_tokens FROM knowledge_bases
                WHERE instr(chunk_separator, '__SPLIT__') > 0''')
            connection.execute("UPDATE knowledge_bases SET chunking_mode='automatic',chunk_separator=? WHERE instr(chunk_separator, '__SPLIT__') > 0", ('\n\n',))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def empty(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0

    def ensure_default_model_connection(
        self, base_url: str, model: str, api_style: str, timeout_seconds: float,
    ) -> None:
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM model_connections WHERE model_connection_id = 'default'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO model_connections
                    (model_connection_id,name,base_url,model,api_style,timeout_seconds,priority,enabled,api_key_env)
                    VALUES ('default',?,?,?,?,?,?,?, 'LLM_API_KEY')""",
                    ("默认模型", base_url, model, api_style, timeout_seconds, 100, int(bool(base_url and model))),
                )

    def list_model_connections(self, environment: dict[str, str] | None = None) -> list[ModelConnectionView]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_connections ORDER BY priority, name"
            ).fetchall()
        environment = environment or {}
        return [self._model_view(dict(row), environment) for row in rows]

    def get_model_connection(self, model_connection_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_connections WHERE model_connection_id = ?", (model_connection_id,)
            ).fetchone()
        if row is None:
            raise KeyError(model_connection_id)
        return dict(row)

    def save_model_connection(self, value: ModelConnectionInput) -> ModelConnectionView:
        with self._lock:
            existing: dict[str, object] = {}
            try:
                existing = self.get_model_connection(value.model_connection_id)
            except KeyError:
                pass
            if value.api_key:
                self.secrets.set(f"model:{value.model_connection_id}", value.api_key)
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO model_connections
                    (model_connection_id,name,base_url,model,api_style,timeout_seconds,priority,enabled,api_key_env)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(model_connection_id) DO UPDATE SET
                    name=excluded.name,base_url=excluded.base_url,model=excluded.model,
                    api_style=excluded.api_style,timeout_seconds=excluded.timeout_seconds,
                    priority=excluded.priority,enabled=excluded.enabled""",
                    (
                        value.model_connection_id, value.name, value.base_url, value.model,
                        value.api_style, value.timeout_seconds, value.priority, int(value.enabled),
                        str(existing.get("api_key_env") or ""),
                    ),
                )
        row = self.get_model_connection(value.model_connection_id)
        return self._model_view(row, {})

    def delete_model_connection(self, model_connection_id: str) -> None:
        with self._lock, self.connect() as connection:
            for row in connection.execute("SELECT planner_model_ids,answer_model_ids FROM agents").fetchall():
                selected = json.loads(row["planner_model_ids"] or "[]") + json.loads(row["answer_model_ids"] or "[]")
                if model_connection_id in selected:
                    raise ValueError("该模型连接仍被 Agent 使用")
            deleted = connection.execute(
                "DELETE FROM model_connections WHERE model_connection_id = ?", (model_connection_id,)
            ).rowcount
            if not deleted:
                raise KeyError(model_connection_id)
        self.secrets.delete(f"model:{model_connection_id}")

    def resolve_model_api_key(self, model_connection_id: str, environment: dict[str, str]) -> str:
        row = self.get_model_connection(model_connection_id)
        return self.secrets.get(f"model:{model_connection_id}") or environment.get(str(row.get("api_key_env") or ""), "")

    def list_knowledge_bases(self, owner_username: str | None = None) -> list[KnowledgeBaseView]:
        with self.connect() as connection:
            if owner_username:
                rows = connection.execute("SELECT * FROM knowledge_bases WHERE owner_username=? ORDER BY name", (owner_username,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM knowledge_bases ORDER BY name").fetchall()
        return [self._kb_view(dict(row)) for row in rows]

    def get_knowledge_base(self, knowledge_base_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
            ).fetchone()
        if row is None:
            raise KeyError(knowledge_base_id)
        return dict(row)

    def save_knowledge_base(self, value: KnowledgeBaseInput, api_key_env: str = "", owner_username: str = "sansuokb", owner_user_id: str | None = None) -> KnowledgeBaseView:
        with self._lock:
            existing = None
            try:
                existing = self.get_knowledge_base(value.knowledge_base_id)
            except KeyError:
                pass
            if value.api_key:
                self.secrets.set(value.knowledge_base_id, value.api_key)
            if value.workflow_api_key:
                self.secrets.set(f"{value.knowledge_base_id}:workflow", value.workflow_api_key)
            if value.source_platform_api_key:
                self.secrets.set(f"{value.knowledge_base_id}:source-platform", value.source_platform_api_key)
            if value.qwenvl_api_key:
                self.secrets.set(f"{value.knowledge_base_id}:qwenvl", value.qwenvl_api_key)
            env_name = api_key_env or str((existing or {}).get("api_key_env") or "")
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO knowledge_bases
                    (knowledge_base_id,owner_user_id,owner_username,tenant_key,name,description,answer_prompt,base_url,dataset_id,api_key_env,search_method,chunking_mode,chunk_separator,chunk_max_tokens,embedding_model,
                     pdf_parser,qwenvl_base_url,qwenvl_model,qwenvl_short_prompt,qwenvl_long_prompt,qwenvl_page_threshold,qwenvl_batch_pages,qwenvl_dpi,
                     source_platform_mode,source_platform_base_url,source_platform_command,source_platform_url_template,enabled,use_legacy_data,
                     metadata_ai_enabled,metadata_extraction_prompt,metadata_fields,workflow_enabled,workflow_base_url,
                     workflow_app_id,workflow_markdown_input,workflow_file_input,workflow_output_field)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(knowledge_base_id) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,owner_username=excluded.owner_username,tenant_key=excluded.tenant_key,name=excluded.name,description=excluded.description,answer_prompt=excluded.answer_prompt,base_url=excluded.base_url,
                    dataset_id=excluded.dataset_id,api_key_env=excluded.api_key_env,
                    search_method=excluded.search_method,enabled=excluded.enabled,
                    chunking_mode=excluded.chunking_mode,chunk_separator=excluded.chunk_separator,
                    chunk_max_tokens=excluded.chunk_max_tokens,embedding_model=excluded.embedding_model,
                    pdf_parser=excluded.pdf_parser,qwenvl_base_url=excluded.qwenvl_base_url,qwenvl_model=excluded.qwenvl_model,
                    qwenvl_short_prompt=excluded.qwenvl_short_prompt,qwenvl_long_prompt=excluded.qwenvl_long_prompt,
                    qwenvl_page_threshold=excluded.qwenvl_page_threshold,qwenvl_batch_pages=excluded.qwenvl_batch_pages,qwenvl_dpi=excluded.qwenvl_dpi,
                    source_platform_mode=excluded.source_platform_mode,
                    source_platform_base_url=excluded.source_platform_base_url,
                    source_platform_command=excluded.source_platform_command,
                    source_platform_url_template=excluded.source_platform_url_template,
                    use_legacy_data=excluded.use_legacy_data,
                    metadata_ai_enabled=excluded.metadata_ai_enabled,
                    metadata_extraction_prompt=excluded.metadata_extraction_prompt,
                    metadata_fields=excluded.metadata_fields,workflow_enabled=excluded.workflow_enabled,
                    workflow_base_url=excluded.workflow_base_url,workflow_app_id=excluded.workflow_app_id,
                    workflow_markdown_input=excluded.workflow_markdown_input,workflow_file_input=excluded.workflow_file_input,
                    workflow_output_field=excluded.workflow_output_field""",
                    (
                        value.knowledge_base_id, owner_user_id or ("local:sansuokb" if owner_username == "sansuokb" else f"ldap:{owner_username}"), owner_username, value.tenant_key, value.name, value.description, value.answer_prompt, value.base_url,
                        value.dataset_id, env_name, value.search_method, value.chunking_mode, value.chunk_separator, value.chunk_max_tokens, value.embedding_model,
                        value.pdf_parser, value.qwenvl_base_url, value.qwenvl_model, value.qwenvl_short_prompt,
                        value.qwenvl_long_prompt, value.qwenvl_page_threshold, value.qwenvl_batch_pages, value.qwenvl_dpi,
                        value.source_platform_mode, value.source_platform_base_url,
                        json.dumps(value.source_platform_command, ensure_ascii=False), value.source_platform_url_template,
                        int(value.enabled), int(value.use_legacy_data), int(value.metadata_ai_enabled),
                        value.metadata_extraction_prompt,
                        json.dumps([item.model_dump() for item in value.metadata_fields], ensure_ascii=False),
                        int(value.workflow_enabled), value.workflow_base_url, value.workflow_app_id,
                        value.workflow_markdown_input, value.workflow_file_input, value.workflow_output_field,
                    ),
                )
            return next(item for item in self.list_knowledge_bases() if item.knowledge_base_id == value.knowledge_base_id)

    def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        with self._lock, self.connect() as connection:
            used = connection.execute(
                "SELECT COUNT(*) FROM agents WHERE knowledge_base_id = ?", (knowledge_base_id,)
            ).fetchone()[0]
            if used:
                raise ValueError("知识库仍被 Agent 使用")
            deleted = connection.execute(
                "DELETE FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
            ).rowcount
            if not deleted:
                raise KeyError(knowledge_base_id)
        self.secrets.delete(knowledge_base_id)
        self.secrets.delete(f"{knowledge_base_id}:workflow")
        self.secrets.delete(f"{knowledge_base_id}:source-platform")
        self.secrets.delete(f"{knowledge_base_id}:qwenvl")

    def list_agents(self) -> list[AgentConfigView]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM agents ORDER BY name").fetchall()
        return [self._agent_view(dict(row)) for row in rows]

    def get_agent(self, agent_id: str) -> AgentConfigView:
        agent = next((item for item in self.list_agents() if item.agent_id == agent_id), None)
        if agent is not None:
            return agent
        raise KeyError(agent_id)

    def save_agent(self, value: AgentConfigInput) -> AgentConfigView:
        self.get_knowledge_base(value.knowledge_base_id)
        with self._lock, self.connect() as connection:
            if value.is_default:
                connection.execute("UPDATE agents SET is_default = 0")
            connection.execute(
                """INSERT INTO agents
                (agent_id,name,description,knowledge_base_id,planner_prompt,planner_prompt_custom,planner_model_ids,answer_model_ids,glossary_documents,allowed_tools,max_tool_calls,
                 max_retrieval_rounds,max_evidence_for_answer,enabled,is_default)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                name=excluded.name,description=excluded.description,knowledge_base_id=excluded.knowledge_base_id,
                planner_prompt=excluded.planner_prompt,allowed_tools=excluded.allowed_tools,
                planner_prompt_custom=excluded.planner_prompt_custom,planner_model_ids=excluded.planner_model_ids,
                answer_model_ids=excluded.answer_model_ids,glossary_documents=excluded.glossary_documents,
                max_tool_calls=excluded.max_tool_calls,max_retrieval_rounds=excluded.max_retrieval_rounds,
                max_evidence_for_answer=excluded.max_evidence_for_answer,enabled=excluded.enabled,
                is_default=excluded.is_default""",
                (
                    value.agent_id, value.name, value.description, value.knowledge_base_id,
                    value.planner_prompt, int(bool(value.planner_prompt.strip())),
                    json.dumps(value.planner_model_ids), json.dumps(value.answer_model_ids),
                    json.dumps([item.model_dump() for item in value.glossary_documents], ensure_ascii=False),
                    json.dumps(value.allowed_tools), value.max_tool_calls,
                    value.max_retrieval_rounds, value.max_evidence_for_answer, int(value.enabled),
                    int(value.is_default),
                ),
            )
            connection.execute("UPDATE agents SET agent_type=?,member_agent_ids=?,supervisor_routing=?,supervisor_synthesis=? WHERE agent_id=?",
                               (value.agent_type, json.dumps(value.member_agent_ids), value.supervisor_routing, value.supervisor_synthesis, value.agent_id))
            connection.execute('UPDATE agents SET runtime_mode=?,react_tools=? WHERE agent_id=?',
                               (value.runtime_mode,json.dumps(value.react_tools),value.agent_id))
            default_count = connection.execute("SELECT COUNT(*) FROM agents WHERE is_default = 1").fetchone()[0]
            if not default_count:
                connection.execute("UPDATE agents SET is_default = 1 WHERE agent_id = ?", (value.agent_id,))
        return self.get_agent(value.agent_id)

    def delete_agent(self, agent_id: str) -> None:
        with self._lock, self.connect() as connection:
            row = connection.execute("SELECT is_default FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                raise KeyError(agent_id)
            if row["is_default"]:
                raise ValueError("默认 Agent 不能删除，请先设置其他默认 Agent")
            connection.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))

    def resolve_api_key(self, knowledge_base_id: str, environment: dict[str, str]) -> str:
        row = self.get_knowledge_base(knowledge_base_id)
        secret = self.secrets.get(knowledge_base_id)
        return secret or environment.get(str(row.get("api_key_env") or ""), "")

    def _kb_view(self, row: dict[str, object]) -> KnowledgeBaseView:
        configured = bool(self.secrets.get(str(row["knowledge_base_id"]))) or bool(row.get("api_key_env"))
        return KnowledgeBaseView(
            knowledge_base_id=str(row["knowledge_base_id"]),
            tenant_key=str(row.get("tenant_key") or ""),
            owner_user_id=str(row.get("owner_user_id") or "local:sansuokb"),
            owner_username=str(row.get("owner_username") or "sansuokb"),
            name=str(row["name"]),
            description=str(row["description"]), answer_prompt=str(row.get("answer_prompt") or ""),
            answer_prompt_custom=bool(str(row.get("answer_prompt") or "").strip()), base_url=str(row["base_url"]),
            dataset_id=str(row["dataset_id"]), search_method=str(row["search_method"]),
            chunking_mode=str(row.get("chunking_mode") or "automatic"),
            chunk_separator=str(row.get("chunk_separator") or "\n\n"),
            chunk_max_tokens=int(row.get("chunk_max_tokens") or 800),
            embedding_model=str(row.get("embedding_model") or ""),
            pdf_parser=str(row.get("pdf_parser") or "default"),
            qwenvl_base_url=str(row.get("qwenvl_base_url") or ""),
            qwenvl_api_key_configured=bool(self.secrets.get(f"{row['knowledge_base_id']}:qwenvl")),
            qwenvl_model=str(row.get("qwenvl_model") or "qwen-vl-max"),
            qwenvl_short_prompt=str(row.get("qwenvl_short_prompt") or ""),
            qwenvl_long_prompt=str(row.get("qwenvl_long_prompt") or ""),
            qwenvl_page_threshold=int(row.get("qwenvl_page_threshold") or 10),
            qwenvl_batch_pages=int(row.get("qwenvl_batch_pages") or 3), qwenvl_dpi=int(row.get("qwenvl_dpi") or 200),
            source_platform_mode=str(row.get("source_platform_mode") or "inherit"),
            source_platform_base_url=str(row.get("source_platform_base_url") or ""),
            source_platform_api_key_configured=bool(self.secrets.get(f"{row['knowledge_base_id']}:source-platform")),
            source_platform_command=json.loads(str(row.get("source_platform_command") or "[]")),
            source_platform_url_template=str(row.get("source_platform_url_template") or ""),
            enabled=bool(row["enabled"]), use_legacy_data=bool(row["use_legacy_data"]),
            api_key_configured=configured, api_key_hint="已配置" if configured else "未配置",
            metadata_ai_enabled=bool(row.get("metadata_ai_enabled", 1)),
            metadata_extraction_prompt=str(row.get("metadata_extraction_prompt") or ""),
            metadata_fields=json.loads(str(row.get("metadata_fields") or "[]")),
            workflow_enabled=bool(row.get("workflow_enabled", 0)), workflow_base_url=str(row.get("workflow_base_url") or ""),
            workflow_app_id=str(row.get("workflow_app_id") or ""),
            workflow_api_key_configured=bool(self.secrets.get(f"{row['knowledge_base_id']}:workflow")),
            workflow_markdown_input=str(row.get("workflow_markdown_input") or "markdown"),
            workflow_file_input=str(row.get("workflow_file_input") or "file"), workflow_output_field=str(row.get("workflow_output_field") or "markdown"),
        )

    def _model_view(self, row: dict[str, object], environment: dict[str, str]) -> ModelConnectionView:
        configured = bool(self.secrets.get(f"model:{row['model_connection_id']}")) or bool(
            environment.get(str(row.get("api_key_env") or ""), "")
        )
        return ModelConnectionView(
            model_connection_id=str(row["model_connection_id"]), name=str(row["name"]),
            base_url=str(row["base_url"]), model=str(row["model"]), api_style=str(row["api_style"]),
            timeout_seconds=float(row["timeout_seconds"]), priority=int(row["priority"]),
            enabled=bool(row["enabled"]), api_key_configured=configured,
        )

    @staticmethod
    def _agent_view(row: dict[str, object]) -> AgentConfigView:
        return AgentConfigView(
            agent_id=str(row["agent_id"]), name=str(row["name"]), description=str(row["description"]),
            knowledge_base_id=str(row["knowledge_base_id"]), planner_prompt=str(row["planner_prompt"]),
            agent_type=str(row.get("agent_type") or "knowledge"),
            runtime_mode=str(row.get('runtime_mode') or 'legacy'),
            **({'react_tools':json.loads(str(row['react_tools']))} if row.get('react_tools') is not None else {}),
            member_agent_ids=json.loads(str(row.get("member_agent_ids") or "[]")),
            supervisor_routing=str(row.get("supervisor_routing") or "all"),
            supervisor_synthesis=str(row.get("supervisor_synthesis") or "separate"),
            planner_prompt_custom=bool(row.get("planner_prompt_custom", 0)),
            planner_model_ids=json.loads(str(row.get("planner_model_ids") or "[]")),
            answer_model_ids=json.loads(str(row.get("answer_model_ids") or "[]")),
            glossary_documents=json.loads(str(row.get("glossary_documents") or "[]")),
            allowed_tools=json.loads(str(row["allowed_tools"])), max_tool_calls=int(row["max_tool_calls"]),
            max_retrieval_rounds=int(row["max_retrieval_rounds"]),
            max_evidence_for_answer=int(row["max_evidence_for_answer"]), enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
        )
