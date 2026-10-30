from __future__ import annotations

import logging
import shutil
import json
import tempfile
import asyncio
import functools
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent_registry import AgentRegistry, AgentRuntime
from app.auth import AuthenticatedUser, LdapAuthenticator, SessionManager, verify_bearer_key, verify_password
from app.clients.llm import LLMClient
from app.clients.pdf_platform import CommandPdfPlatformClient, HttpPdfPlatformClient, MockPdfPlatformClient
from app.config import Settings, get_settings
from app.ingestion import IngestionService, JobStore
from app.ingest_queue import IngestQueueManager, IngestQueueRepository
from app.delete_queue import DeleteQueueManager, DeleteQueueRepository
from app.concurrency import AskConcurrency
from app.document_parser import DOCUMENT_TYPES
from app.ocr import CommandOcrProvider
from app.schemas import (
    AgentConfigInput, AgentConfigView, AgentSummary, AskRequest, AskResponse, ConnectionTestResult,
    IngestJob, KnowledgeBaseInput, KnowledgeBaseView, RetrievalResponse, Source, SourcePage, MarkdownPreviewResponse,
    SourceMetadataUpdate, MetadataBatchRequest,
    BatchIngestCreate, LoginRequest, RewritePreviewRequest, RewritePreviewResponse,
    DeleteAllSourcesRequest, DeleteJob, TaskPage,
    ModelConnectionInput, ModelConnectionTestResult, ModelConnectionView,
)


ROOT = Path(__file__).resolve().parent.parent
# Keep the public build identifier compatible with existing deployment checks;
# feature additions are exposed separately in /api/health.
BUILD_VERSION = "2026.10.31-semi-platform-ui.1"
logger = logging.getLogger("agentic_rag.ingest")


def _source_base_url(request: Request, settings: Settings) -> str:
    configured = settings.app_public_base_url.strip()
    if configured:
        return configured.rstrip("/") + "/"

    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    if forwarded_prefix or forwarded_host:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
        host = (forwarded_host or request.headers.get("host", request.url.netloc)).split(",", 1)[0].strip()
        prefix = "/" + forwarded_prefix.strip("/") if forwarded_prefix.strip("/") else ""
        return f"{scheme}://{host}{prefix}/"

    # Browser requests carry the externally visible origin and page URL,
    # preserving a reverse proxy port and subpath such as /kb/.
    origin = request.headers.get("origin", "").strip()
    referer = request.headers.get("referer", "").strip()
    if origin and referer:
        origin_parts = urlsplit(origin)
        referer_parts = urlsplit(referer)
        if origin_parts.scheme in {"http", "https"} and referer_parts.netloc == origin_parts.netloc:
            page_path = referer_parts.path
            prefix = page_path if page_path.endswith("/") else page_path.rsplit("/", 1)[0] + "/"
            return origin.rstrip("/") + (prefix if prefix.startswith("/") else "/" + prefix)
    return str(request.base_url)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_directories()
    if settings.pdf_platform_mode == "http" and not settings.app_mock_external_services:
        pdf_platform = HttpPdfPlatformClient(settings.pdf_platform_base_url, settings.pdf_platform_api_key, settings.request_timeout_seconds)
    elif settings.pdf_platform_mode == "command" and not settings.app_mock_external_services:
        pdf_platform = CommandPdfPlatformClient(settings.pdf_platform_command, settings.pdf_platform_url_template)
    else:
        pdf_platform = MockPdfPlatformClient()
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_api_style, settings.request_timeout_seconds)
    ocr = CommandOcrProvider(settings.ocr_command) if settings.ocr_enabled else None
    registry = AgentRegistry(settings, llm, pdf_platform, ocr)
    default_runtime = registry.default()
    jobs = JobStore()
    ingest_queue_repository = IngestQueueRepository(settings.ingest_queue_database_path)
    ingest_queue = IngestQueueManager(
        ingest_queue_repository,
        registry.get,
        settings.ingest_worker_count,
        settings.ingest_max_retries,
    )
    delete_queue_repository = DeleteQueueRepository(settings.delete_queue_database_path)
    delete_queue = DeleteQueueManager(delete_queue_repository, registry.get)
    sessions = SessionManager(settings.admin_username, settings.admin_password_hash, settings.auth_session_hours)
    ldap = LdapAuthenticator(
        settings.ldap_enabled, settings.ldap_server_url, settings.ldap_user_dn_template,
        settings.ldap_bind_dn, settings.ldap_bind_password, settings.ldap_timeout_seconds,
        settings.ldap_base_dn, settings.ldap_user_filter,
    )
    ask_concurrency = AskConcurrency(settings.ask_concurrency_limit, settings.ask_queue_timeout_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ingest_queue.start()
        delete_queue.start()
        try:
            yield
        finally:
            ingest_queue.shutdown()
            delete_queue.shutdown()
            ask_concurrency.shutdown()

    app = FastAPI(title="Agentic RAG + JIT 文件检索", version=BUILD_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.repository = default_runtime.repository
    app.state.ingestion = default_runtime.ingestion
    app.state.tools = default_runtime.tools
    app.state.agent = default_runtime.agent
    app.state.jobs = jobs
    app.state.ingest_queue = ingest_queue
    app.state.ingest_queue_repository = ingest_queue_repository
    app.state.delete_queue = delete_queue
    app.state.delete_queue_repository = delete_queue_repository
    app.state.ask_concurrency = ask_concurrency

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if not settings.auth_enabled or not request.url.path.startswith("/api/"):
            return await call_next(request)
        public_paths = {"/api/health", "/api/auth/login", "/api/auth/session"}
        if request.url.path in public_paths:
            return await call_next(request)
        session_user = sessions.user(request.cookies.get(sessions.cookie_name))
        if session_user is not None:
            request.state.auth_user = session_user
            return await call_next(request)
        path_parts = request.url.path.strip("/").split("/")
        external_read_request = (
            (request.method == "GET" and request.url.path == "/api/agents")
            or (request.method == "GET" and request.url.path.startswith("/api/files/"))
            or (
                request.method == "POST"
                and (
                    request.url.path in {"/api/ask", "/api/retrieve"}
                    or (
                        len(path_parts) == 4
                        and path_parts[:2] == ["api", "agents"]
                        and path_parts[3] in {"ask", "retrieve"}
                    )
                )
            )
        )
        if external_read_request and settings.external_api_enabled:
            if verify_bearer_key(request.headers.get("authorization"), settings.external_api_key):
                request.state.auth_user = AuthenticatedUser("api", False, "api_key")
                return await call_next(request)
            return JSONResponse({"detail": "API Key 无效或缺失"}, status_code=401)
        return JSONResponse({"detail": "请先登录"}, status_code=401)

    @app.get("/api/auth/session")
    def auth_session(request: Request) -> dict[str, object]:
        user = sessions.user(request.cookies.get(sessions.cookie_name))
        authenticated = not settings.auth_enabled or user is not None
        return {"authenticated": authenticated, "username": user.username if user else (settings.admin_username if not settings.auth_enabled else ""), "is_admin": bool(user.is_admin) if user else not settings.auth_enabled}

    @app.post("/api/auth/login")
    def login(value: LoginRequest, response: Response) -> dict[str, object]:
        username_ok = __import__("hmac").compare_digest(value.username, settings.admin_username)
        if username_ok and verify_password(value.password, settings.admin_password_hash):
            user = AuthenticatedUser(settings.admin_username, True, "local")
        elif ldap.authenticate(value.username.strip(), value.password):
            user = AuthenticatedUser(value.username.strip(), False, "ldap")
        else:
            raise HTTPException(401, "账号或密码错误")
        response.set_cookie(
            sessions.cookie_name,
            sessions.create(user.username, user.is_admin, user.auth_source),
            max_age=sessions.lifetime_seconds,
            httponly=True,
            samesite="strict",
            secure=settings.auth_cookie_secure,
            path="/",
        )
        return {"authenticated": True, "username": user.username, "is_admin": user.is_admin}

    @app.post("/api/auth/logout", status_code=204)
    def logout(response: Response) -> None:
        response.delete_cookie(sessions.cookie_name, path="/")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "build_version": BUILD_VERSION,
            "mock_external_services": settings.app_mock_external_services,
            "llm_configured": any(
                item.agent.llm.configured or item.agent.answer_llm.configured
                for item in registry.runtimes()
            ),
            "model_connections": len(registry.list_model_connections()),
            "external_api_configured": bool(settings.external_api_enabled and settings.external_api_key),
            "agent_engine": "langgraph",
            "default_agent": registry.default_agent_id,
            "agents": len(registry.list()),
            "ripgrep_available": shutil.which("rg") is not None,
            "ask_concurrency_limit": settings.ask_concurrency_limit,
            "ask_active": ask_concurrency.active,
            "ask_rejected": ask_concurrency.rejected,
            "ldap_enabled": settings.ldap_enabled,
            "multi_user_auth": True,
        }

    def current_user(request: Request) -> AuthenticatedUser:
        user = getattr(request.state, "auth_user", None)
        if isinstance(user, AuthenticatedUser):
            return user
        return AuthenticatedUser(settings.admin_username, True, "local")

    def require_admin(request: Request) -> AuthenticatedUser:
        user = current_user(request)
        if not user.is_admin:
            raise HTTPException(403, "只有 sansuokb 管理员可以修改全局配置")
        return user

    def can_use_kb(request: Request, knowledge_base_id: str) -> bool:
        user = current_user(request)
        if user.is_admin or user.auth_source == "api_key":
            return True
        try:
            row = registry.store.get_knowledge_base(knowledge_base_id)
        except KeyError:
            return False
        return str(row.get("owner_username") or "") == user.username

    def authorize_agent(request: Request, agent_id: str) -> AgentRuntime:
        value = runtime(agent_id)
        if not can_use_kb(request, value.profile.knowledge_base_id):
            raise HTTPException(403, "无权访问该知识库")
        return value

    def runtime(agent_id: str) -> AgentRuntime:
        try:
            return registry.get(agent_id)
        except KeyError:
            raise HTTPException(404, "Agent 不存在") from None

    @app.get("/api/agents", response_model=list[AgentSummary])
    def list_agents(request: Request) -> list[AgentSummary]:
        user = current_user(request)
        values = registry.list()
        if user.is_admin or user.auth_source == "api_key":
            return values
        owned = {item.knowledge_base_id for item in registry.list_knowledge_bases(user.username)}
        return [item for item in values if registry.get(item.agent_id).profile.knowledge_base_id in owned]

    @app.get("/api/admin/knowledge-bases", response_model=list[KnowledgeBaseView])
    def list_knowledge_bases(request: Request) -> list[KnowledgeBaseView]:
        user = current_user(request)
        return registry.list_knowledge_bases(None if user.is_admin else user.username)

    @app.post("/api/admin/knowledge-bases/test", response_model=ConnectionTestResult)
    def test_knowledge_base(value: KnowledgeBaseInput, request: Request) -> ConnectionTestResult:
        require_admin(request)
        try:
            return registry.test_connection(value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/admin/knowledge-bases", response_model=KnowledgeBaseView)
    def create_knowledge_base(value: KnowledgeBaseInput, request: Request) -> KnowledgeBaseView:
        user = current_user(request)
        if not user.is_admin:
            value = value.model_copy(update={
                "base_url": settings.dify_base_url or value.base_url,
                "dataset_id": settings.dify_dataset_id or value.dataset_id,
                "api_key": None, "pdf_parser": "default", "qwenvl_base_url": "",
                "qwenvl_api_key": None, "source_platform_mode": "inherit",
                "source_platform_base_url": "", "source_platform_api_key": None,
                "source_platform_command": [], "source_platform_url_template": "",
                "workflow_enabled": False, "workflow_base_url": "", "workflow_api_key": None,
            })
        try:
            return registry.save_knowledge_base(value, owner_username=user.username, owner_user_id=("local:" if user.is_admin else "ldap:") + user.username)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/admin/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseView)
    def update_knowledge_base(knowledge_base_id: str, value: KnowledgeBaseInput, request: Request) -> KnowledgeBaseView:
        if value.knowledge_base_id != knowledge_base_id:
            raise HTTPException(400, "Knowledge base id 不可修改")
        if not can_use_kb(request, knowledge_base_id):
            raise HTTPException(403, "无权修改该知识库")
        user = current_user(request)
        if not user.is_admin:
            value = value.model_copy(update={
                "base_url": settings.dify_base_url or value.base_url, "dataset_id": settings.dify_dataset_id or value.dataset_id,
                "api_key": None, "pdf_parser": "default", "qwenvl_base_url": "", "qwenvl_api_key": None,
                "source_platform_mode": "inherit", "source_platform_base_url": "", "source_platform_api_key": None,
                "source_platform_command": [], "source_platform_url_template": "", "workflow_enabled": False,
                "workflow_base_url": "", "workflow_api_key": None,
            })
        try:
            return registry.save_knowledge_base(value, owner_username=user.username, owner_user_id=("local:" if user.is_admin else "ldap:") + user.username)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/admin/knowledge-bases/{knowledge_base_id}", status_code=204)
    def delete_knowledge_base(knowledge_base_id: str, request: Request) -> None:
        if not can_use_kb(request, knowledge_base_id):
            raise HTTPException(403, "无权删除该知识库")
        try:
            registry.delete_knowledge_base(knowledge_base_id)
        except KeyError:
            raise HTTPException(404, "知识库不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/admin/agents", response_model=list[AgentConfigView])
    def list_agent_configs(request: Request) -> list[AgentConfigView]:
        user = current_user(request)
        values = registry.list_agent_configs()
        if user.is_admin:
            return values
        owned = {item.knowledge_base_id for item in registry.list_knowledge_bases(user.username)}
        return [item for item in values if item.knowledge_base_id in owned]

    @app.post("/api/admin/agents", response_model=AgentConfigView)
    def create_agent(value: AgentConfigInput, request: Request) -> AgentConfigView:
        user = current_user(request)
        if not can_use_kb(request, value.knowledge_base_id):
            raise HTTPException(403, "只能为自己的知识库创建 Agent")
        if not user.is_admin:
            value = value.model_copy(update={"planner_model_ids": [], "answer_model_ids": [], "is_default": False})
        try:
            return registry.save_agent(value)
        except KeyError:
            raise HTTPException(400, "绑定的知识库不存在") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/admin/agents/{agent_id}", response_model=AgentConfigView)
    def update_agent(agent_id: str, value: AgentConfigInput, request: Request) -> AgentConfigView:
        if value.agent_id != agent_id:
            raise HTTPException(400, "Agent id 不可修改")
        return create_agent(value, request)

    @app.delete("/api/admin/agents/{agent_id}", status_code=204)
    def delete_agent(agent_id: str, request: Request) -> None:
        try:
            if not can_use_kb(request, registry.store.get_agent(agent_id).knowledge_base_id):
                raise HTTPException(403, "无权删除该 Agent")
        except KeyError:
            raise HTTPException(404, "Agent 不存在") from None
        try:
            registry.delete_agent(agent_id)
        except KeyError:
            raise HTTPException(404, "Agent 不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/admin/model-connections", response_model=list[ModelConnectionView])
    def list_model_connections(request: Request) -> list[ModelConnectionView]:
        if not current_user(request).is_admin:
            return []
        return registry.list_model_connections()

    @app.post("/api/admin/model-connections/test", response_model=ModelConnectionTestResult)
    def test_model_connection(value: ModelConnectionInput, request: Request) -> ModelConnectionTestResult:
        require_admin(request)
        try:
            return registry.test_model_connection(value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/admin/model-connections", response_model=ModelConnectionView)
    def create_model_connection(value: ModelConnectionInput, request: Request) -> ModelConnectionView:
        require_admin(request)
        try:
            return registry.save_model_connection(value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/admin/model-connections/{model_connection_id}", response_model=ModelConnectionView)
    def update_model_connection(model_connection_id: str, value: ModelConnectionInput, request: Request) -> ModelConnectionView:
        if value.model_connection_id != model_connection_id:
            raise HTTPException(400, "Model connection id 不可修改")
        return create_model_connection(value, request)

    @app.delete("/api/admin/model-connections/{model_connection_id}", status_code=204)
    def delete_model_connection(model_connection_id: str, request: Request) -> None:
        require_admin(request)
        try:
            registry.delete_model_connection(model_connection_id)
        except KeyError:
            raise HTTPException(404, "模型连接不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/agents/{agent_id}/ask", response_model=AskResponse)
    async def ask_agent(agent_id: str, request: AskRequest, http_request: Request) -> AskResponse:
        agent_runtime = authorize_agent(http_request, agent_id)
        try:
            async with ask_concurrency.acquire(request.conversation_id):
                return await asyncio.get_running_loop().run_in_executor(
                    ask_concurrency.executor,
                    functools.partial(agent_runtime.agent.ask,
                    request.question.strip(), _source_base_url(http_request, settings),
                    request.conversation_id, request.history),
                )
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(429, "问答请求较多，请稍后重试", headers={"Retry-After": "3"}) from None

    @app.post("/api/agents/{agent_id}/retrieve", response_model=RetrievalResponse)
    async def retrieve_agent(agent_id: str, request: AskRequest, http_request: Request) -> RetrievalResponse:
        agent_runtime = authorize_agent(http_request, agent_id)
        try:
            async with ask_concurrency.acquire(request.conversation_id):
                return await asyncio.get_running_loop().run_in_executor(
                    ask_concurrency.executor,
                    functools.partial(agent_runtime.agent.retrieve,
                    request.question.strip(), _source_base_url(http_request, settings),
                    request.conversation_id, request.history),
                )
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(429, "检索请求较多，请稍后重试", headers={"Retry-After": "3"}) from None

    @app.get("/api/agents/{agent_id}/sources", response_model=list[Source])
    def list_agent_sources(agent_id: str, request: Request, limit: int = 100, offset: int = 0) -> list[Source]:
        return authorize_agent(request, agent_id).repository.list(min(max(limit, 1), 500), max(offset, 0))

    @app.get("/api/agents/{agent_id}/sources/page", response_model=SourcePage)
    def page_agent_sources(
        agent_id: str,
        request: Request,
        page: int = 1,
        page_size: int = 30,
        query: str = "",
        parse_status: str = "",
        dify_status: str = "",
        document_type: str = "",
        created_from: str = "",
        created_to: str = "",
    ) -> SourcePage:
        agent_runtime = authorize_agent(request, agent_id)
        page = max(1, page)
        page_size = min(max(page_size, 1), 100)
        items, total = agent_runtime.repository.list_page(
            page, page_size, query, parse_status, dify_status, document_type, created_from, created_to
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
            items, _ = agent_runtime.repository.list_page(
                page, page_size, query, parse_status, dify_status, document_type, created_from, created_to
            )
        return SourcePage(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)

    @app.get("/api/agents/{agent_id}/sources/stats")
    def agent_source_stats(agent_id: str, request: Request) -> dict[str, int]:
        return authorize_agent(request, agent_id).repository.stats()

    @app.get("/api/agents/{agent_id}/sources/{source_id}/markdown", response_model=MarkdownPreviewResponse)
    def preview_agent_markdown(agent_id: str, source_id: str, request: Request, version: str = "document") -> MarkdownPreviewResponse:
        try:
            source = authorize_agent(request, agent_id).repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        source_dir = Path(source.source_path).resolve().parent
        versions: dict[str, Path] = {
            "document": source_dir / "document.md",
            "extracted": source_dir / "extracted.md",
            "preview": source_dir / "preview.md",
            "workflow": source_dir / "workflow.md",
            "mineru-original": source_dir / "mineru-original.md",
        }
        batches_dir = source_dir / "vision-batches"
        if batches_dir.exists():
            for path in sorted(batches_dir.glob("batch-*.md")):
                versions[path.stem] = path
        available = [name for name, path in versions.items() if path.is_file()]
        if version == "document" and version not in available and available:
            version = available[0]
        if version not in versions or version not in available:
            raise HTTPException(404, "Markdown 版本不存在")
        path = versions[version].resolve()
        if source_dir not in path.parents:
            raise HTTPException(400, "无效 Markdown 路径")
        markdown = path.read_text(encoding="utf-8", errors="replace")
        stats = {
            "characters": len(markdown), "lines": len(markdown.splitlines()),
            "empty": not bool(markdown.strip()), "unreadable_marks": markdown.count("[无法辨认]"),
            "replacement_characters": markdown.count("�"),
        }
        metadata_path = source_dir / "metadata.json"
        if metadata_path.is_file():
            try:
                parser_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                stats.update({
                    "parser_engine": parser_metadata.get("parser_engine", ""),
                    "parser_requested": parser_metadata.get("parser_requested", ""),
                    "mineru_response_field": parser_metadata.get("mineru_response_field", ""),
                    "mineru_error": parser_metadata.get("mineru_error", ""),
                })
            except (OSError, json.JSONDecodeError):
                pass
        return MarkdownPreviewResponse(
            source_id=source.source_id, filename=source.filename, version=version,
            markdown=markdown, available_versions=available, stats=stats,
        )

    @app.get("/api/agents/{agent_id}/sources/{source_id}", response_model=Source)
    def get_agent_source(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            return authorize_agent(request, agent_id).repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/retry", response_model=Source)
    def retry_agent_source(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            return authorize_agent(request, agent_id).ingestion.retry_failed(source_id, source_platform_user=current_user(request).username)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/reparse", response_model=Source)
    def reparse_agent_source(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            return authorize_agent(request, agent_id).ingestion.reparse_source(source_id, source_platform_user=current_user(request).username)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except Exception as exc:
            raise HTTPException(502, f"重新解析失败：{str(exc)[:500]}") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/refresh-dify-status", response_model=Source)
    def refresh_agent_dify_status(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            return authorize_agent(request, agent_id).ingestion.refresh_dify_status(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.get("/api/agents/{agent_id}/sources/{source_id}/rewrite-preview", response_model=RewritePreviewResponse)
    def get_agent_rewrite_preview(agent_id: str, source_id: str, request: Request) -> RewritePreviewResponse:
        try:
            return RewritePreviewResponse.model_validate(authorize_agent(request, agent_id).ingestion.get_rewrite_preview(source_id))
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/rewrite-preview", response_model=RewritePreviewResponse)
    def regenerate_agent_rewrite_preview(agent_id: str, source_id: str, value: RewritePreviewRequest, request: Request) -> RewritePreviewResponse:
        try:
            result = authorize_agent(request, agent_id).ingestion.regenerate_rewrite_preview(source_id, value.rewrite_prompt)
            return RewritePreviewResponse.model_validate(result)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/confirm-rewrite", response_model=Source)
    def confirm_agent_rewrite(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            source = authorize_agent(request, agent_id).ingestion.confirm_rewrite(source_id, source_platform_user=current_user(request).username)
            ingest_queue_repository.complete_source(source_id)
            return source
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/use-original", response_model=Source)
    def use_agent_original(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            source = authorize_agent(request, agent_id).ingestion.use_original(source_id, source_platform_user=current_user(request).username)
            ingest_queue_repository.complete_source(source_id)
            return source
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/api/agents/{agent_id}/sources/{source_id}", status_code=204)
    def delete_agent_source(agent_id: str, source_id: str, request: Request) -> None:
        try:
            authorize_agent(request, agent_id).ingestion.delete_source(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except Exception as exc:
            raise HTTPException(502, f"删除失败：{str(exc)[:300]}") from None

    @app.get("/api/agents/{agent_id}/delete-job/active", response_model=DeleteJob | None)
    def get_active_delete_job(agent_id: str, request: Request) -> DeleteJob | None:
        authorize_agent(request, agent_id)
        return delete_queue_repository.active_for_agent(agent_id)

    @app.post("/api/agents/{agent_id}/sources/delete-all", response_model=DeleteJob, status_code=202)
    def delete_all_agent_sources(agent_id: str, value: DeleteAllSourcesRequest, request: Request) -> DeleteJob:
        runtime_value = authorize_agent(request, agent_id)
        profile = runtime_value.profile
        knowledge_base = next(
            (
                item for item in registry.list_knowledge_bases()
                if item.knowledge_base_id == profile.knowledge_base_id
            ),
            None,
        )
        if knowledge_base is None:
            raise HTTPException(409, "Agent 绑定的知识库不存在")
        if value.confirmation.strip() != knowledge_base.name:
            raise HTTPException(400, "确认名称不匹配，请完整输入当前知识库名称")
        try:
            ingest_queue_repository.cancel_inactive_jobs_for_bulk_delete(agent_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        try:
            return delete_queue_repository.create(
                agent_id,
                knowledge_base.knowledge_base_id,
                knowledge_base.name,
                runtime_value.repository.list_all(),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/delete-jobs/{job_id}", response_model=DeleteJob)
    def get_delete_job(job_id: str, request: Request) -> DeleteJob:
        try:
            job = delete_queue_repository.get(job_id)
            if not can_use_kb(request, job.knowledge_base_id):
                raise HTTPException(403, "无权访问该清空任务")
            return job
        except KeyError:
            raise HTTPException(404, "清空任务不存在") from None

    @app.put("/api/agents/{agent_id}/sources/{source_id}/metadata", response_model=Source)
    def update_agent_source_metadata(agent_id: str, source_id: str, value: SourceMetadataUpdate, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        try:
            return runtime_value.repository.update(
                source_id,
                **value.model_dump(exclude_unset=True),
                metadata_status="manual",
                metadata_error=None,
            )
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/extract-metadata", response_model=Source)
    def extract_agent_source_metadata(agent_id: str, source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        try:
            source = runtime_value.repository.get(source_id)
            markdown = Path(source.local_md_path).read_text(encoding="utf-8")
            runtime_value.ingestion._extract_custom_metadata(source, markdown)
            return runtime_value.repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/agents/{agent_id}/sources/extract-metadata-batch")
    def extract_agent_sources_metadata_batch(agent_id: str, value: MetadataBatchRequest, request: Request) -> dict[str, object]:
        runtime_value = authorize_agent(request, agent_id)
        sources = runtime_value.repository.list(500)
        selected = [item for item in sources if not value.source_ids or item.source_id in set(value.source_ids)]
        results: list[dict[str, object]] = []
        for source in selected:
            try:
                markdown = Path(source.local_md_path).read_text(encoding="utf-8")
                runtime_value.ingestion._extract_custom_metadata(source, markdown)
                refreshed = runtime_value.repository.get(source.source_id)
                results.append({"source_id": source.source_id, "filename": source.filename, "status": refreshed.metadata_status, "error": refreshed.metadata_error})
            except Exception as exc:
                results.append({"source_id": source.source_id, "filename": source.filename, "status": "failed", "error": str(exc)[:300]})
        return {"total": len(results), "success": sum(item["status"] == "success" for item in results), "failed": sum(item["status"] == "failed" for item in results), "items": results}

    @app.get("/api/sources", response_model=list[Source])
    def list_sources(limit: int = 100, offset: int = 0) -> list[Source]:
        return registry.default().repository.list(min(max(limit, 1), 500), max(offset, 0))

    @app.get("/api/sources/{source_id}", response_model=Source)
    def get_source(source_id: str) -> Source:
        try:
            return registry.default().repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/sources/{source_id}/retry", response_model=Source)
    def retry_source(source_id: str) -> Source:
        try:
            return registry.default().ingestion.retry_failed(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/sources/{source_id}/refresh-dify-status", response_model=Source)
    def refresh_dify_status(source_id: str) -> Source:
        try:
            return registry.default().ingestion.refresh_dify_status(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.delete("/api/sources/{source_id}", status_code=204)
    def delete_source(source_id: str) -> None:
        try:
            registry.default().ingestion.delete_source(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except Exception as exc:
            raise HTTPException(502, f"删除失败：{str(exc)[:300]}") from None

    @app.post("/api/ingest", response_model=IngestJob, status_code=202)
    async def ingest_files(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...), rewrite_enabled: bool = Form(False), rewrite_prompt: str = Form(""), workflow_enabled: bool = Form(False), rewrite_review_required: bool = Form(False)) -> IngestJob:
        return await _start_ingest(background_tasks, files, registry.default().ingestion, jobs, rewrite_enabled, rewrite_prompt, workflow_enabled, rewrite_review_required, "sansuokb")

    @app.post("/api/agents/{agent_id}/ingest", response_model=IngestJob, status_code=202)
    async def ingest_agent_files(
        agent_id: str, request: Request, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...), rewrite_enabled: bool = Form(False), rewrite_prompt: str = Form(""), workflow_enabled: bool = Form(False), rewrite_review_required: bool = Form(False)
    ) -> IngestJob:
        agent_runtime = authorize_agent(request, agent_id)
        return await _start_ingest(background_tasks, files, agent_runtime.ingestion, jobs, rewrite_enabled, rewrite_prompt, workflow_enabled, rewrite_review_required, current_user(request).username)

    @app.post("/api/agents/{agent_id}/ingest-jobs", response_model=IngestJob, status_code=201)
    def create_ingest_job(agent_id: str, value: BatchIngestCreate, request: Request) -> IngestJob:
        authorize_agent(request, agent_id)
        if len(value.filenames) > settings.ingest_max_files_per_job:
            raise HTTPException(400, f"单个任务最多 {settings.ingest_max_files_per_job} 个文件")
        invalid = [name for name in value.filenames if Path(name).suffix.casefold() not in DOCUMENT_TYPES]
        if invalid:
            raise HTTPException(400, f"存在不支持的文件格式：{Path(invalid[0]).name}")
        return ingest_queue_repository.create(agent_id, value)

    @app.post("/api/jobs/{job_id}/upload-batch", response_model=IngestJob)
    async def upload_ingest_batch(
        job_id: str,
        request: Request,
        files: list[UploadFile] = File(...),
        positions: str = Form(...),
    ) -> IngestJob:
        try:
            indexes = json.loads(positions)
        except json.JSONDecodeError:
            raise HTTPException(400, "positions 必须是 JSON 数组") from None
        if not isinstance(indexes, list) or len(indexes) != len(files) or not all(isinstance(value, int) for value in indexes):
            raise HTTPException(400, "positions 必须与 files 一一对应")
        if len(files) > settings.ingest_upload_batch_size:
            raise HTTPException(400, f"每批最多上传 {settings.ingest_upload_batch_size} 个文件")
        try:
            job = ingest_queue_repository.get(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        if job.status != "uploading":
            raise HTTPException(409, "任务已开始处理，不能继续上传")
        # Resolve the job's agent and enforce ownership before accepting bytes.
        job_record = ingest_queue_repository.job_record(job_id)
        authorize_agent(request, str(job_record["agent_id"]))
        for upload, position in zip(files, indexes):
            try:
                item = ingest_queue_repository.item_for_position(job_id, position)
            except KeyError:
                raise HTTPException(400, f"文件位置不存在：{position}") from None
            filename = Path(upload.filename or "").name
            if filename != item["filename"]:
                raise HTTPException(400, f"文件顺序不匹配：预期 {item['filename']}，收到 {filename}")
            target_dir = settings.ingest_staging_dir / job_id / item["item_id"]
            target_dir.mkdir(parents=True, exist_ok=True)
            # Ingestion derives the user-visible name from input_path.name.
            # Each item has its own directory, so retaining the sanitized
            # original name is safe and prevents every queued file becoming
            # source.pdf/source.md in metadata and Dify.
            target = target_dir / filename
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    handle.write(chunk)
            ingest_queue_repository.mark_uploaded(item["item_id"], target)
        return ingest_queue_repository.get(job_id)

    @app.post("/api/jobs/{job_id}/start", response_model=IngestJob)
    def start_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        authorize_agent(request, str(job_record["agent_id"]))
        try:
            return ingest_queue_repository.start(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/tasks", response_model=TaskPage)
    def list_tasks(
        request: Request,
        page: int = 1,
        page_size: int = 30,
        status: str = "",
        task_type: str = "",
        agent_id: str = "",
    ) -> TaskPage:
        page = max(1, page)
        page_size = min(max(page_size, 1), 100)
        visible_agent_ids = {
            item.agent_id for item in registry.list()
            if can_use_kb(request, registry.get(item.agent_id).profile.knowledge_base_id)
        }
        if agent_id:
            if agent_id not in visible_agent_ids:
                raise HTTPException(403, "无权访问该 Agent 的任务")
            visible_agent_ids = {agent_id}
        values = []
        if task_type in {"", "document_ingest"}:
            values.extend(ingest_queue_repository.task_summaries(visible_agent_ids))
        if task_type in {"", "document_delete"}:
            values.extend(delete_queue_repository.task_summaries(visible_agent_ids))
        if status:
            expected = status.strip().upper()
            values = [item for item in values if item.status == expected]
        values.sort(key=lambda item: item.updated_at, reverse=True)
        total = len(values)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        return TaskPage(
            items=values[start:start + page_size], page=page, page_size=page_size,
            total=total, total_pages=total_pages,
        )

    @app.post("/api/jobs/{job_id}/pause", response_model=IngestJob)
    def pause_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        authorize_agent(request, str(job_record["agent_id"]))
        try:
            return ingest_queue_repository.pause(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None

    @app.post("/api/jobs/{job_id}/resume", response_model=IngestJob)
    def resume_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        authorize_agent(request, str(job_record["agent_id"]))
        try:
            return ingest_queue_repository.resume(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None

    @app.post("/api/jobs/{job_id}/retry-failed", response_model=IngestJob)
    def retry_failed_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        authorize_agent(request, str(job_record["agent_id"]))
        try:
            return ingest_queue_repository.retry_failed(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None

    @app.get("/api/jobs/{job_id}", response_model=IngestJob)
    def get_job(job_id: str, request: Request) -> IngestJob:
        try:
            record = ingest_queue_repository.job_record(job_id)
            authorize_agent(request, str(record["agent_id"]))
            return ingest_queue_repository.get(job_id)
        except KeyError:
            try:
                return jobs.get(job_id)
            except KeyError:
                raise HTTPException(404, "任务不存在") from None

    @app.post("/api/ask", response_model=AskResponse)
    async def ask(request: AskRequest, http_request: Request) -> AskResponse:
        try:
            async with ask_concurrency.acquire(request.conversation_id):
                return await asyncio.get_running_loop().run_in_executor(
                    ask_concurrency.executor,
                    functools.partial(registry.default().agent.ask,
                    request.question.strip(), _source_base_url(http_request, settings),
                    request.conversation_id, request.history),
                )
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(429, "问答请求较多，请稍后重试", headers={"Retry-After": "3"}) from None

    @app.post("/api/retrieve", response_model=RetrievalResponse)
    async def retrieve(request: AskRequest, http_request: Request) -> RetrievalResponse:
        try:
            async with ask_concurrency.acquire(request.conversation_id):
                return await asyncio.get_running_loop().run_in_executor(
                    ask_concurrency.executor,
                    functools.partial(registry.default().agent.retrieve,
                    request.question.strip(), _source_base_url(http_request, settings),
                    request.conversation_id, request.history),
                )
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(429, "检索请求较多，请稍后重试", headers={"Retry-After": "3"}) from None

    @app.get("/api/files/{file_id}")
    def serve_source_file(file_id: str, request: Request) -> FileResponse:
        source = None
        matched_runtime = None
        for agent_runtime in registry.runtimes():
            candidate = next((item for item in agent_runtime.repository.list(10000)
                              if item.source_id == file_id or item.pdf_platform_id == file_id), None)
            if candidate:
                source, matched_runtime = candidate, agent_runtime
                break
        if source is None:
            raise HTTPException(404, "文件不存在")
        if matched_runtime is not None and not can_use_kb(request, matched_runtime.profile.knowledge_base_id):
            raise HTTPException(403, "无权访问该文件")
        return FileResponse(source.source_path, media_type=source.mime_type, filename=source.filename)

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
    return app


async def _start_ingest(
    background_tasks: BackgroundTasks,
    files: list[UploadFile],
    ingestion: IngestionService,
    jobs: JobStore,
    rewrite_enabled: bool = False,
    rewrite_prompt: str = "",
    workflow_enabled: bool = False,
    rewrite_review_required: bool = False,
    source_platform_user: str = "sansuokb",
) -> IngestJob:
    if not files:
        raise HTTPException(400, "至少上传一个文件")
    temp_dir = Path(tempfile.mkdtemp(prefix="agentic-rag-upload-"))
    paths: list[Path] = []
    for index, upload in enumerate(files):
        filename = Path(upload.filename or f"upload-{index}").name
        if Path(filename).suffix.casefold() not in DOCUMENT_TYPES:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(400, f"仅支持 PDF、DOC、DOCX、TXT：{filename}")
        target = temp_dir / f"{index:04d}" / filename
        target.parent.mkdir()
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                handle.write(chunk)
        paths.append(target)
    job = jobs.create([path.name for path in paths])
    background_tasks.add_task(_run_job, jobs, ingestion, job.job_id, paths, temp_dir, rewrite_enabled, rewrite_prompt, workflow_enabled, rewrite_review_required, source_platform_user)
    return job


def _run_job(jobs: JobStore, ingestion: IngestionService, job_id: str, paths: list[Path], temp_dir: Path, rewrite_enabled: bool = False, rewrite_prompt: str = "", workflow_enabled: bool = False, rewrite_review_required: bool = False, source_platform_user: str = "sansuokb") -> None:
    jobs.mutate(job_id, lambda job: setattr(job, "status", "running"))
    logger.info("legacy_ingest_job_started job_id=%s files=%d", job_id, len(paths))
    try:
        for index, path in enumerate(paths):
            jobs.mutate(job_id, lambda job, i=index: setattr(job.items[i], "status", "running"))
            try:
                source, duplicate = ingestion.ingest(path, rewrite_enabled, rewrite_prompt, workflow_enabled, rewrite_review_required, source_platform_user)
                def mark_success(job: IngestJob, i: int = index, item: Source = source, is_duplicate: bool = duplicate) -> None:
                    job.items[i].source_id = item.source_id
                    all_success = all(status == "success" for status in (item.parse_status, item.dify_status, item.pdf_platform_status))
                    if item.rewrite_status == "preview_ready":
                        job.items[i].status = "preview_ready"
                        job.items[i].message = "AI 改写完成，等待预览确认"
                    else:
                        job.items[i].status = ("duplicate" if is_duplicate else "success") if all_success else "failed"
                        job.items[i].message = item.error_message or "入库完成"
                    job.completed += 1
                jobs.mutate(job_id, mark_success)
                if all(status == "success" for status in (source.parse_status, source.dify_status, source.pdf_platform_status)):
                    logger.info(
                        "legacy_ingest_succeeded job_id=%s filename=%r source_id=%s duplicate=%s",
                        job_id, path.name, source.source_id, duplicate,
                    )
                else:
                    logger.error(
                        "legacy_ingest_failed job_id=%s filename=%r source_id=%s error=%r",
                        job_id, path.name, source.source_id, source.error_message or "入库流程未全部完成",
                    )
            except Exception as exc:
                def mark_failed(job: IngestJob, i: int = index, error: Exception = exc) -> None:
                    job.items[i].status = "failed"
                    job.items[i].message = str(error)[:500]
                    job.completed += 1
                jobs.mutate(job_id, mark_failed)
                logger.exception(
                    "legacy_ingest_failed job_id=%s filename=%r error=%r",
                    job_id, path.name, str(exc)[:500],
                )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        jobs.mutate(job_id, lambda job: setattr(job, "status", "completed"))
        logger.info("legacy_ingest_job_completed job_id=%s", job_id)


app = create_app()
