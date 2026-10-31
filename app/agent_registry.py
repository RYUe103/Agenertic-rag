from __future__ import annotations

import os
import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator

from app.agent import AgentService
from app.supervisor import SupervisorService
from app.clients.dify import DifyClient, MockDifyClient
from app.clients.llm import LLMClient
from app.clients.llm_pool import LLMModelPool
from app.clients.pdf_platform import CommandPdfPlatformClient, HttpPdfPlatformClient, MockPdfPlatformClient, PdfPlatformClient
from app.clients.workflow import DifyWorkflowClient
from app.config import Settings
from app.config_store import ConfigStore, FileSecretStore, KeychainSecretStore, MemorySecretStore
from app.db import SourceRepository
from app.ingestion import IngestionService
from app.ocr import OcrProvider
from app.mineru import MinerUClient, MinerUCloudClient
from app.qwenvl import QwenVLClient
from app.retrieval import RetrievalTools
from app.schemas import (
    AgentConfigInput, AgentConfigView, AgentSummary, ConnectionTestResult,
    KnowledgeBaseInput, KnowledgeBaseView, MetadataFieldDefinition,
    ModelConnectionInput, ModelConnectionTestResult, ModelConnectionView,
)


DEFAULT_TOOLS = [
    "vector_search", "grep_files", "keyword_search", "find_files", "search_within_document", "metadata_search",
    "dify_metadata_search", "search_bm25", "search_hybrid",
    "read_full_document", "get_document", "read_document_section", "get_document_page", "get_document_table", "get_document_status", "get_adjacent_chunks", "get_document_outline",
    "search_knowledge", "upload_document", "delete_document", "parse_document", "rebuild_document_index", "extract_table", "export_csv", "export_excel", "analyze_data",
]


class AgentProfile(BaseModel):
    id: str
    name: str
    description: str = ""
    use_legacy_data: bool = False
    planner_prompt: str = ""
    dify_base_url_env: str = "DIFY_BASE_URL"
    dify_api_key_env: str = "DIFY_KNOWLEDGE_API_KEY"
    dify_dataset_id_env: str = "DIFY_DATASET_ID"
    allowed_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOLS))
    max_tool_calls: int = 6
    max_retrieval_rounds: int = 3
    max_evidence_for_answer: int = 12

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        validate_identifier(value, "Agent")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, value: list[str]) -> list[str]:
        return validate_tools(value)


class AgentCatalog(BaseModel):
    default_agent: str
    agents: list[AgentProfile]


@dataclass
class AgentRuntime:
    profile: AgentConfigView
    repository: SourceRepository
    dify: object
    tools: RetrievalTools
    ingestion: IngestionService
    agent: AgentService | SupervisorService
    owner_username: str = "sansuokb"


def validate_identifier(value: str, kind: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError(f"{kind} id 只能使用小写字母、数字、下划线和连字符")


def validate_tools(value: list[str]) -> list[str]:
    unknown = sorted(set(value) - set(DEFAULT_TOOLS))
    if unknown:
        raise ValueError(f"未知工具：{', '.join(unknown)}")
    if not value:
        raise ValueError("Agent 至少需要一个工具")
    return list(dict.fromkeys(value))


class AgentRegistry:
    def __init__(self, settings: Settings, llm: LLMClient, pdf_platform: PdfPlatformClient, ocr: OcrProvider | None = None) -> None:
        self.settings = settings
        self.llm = llm
        self.pdf_platform = pdf_platform
        self.ocr = ocr
        self._lock = threading.RLock()
        self._environment = {
            **{key: str(value) for key, value in dotenv_values(".env").items() if value is not None},
            **os.environ,
        }
        if settings.app_mock_external_services:
            secrets = MemorySecretStore()
        elif sys.platform == "darwin":
            secrets = KeychainSecretStore()
        else:
            secrets = FileSecretStore(settings.app_data_dir / "secrets.json")
        self.store = ConfigStore(settings.control_database_path, secrets)
        self.store.ensure_default_model_connection(
            settings.llm_base_url, settings.llm_model, settings.llm_api_style,
            settings.request_timeout_seconds,
        )
        if self.store.empty():
            self._seed_from_yaml()
        else:
            self._migrate_legacy_planner_prompts()
        self._agents: dict[str, AgentRuntime] = {}
        self._knowledge_runtimes: dict[str, AgentRuntime] = {}
        self.default_agent_id = ""
        self._rebuild()

    def get(self, agent_id: str) -> AgentRuntime:
        with self._lock:
            if agent_id in self._agents:
                return self._agents[agent_id]
            if agent_id in self._knowledge_runtimes:
                return self._knowledge_runtimes[agent_id]
            raise KeyError(agent_id)

    def default(self) -> AgentRuntime:
        return self.get(self.default_agent_id)

    def list(self) -> list[AgentSummary]:
        with self._lock:
            return [AgentSummary(
                agent_id=item.profile.agent_id, name=item.profile.name,
                description=item.profile.description, is_default=item.profile.agent_id == self.default_agent_id,
                allowed_tools=item.profile.allowed_tools,
            ) for item in self._agents.values()]

    def runtimes(self) -> list[AgentRuntime]:
        with self._lock:
            return list(self._agents.values()) + list(self._knowledge_runtimes.values())

    def list_knowledge_bases(self, owner_username: str | None = None) -> list[KnowledgeBaseView]:
        default_prompt = LLMClient.DEFAULT_ANSWER_PROMPT
        values = self.store.list_knowledge_bases(owner_username)
        return [
            item if item.answer_prompt_custom else item.model_copy(update={"answer_prompt": default_prompt})
            for item in values
        ]

    def list_agent_configs(self) -> list[AgentConfigView]:
        result = self.store.list_agents()
        default_prompt = self._default_prompt()
        return [
            item.model_copy(update={"planner_prompt": default_prompt})
            if not item.planner_prompt_custom else item
            for item in result
        ]

    def list_model_connections(self) -> list[ModelConnectionView]:
        return self.store.list_model_connections(self._environment)

    def save_model_connection(self, value: ModelConnectionInput) -> ModelConnectionView:
        validate_identifier(value.model_connection_id, "Model connection")
        if not value.base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        if value.enabled and not (value.api_key or self._existing_model_key(value.model_connection_id)):
            raise ValueError("启用的模型连接必须配置 API Key")
        if value.model_connection_id == "default" and not value.enabled:
            implicit = any(
                not item.planner_model_ids or not item.answer_model_ids
                for item in self.store.list_agents()
            )
            if implicit:
                raise ValueError("尚有 Agent 隐式使用默认模型，不能停用")
        if not value.enabled:
            used = any(
                value.model_connection_id in item.planner_model_ids
                or value.model_connection_id in item.answer_model_ids
                for item in self.store.list_agents()
            )
            if used:
                raise ValueError("该模型连接仍被 Agent 使用，请先修改 Agent 模型链")
        result = self.store.save_model_connection(value)
        self._rebuild()
        return next(
            item for item in self.list_model_connections()
            if item.model_connection_id == result.model_connection_id
        )

    def delete_model_connection(self, model_connection_id: str) -> None:
        if model_connection_id == "default":
            raise ValueError("默认模型连接不能删除，可以修改它的配置")
        self.store.delete_model_connection(model_connection_id)
        self._rebuild()

    def test_model_connection(self, value: ModelConnectionInput) -> ModelConnectionTestResult:
        import time

        if not value.base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        if self.settings.app_mock_external_services:
            return ModelConnectionTestResult(ok=True, message="本地演示模型连接可用", latency_ms=0)
        key = value.api_key or self._existing_model_key(value.model_connection_id)
        if not key:
            return ModelConnectionTestResult(ok=False, message="尚未配置 API Key")
        client = LLMClient(value.base_url, key, value.model, value.api_style, value.timeout_seconds)
        started = time.monotonic()
        try:
            response = client.answer("只回答 OK", [], "只回答 OK。")
            return ModelConnectionTestResult(
                ok=True, message=f"连接成功，模型返回：{response[:80]}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            return ModelConnectionTestResult(ok=False, message=f"连接失败：{str(exc)[:300]}")

    def save_knowledge_base(self, value: KnowledgeBaseInput, owner_username: str = "sansuokb", owner_user_id: str | None = None) -> KnowledgeBaseView:
        validate_identifier(value.knowledge_base_id, "Knowledge base")
        if not value.enabled:
            bound = [item for item in self.store.list_agents() if item.enabled and item.knowledge_base_id == value.knowledge_base_id]
            if bound:
                raise ValueError("请先停用或改绑使用该知识库的 Agent")
        if value.enabled:
            self._validate_kb_fields(value)
        if value.answer_prompt.strip() == LLMClient.DEFAULT_ANSWER_PROMPT:
            value.answer_prompt = ""
        result = self.store.save_knowledge_base(value, owner_username=owner_username, owner_user_id=owner_user_id)
        self._rebuild()
        return result if result.answer_prompt_custom else result.model_copy(
            update={"answer_prompt": LLMClient.DEFAULT_ANSWER_PROMPT}
        )

    def create_knowledge_base(self, value: KnowledgeBaseInput, owner_username: str, owner_user_id: str) -> KnowledgeBaseView:
        with self._lock:
            try:
                self.store.get_knowledge_base(value.knowledge_base_id)
            except KeyError:
                return self.save_knowledge_base(value, owner_username, owner_user_id)
            raise ValueError("知识库 ID 已存在")

    def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        self.store.delete_knowledge_base(knowledge_base_id)
        self._rebuild()

    def save_agent(self, value: AgentConfigInput) -> AgentConfigView:
        validate_identifier(value.agent_id, "Agent")
        value.allowed_tools = validate_tools(value.allowed_tools)
        value.planner_model_ids = self._validate_model_ids(value.planner_model_ids, "Planner")
        value.answer_model_ids = self._validate_model_ids(value.answer_model_ids, "Answer")
        value.member_agent_ids = list(dict.fromkeys(value.member_agent_ids))
        configs = {item.agent_id: item for item in self.store.list_agents()}
        configs[value.agent_id] = value
        for supervisor in configs.values():
            if supervisor.agent_type != "supervisor":
                if supervisor.member_agent_ids:
                    raise ValueError("只有 Supervisor 可以配置专业 Agent")
                continue
            if not supervisor.enabled:
                continue
            if not supervisor.member_agent_ids:
                raise ValueError("启用的 Supervisor 至少需要一个专业 Agent")
            if len(supervisor.member_agent_ids) > min(supervisor.max_tool_calls, supervisor.max_evidence_for_answer):
                raise ValueError("Supervisor 的工具调用和证据上限不能小于成员数量")
            for member_id in supervisor.member_agent_ids:
                member = configs.get(member_id)
                if not member or not member.enabled or member.agent_type == "supervisor" or member_id == supervisor.agent_id:
                    raise ValueError("Supervisor 成员必须是已启用的专业 Agent")
                if member.knowledge_base_id != supervisor.knowledge_base_id:
                    raise ValueError("Supervisor 成员必须绑定同一知识库")
                if not set(member.allowed_tools) & set(supervisor.allowed_tools):
                    raise ValueError("Supervisor 与每个成员至少需要一个共同授权的工具")
        kb = self.store.get_knowledge_base(value.knowledge_base_id)
        if value.enabled and not bool(kb["enabled"]):
            raise ValueError("启用的 Agent 不能绑定已停用知识库")
        if value.enabled:
            self._client_for_row(kb)
        elif value.agent_id in {item.agent_id for item in self.store.list_agents() if item.enabled}:
            others = [item for item in self.store.list_agents() if item.enabled and item.agent_id != value.agent_id]
            if not others:
                raise ValueError("至少需要保留一个启用的 Agent")
        # The UI displays the live default in the editor. Saving it unchanged
        # must keep the Agent on the live default instead of creating a copy.
        if value.planner_prompt.strip() == self._default_prompt():
            value.planner_prompt = ""
        result = self.store.save_agent(value)
        self._rebuild()
        return result

    def delete_agent(self, agent_id: str) -> None:
        if any(item.enabled and agent_id in item.member_agent_ids for item in self.store.list_agents()):
            raise ValueError("该 Agent 仍被 Supervisor 使用，请先移除")
        self.store.delete_agent(agent_id)
        self._rebuild()

    def test_connection(self, value: KnowledgeBaseInput) -> ConnectionTestResult:
        self._validate_kb_fields(value)
        try:
            if self.settings.app_mock_external_services:
                return ConnectionTestResult(ok=True, message="本地演示连接可用", document_count=0)
            key = value.api_key or self._existing_api_key(value.knowledge_base_id)
            if not key:
                return ConnectionTestResult(ok=False, message="尚未配置 API Key")
            client = DifyClient(value.base_url, key, value.dataset_id, self.settings.request_timeout_seconds, value.search_method, value.chunking_mode, value.chunk_separator, value.chunk_max_tokens)
            documents = client.list_documents(limit=100)
            return ConnectionTestResult(ok=True, message="Dify 知识库连接成功", document_count=len(documents))
        except Exception as exc:
            return ConnectionTestResult(ok=False, message=f"连接失败：{str(exc)[:300]}")

    def _rebuild(self) -> None:
        with self._lock:
            built: dict[str, AgentRuntime] = {}
            kb_rows = {kb.knowledge_base_id: self.store.get_knowledge_base(kb.knowledge_base_id) for kb in self.store.list_knowledge_bases()}
            enabled_configs = [item for item in self.store.list_agents() if item.enabled]
            for config in enabled_configs:
                kb = kb_rows.get(config.knowledge_base_id)
                if kb and bool(kb["enabled"]):
                    built[config.agent_id] = self._build_runtime(config, kb)
            if not built:
                raise RuntimeError("至少需要一个启用的 Agent 和知识库")
            configured_default = next((item.agent_id for item in enabled_configs if item.is_default and item.agent_id in built), None)
            self._agents = built
            self.default_agent_id = configured_default or next(iter(built))
            shared_profile = built[self.default_agent_id].profile
            self._knowledge_runtimes = {}
            for kb_id, kb in kb_rows.items():
                if bool(kb["enabled"]):
                    runtime_id = f"@kb:{kb_id}"
                    profile = shared_profile.model_copy(update={"agent_id": runtime_id, "knowledge_base_id": kb_id})
                    self._knowledge_runtimes[runtime_id] = self._build_runtime(profile, kb)
            configs = {item.agent_id: item for item in enabled_configs}
            for runtime in [*self._agents.values(), *self._knowledge_runtimes.values()]:
                if runtime.profile.agent_type == "supervisor":
                    runtime.agent = self._build_supervisor(runtime, configs)

    def _build_supervisor(self, runtime: AgentRuntime, configs: dict[str, AgentConfigView]) -> SupervisorService:
        base = runtime.agent
        assert isinstance(base, AgentService)
        members = [configs[member_id] for member_id in runtime.profile.member_agent_ids]
        budget = runtime.profile.max_tool_calls // len(members)
        evidence_budget = runtime.profile.max_evidence_for_answer // len(members)
        specialists = [AgentService(
            runtime.tools, self._model_pool(member.planner_model_ids, "planner"),
            min(member.max_tool_calls, budget), min(member.max_retrieval_rounds, runtime.profile.max_retrieval_rounds),
            min(member.max_evidence_for_answer, evidence_budget), agent_id=member.agent_id, agent_name=member.name,
            allowed_tools=[name for name in member.allowed_tools if name in base.allowed_tools],
            planner_prompt=self._planner_prompt(member), metadata_fields=base.metadata_fields,
            knowledge_base_id=base.knowledge_base_id, answer_prompt=base.answer_prompt,
            glossary_documents=member.glossary_documents, answer_llm=self._model_pool(member.answer_model_ids, "answer"),
        ) for member in members]
        candidates = [{"id": member.agent_id, "name": member.name, "type": member.agent_type,
                       "description": member.description} for member in members]
        return SupervisorService(base, specialists, candidates, runtime.profile.supervisor_routing,
                                 runtime.profile.supervisor_synthesis)

    def _build_runtime(self, profile: AgentConfigView, kb: dict[str, object]) -> AgentRuntime:
        data_dir = self.settings.app_data_dir if bool(kb["use_legacy_data"]) else self.settings.app_data_dir / "knowledge_bases" / str(kb["knowledge_base_id"])
        files_dir = data_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        repository = SourceRepository(data_dir / "sources.db", str(kb["knowledge_base_id"]))
        dify = self._client_for_row(kb)
        tools = RetrievalTools(repository, dify, self.settings.grep_timeout_seconds)
        api_key = self.store.resolve_api_key(str(kb["knowledge_base_id"]), self._environment)
        metadata_fields = [MetadataFieldDefinition.model_validate(item) for item in json.loads(str(kb.get("metadata_fields") or "[]"))]
        planner_llm = self._model_pool(profile.planner_model_ids, "planner")
        answer_llm = self._model_pool(profile.answer_model_ids, "answer")
        workflow = None
        if bool(kb.get("workflow_enabled")):
            workflow_key = self.store.secrets.get(f"{kb['knowledge_base_id']}:workflow")
            if workflow_key and kb.get("workflow_base_url"):
                workflow = DifyWorkflowClient(
                    str(kb["workflow_base_url"]), workflow_key, str(kb.get("workflow_app_id") or ""), self.settings.request_timeout_seconds,
                    str(kb.get("workflow_markdown_input") or "markdown"), str(kb.get("workflow_file_input") or "file"), str(kb.get("workflow_output_field") or "markdown"),
                )
        parser_mode = str(kb.get("pdf_parser") or "default")
        mineru = None
        if not self.settings.app_mock_external_services and (parser_mode == "mineru" or (parser_mode == "default" and self.settings.mineru_enabled)):
            if not self.settings.mineru_url:
                raise ValueError(f"知识库 {kb['knowledge_base_id']} 选择了 MinerU，但 MINERU_URL 未配置")
            if self.settings.mineru_cloud_enabled or self.settings.mineru_url.rstrip('/').endswith('mineru.net/api'):
                mineru = MinerUCloudClient(self.settings.mineru_url, self.settings.mineru_api_key, self.settings.mineru_timeout_seconds,
                                           fallback_on_error=self.settings.mineru_fallback_on_error)
            else:
                mineru = MinerUClient(
                    self.settings.mineru_url, self.settings.mineru_timeout_seconds,
                    self.settings.mineru_return_content_list, self.settings.mineru_return_images,
                    self.settings.mineru_fallback_on_error,
                )
        vision = None
        if parser_mode == "qwenvl":
            vision = QwenVLClient(
                str(kb.get("qwenvl_base_url") or ""),
                self.store.secrets.get(f"{kb['knowledge_base_id']}:qwenvl"),
                str(kb.get("qwenvl_model") or "qwen-vl-max"),
                max(self.settings.request_timeout_seconds, 180),
                str(kb.get("qwenvl_short_prompt") or ""), str(kb.get("qwenvl_long_prompt") or ""),
                int(kb.get("qwenvl_page_threshold") or 10), int(kb.get("qwenvl_batch_pages") or 3),
                int(kb.get("qwenvl_dpi") or 200),
            )
        ingestion = IngestionService(
            repository, files_dir, dify, self._source_platform_for_row(kb),
            [api_key, self._source_platform_key(str(kb["knowledge_base_id"])), self.settings.pdf_platform_api_key,
             self.store.secrets.get(f"{kb['knowledge_base_id']}:qwenvl")], self.ocr,
            planner_llm, metadata_fields, bool(kb.get("metadata_ai_enabled", 1)), str(kb.get("metadata_extraction_prompt") or ""), workflow, mineru, vision,
            str(kb.get('chunking_mode') or ''), int(kb.get('chunk_max_tokens') or 0) or None, str(kb.get('embedding_model') or ''),
        )
        agent = AgentService(
            tools, planner_llm, profile.max_tool_calls, profile.max_retrieval_rounds,
            profile.max_evidence_for_answer, agent_id=profile.agent_id, agent_name=profile.name,
            allowed_tools=[name for name in profile.allowed_tools if name in DEFAULT_TOOLS], planner_prompt=self._planner_prompt(profile),
            metadata_fields=metadata_fields, knowledge_base_id=str(kb["knowledge_base_id"]),
            answer_prompt=str(kb.get("answer_prompt") or "") or LLMClient.DEFAULT_ANSWER_PROMPT,
            glossary_documents=profile.glossary_documents,
            answer_llm=answer_llm,
        )
        return AgentRuntime(profile, repository, dify, tools, ingestion, agent, str(kb.get("owner_username") or "sansuokb"))

    def _client_for_row(self, kb: dict[str, object]) -> object:
        kb_id = str(kb["knowledge_base_id"])
        if self.settings.app_mock_external_services:
            client = MockDifyClient()
            client.dataset_id = str(kb["dataset_id"] or f"mock-{kb_id}")
            return client
        api_key = self.store.resolve_api_key(kb_id, self._environment)
        if not (kb["base_url"] and api_key and kb["dataset_id"]):
            raise ValueError(f"知识库 {kb_id} 的 Dify 配置不完整")
        return DifyClient(
            str(kb["base_url"]), api_key, str(kb["dataset_id"]), self.settings.request_timeout_seconds,
            str(kb["search_method"]), str(kb.get("chunking_mode") or "automatic"),
            str(kb.get("chunk_separator") or "\n\n"), int(kb.get("chunk_max_tokens") or 800),
        )

    def _source_platform_for_row(self, kb: dict[str, object]) -> PdfPlatformClient:
        mode = str(kb.get("source_platform_mode") or "inherit")
        if mode == "inherit":
            return self.pdf_platform
        if mode == "local" or self.settings.app_mock_external_services:
            return MockPdfPlatformClient()
        if mode == "http":
            return HttpPdfPlatformClient(
                str(kb.get("source_platform_base_url") or ""),
                self._source_platform_key(str(kb["knowledge_base_id"])),
                self.settings.request_timeout_seconds,
            )
        if mode == "command":
            command = json.loads(str(kb.get("source_platform_command") or "[]"))
            return CommandPdfPlatformClient(
                [str(item) for item in command], str(kb.get("source_platform_url_template") or "")
            )
        raise ValueError(f"不支持的源文件平台模式：{mode}")

    def _source_platform_key(self, kb_id: str) -> str:
        return self.store.secrets.get(f"{kb_id}:source-platform")

    def _existing_api_key(self, kb_id: str) -> str:
        try:
            return self.store.resolve_api_key(kb_id, self._environment)
        except KeyError:
            return ""

    def _existing_model_key(self, model_connection_id: str) -> str:
        try:
            return self.store.resolve_model_api_key(model_connection_id, self._environment)
        except KeyError:
            return ""

    def _validate_model_ids(self, values: list[str], role: str) -> list[str]:
        selected = list(dict.fromkeys(values or ["default"]))
        rows = {item.model_connection_id: item for item in self.list_model_connections()}
        missing = [value for value in selected if value not in rows]
        if missing:
            raise ValueError(f"未知模型连接：{', '.join(missing)}")
        disabled = [value for value in selected if not rows[value].enabled] if not self.settings.app_mock_external_services else []
        if disabled:
            raise ValueError(f"模型连接已停用：{', '.join(disabled)}")
        incompatible = [
            value for value in selected
            if role == "Planner" and rows[value].api_style != "chat_completions"
        ]
        if incompatible:
            raise ValueError(f"Planner 仅支持 Chat Completions 模型：{', '.join(incompatible)}")
        return selected

    def _model_pool(self, values: list[str], role: str) -> LLMModelPool:
        ids = values or ["default"]
        rows = [self.store.get_model_connection(model_id) for model_id in ids]
        rows.sort(key=lambda row: (int(row.get("priority") or 100), ids.index(str(row["model_connection_id"]))))
        clients: list[tuple[str, LLMClient]] = []
        for row in rows:
            if not bool(row.get("enabled")):
                continue
            model_id = str(row["model_connection_id"])
            clients.append((model_id, LLMClient(
                str(row["base_url"]), "" if self.settings.app_mock_external_services else self.store.resolve_model_api_key(model_id, self._environment),
                str(row["model"]), str(row["api_style"]), float(row["timeout_seconds"]),
            )))
        return LLMModelPool(clients, role)

    @staticmethod
    def _validate_kb_fields(value: KnowledgeBaseInput) -> None:
        if not value.base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        if not value.dataset_id.strip():
            raise ValueError("Dataset ID 不能为空")
        if value.source_platform_mode == "http" and not value.source_platform_base_url.startswith(("http://", "https://")):
            raise ValueError("源文件平台 Base URL 必须以 http:// 或 https:// 开头")
        if value.source_platform_mode == "command" and not value.source_platform_command:
            raise ValueError("命令行源文件平台必须配置执行命令")
        if value.pdf_parser == "qwenvl":
            if not value.qwenvl_base_url.startswith(("http://", "https://")):
                raise ValueError("Qwen-VL Base URL 必须以 http:// 或 https:// 开头")
            if not value.qwenvl_model.strip():
                raise ValueError("Qwen-VL 模型名称不能为空")

    def _seed_from_yaml(self) -> None:
        catalog = self._load_catalog(self.settings.agent_config_path)
        for profile in catalog.agents:
            kb_id = "default-kb" if profile.use_legacy_data else f"kb-{profile.id}"
            self.store.save_knowledge_base(KnowledgeBaseInput(
                knowledge_base_id=kb_id, name=f"{profile.name}知识库",
                description=f"由 {profile.name} 使用的固定知识库",
                base_url=self._env(profile.dify_base_url_env) or "https://api.dify.ai",
                dataset_id=self._env(profile.dify_dataset_id_env) or f"mock-{profile.id}",
                enabled=True, use_legacy_data=profile.use_legacy_data,
            ), api_key_env=profile.dify_api_key_env)
            self.store.save_agent(AgentConfigInput(
                agent_id=profile.id, name=profile.name, description=profile.description,
                knowledge_base_id=kb_id,
                planner_prompt="" if self._is_default_prompt_reference(profile.planner_prompt) else self._prompt(profile.planner_prompt),
                allowed_tools=profile.allowed_tools, max_tool_calls=profile.max_tool_calls,
                max_retrieval_rounds=profile.max_retrieval_rounds,
                max_evidence_for_answer=profile.max_evidence_for_answer,
                enabled=True, is_default=profile.id == catalog.default_agent,
            ))

    def _env(self, name: str) -> str:
        fallbacks = {"DIFY_BASE_URL": self.settings.dify_base_url, "DIFY_KNOWLEDGE_API_KEY": self.settings.dify_knowledge_api_key, "DIFY_DATASET_ID": self.settings.dify_dataset_id}
        return self._environment.get(name, fallbacks.get(name, ""))

    def _prompt(self, value: str) -> str:
        if not value:
            return ""
        if "\n" in value or len(value) > 240:
            return value.strip()
        path = Path(value)
        if not path.is_absolute():
            path = self.settings.agent_config_path.parent.parent / path
        return path.read_text(encoding="utf-8").strip() if path.exists() else value.strip()

    @staticmethod
    def _is_default_prompt_reference(value: str) -> bool:
        return Path(value).name == "default.md" and "prompt" in Path(value).as_posix()

    def _planner_prompt(self, profile: AgentConfigView) -> str:
        """Resolve the live system default while preserving explicit user overrides."""
        if not profile.planner_prompt_custom:
            return self._default_prompt()
        return self._prompt(profile.planner_prompt)

    def _default_prompt(self) -> str:
        default_path = self.settings.agent_config_path.parent.parent / "prompts" / "default.md"
        return default_path.read_text(encoding="utf-8").strip() if default_path.exists() else ""

    def _migrate_legacy_planner_prompts(self) -> None:
        """Clear the old built-in prompt that was persisted before defaults became live."""
        for profile in self.store.list_agents():
            prompt = profile.planner_prompt.strip()
            if prompt and not profile.planner_prompt_custom and self._looks_like_legacy_prompt(prompt):
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE agents SET planner_prompt = '', planner_prompt_custom = 0 WHERE agent_id = ?",
                        (profile.agent_id,),
                    )

    @staticmethod
    def _looks_like_legacy_prompt(prompt: str) -> bool:
        markers = ("普通概念", "每次只选择一个下一步工具", "metadata_search")
        return len(prompt) < 4000 and all(marker in prompt for marker in markers)

    @staticmethod
    def _load_catalog(path: Path) -> AgentCatalog:
        if not path.exists():
            return AgentCatalog(default_agent="default", agents=[AgentProfile(id="default", name="默认助手", use_legacy_data=True)])
        return AgentCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
