from __future__ import annotations

import logging
import shutil
import json
import tempfile
import asyncio
import time
import re
import sqlite3
import uuid
import base64
import csv
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent_registry import AgentRegistry, AgentRuntime
from app.agent import ToolApprovalRequired, agent_execution_context
from app.agent_runs import AgentRunStore
from app.audit import AuditStore, classify
from app.auth import AuthenticatedUser, LdapAuthenticator, SessionManager, verify_bearer_key, verify_password
from app.clients.llm import LLMClient
from app.clients.pdf_platform import CommandPdfPlatformClient, HttpPdfPlatformClient, MockPdfPlatformClient
from app.config import Settings, get_settings
from app.ingestion import IngestionService, JobStore
from app.ingest_queue import IngestQueueManager, IngestQueueRepository
from app.delete_queue import DeleteQueueManager, DeleteQueueRepository
from app.sandbox import SandboxPolicy, SandboxRequest, SandboxStore
from app.metrics import MetricsStore
from app.access import PERMISSIONS, AccessStore, RoleInput, UserRoleInput
from app.organizations import MemberInput, OrganizationInput, OrganizationStore
from app.ldap_config import LdapConfigInput, LdapConfigStore, LdapTestInput
from app.concurrency import AskConcurrency
from app.integrations import ExternalToolCall, IntegrationCreate, IntegrationPermissions, IntegrationStore
from app.tool_registry import TOOL_DESCRIPTIONS, WRITE_TOOLS, ToolApprovalStore, ToolCallStore, ToolConfigInput, ToolConfigStore, describe_tools, input_model
from app.retrieval import PAGE_MARKER, locate_exact_segment
from pydantic import ValidationError
from app.document_parser import DOCUMENT_TYPES
from app.ocr import CommandOcrProvider
from app.schemas import (
    AgentConfigInput, AgentConfigView, AgentSummary, AskRequest, AskResponse, AskHistoryMessage, ConnectionTestResult,
    IngestJob, KnowledgeBaseInput, KnowledgeBaseView, RetrievalResponse, Source, SourcePage, MarkdownPreviewResponse,
    SourceMetadataUpdate, MetadataBatchRequest,
    BatchIngestCreate, BatchReparseCreate, LoginRequest, RewritePreviewRequest, RewritePreviewResponse,
    DeleteAllSourcesRequest, DeleteJob, TaskPage,
    ModelConnectionInput, ModelConnectionTestResult, ModelConnectionView, SourceVersion, RetrievalTestRequest,
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
    ldap_config_store = LdapConfigStore(settings.app_data_dir / "ldap-config.json")
    persisted_ldap = ldap_config_store.load()
    if persisted_ldap:
        settings.ldap_enabled = bool(persisted_ldap.get("enabled", settings.ldap_enabled))
        settings.ldap_server_url = str(persisted_ldap.get("server_url", settings.ldap_server_url))
        settings.ldap_user_dn_template = str(persisted_ldap.get("user_dn_template", settings.ldap_user_dn_template))
        settings.ldap_base_dn = str(persisted_ldap.get("base_dn", settings.ldap_base_dn))
        settings.ldap_user_filter = str(persisted_ldap.get("user_filter", settings.ldap_user_filter))
        settings.ldap_group_base_dn = str(persisted_ldap.get("group_base_dn", settings.ldap_group_base_dn))
        settings.ldap_group_filter = str(persisted_ldap.get("group_filter", settings.ldap_group_filter))
        settings.ldap_group_attribute = str(persisted_ldap.get("group_attribute", settings.ldap_group_attribute))
        settings.ldap_group_role_map = dict(persisted_ldap.get("group_role_map", settings.ldap_group_role_map) or {})
        settings.ldap_bind_dn = str(persisted_ldap.get("bind_dn", settings.ldap_bind_dn))
        settings.ldap_bind_password = str(persisted_ldap.get("bind_password", settings.ldap_bind_password))
        settings.ldap_timeout_seconds = float(persisted_ldap.get("timeout_seconds", settings.ldap_timeout_seconds))
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
        settings.ldap_base_dn, settings.ldap_user_filter, settings.ldap_group_base_dn,
        settings.ldap_group_filter, settings.ldap_group_attribute,
    )
    ask_concurrency = AskConcurrency(settings.ask_concurrency_limit, settings.ask_queue_timeout_seconds)
    integrations = IntegrationStore(settings.app_data_dir / "integrations.db")
    agent_runs = AgentRunStore(settings.app_data_dir / "agent-runs.db")
    audit = AuditStore(settings.app_data_dir / "audit.db")
    sandbox = SandboxStore(settings.app_data_dir / "sandbox.db")
    tool_calls = ToolCallStore(settings.app_data_dir / "tool-calls.db")
    tool_configs = ToolConfigStore(settings.app_data_dir / "tool-registry.db")
    tool_approvals = ToolApprovalStore(settings.app_data_dir / "tool-approvals.db")
    metrics = MetricsStore(settings.app_data_dir / "metrics.db")
    access = AccessStore(settings.app_data_dir / "access.db", f"local:{settings.admin_username}")
    organizations = OrganizationStore(settings.app_data_dir / "organizations.db")
    mcp_manager = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ingest_queue.start()
        delete_queue.start()
        from app.search.worker import IndexWorker
        index_worker = IndexWorker(registry) if settings.local_index_enabled and not settings.app_mock_external_services else None
        if index_worker:
            index_worker.start()
        try:
            async with AsyncExitStack() as stack:
                if mcp_manager is not None:
                    await stack.enter_async_context(mcp_manager.run())
                yield
        finally:
            ingest_queue.shutdown()
            delete_queue.shutdown()
            if index_worker:
                index_worker.shutdown()
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
        # These endpoints authenticate their own scoped bearer credentials,
        # including in local development when session authentication is off.
        if request.url.path.startswith("/api/external/v1/") or request.url.path in {"/api/mcp", "/api/mcp/"}:
            return await call_next(request)
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

    @app.middleware("http")
    async def audit_requests(request: Request, call_next):
        event = classify(request.method, request.url.path)
        metrics_started = time.monotonic()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            try:
                metrics.observe(request.method, request.url.path, status_code, int((time.monotonic() - metrics_started) * 1000))
            except Exception:
                logger.exception("Unable to persist request metric")
            if event:
                user = getattr(request.state, "auth_user", None)
                if isinstance(user, AuthenticatedUser):
                    actor = f"{user.auth_source}:{user.username}"
                else:
                    actor = f"local:{settings.admin_username}" if not settings.auth_enabled else "anonymous"
                try:
                    await asyncio.to_thread(audit.record, actor, *event, status_code)
                except Exception:
                    logger.exception("Unable to persist audit event")
        return response

    @app.get("/api/auth/session")
    def auth_session(request: Request) -> dict[str, object]:
        user = sessions.user(request.cookies.get(sessions.cookie_name))
        authenticated = not settings.auth_enabled or user is not None
        effective = user or AuthenticatedUser(settings.admin_username, not settings.auth_enabled, "local")
        return {"authenticated": authenticated, "username": effective.username if authenticated else "", "is_admin": bool(effective.is_admin), "permissions": sorted(access.permissions(user_id(effective), effective.is_admin)) if authenticated else []}

    @app.post("/api/auth/login")
    def login(value: LoginRequest, response: Response, request: Request) -> dict[str, object]:
        username_ok = __import__("hmac").compare_digest(value.username, settings.admin_username)
        if username_ok and verify_password(value.password, settings.admin_password_hash):
            user = AuthenticatedUser(settings.admin_username, True, "local")
        else:
            ldap_result = ldap.authenticate_with_groups(value.username.strip(), value.password)
            if ldap_result is None and ldap.authenticate(value.username.strip(), value.password):
                from app.auth import LdapAuthentication
                ldap_result = LdapAuthentication(value.username.strip())
            if ldap_result is None:
                raise HTTPException(401, "账号或密码错误")
            user = AuthenticatedUser(value.username.strip(), False, "ldap")
            try:
                mapped_role = next((role for group in ldap_result.groups for mapped_group, role in settings.ldap_group_role_map.items() if mapped_group.casefold() == group.casefold()), 'viewer')
                access.assign(UserRoleInput(user_id=user_id(user), role_name=mapped_role))
            except Exception:
                logger.exception('Unable to provision default viewer role')
        request.state.auth_user = user
        response.set_cookie(
            sessions.cookie_name,
            sessions.create(user.username, user.is_admin, user.auth_source),
            max_age=sessions.lifetime_seconds,
            httponly=True,
            samesite="strict",
            secure=settings.auth_cookie_secure,
            path="/",
        )
        return {"authenticated": True, "username": user.username, "is_admin": user.is_admin, "permissions": sorted(access.permissions(user_id(user), user.is_admin))}

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
            "mcp_available": mcp_manager is not None,
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

    def require_permission(request: Request, permission: str) -> AuthenticatedUser:
        user = current_user(request)
        if permission not in access.permissions(user_id(user), user.is_admin):
            raise HTTPException(403, f"缺少权限：{permission}")
        return user

    def require_any_permission(request: Request, *permissions: str) -> AuthenticatedUser:
        user = current_user(request)
        granted = access.permissions(user_id(user), user.is_admin)
        if not granted.intersection(permissions):
            raise HTTPException(403, f"缺少权限：{' 或 '.join(permissions)}")
        return user

    @app.get('/api/admin/access')
    def access_overview(request: Request) -> dict:
        require_admin(request)
        return {'permissions': list(PERMISSIONS), 'roles': access.roles(), 'users': access.users()}

    @app.get('/api/admin/tools')
    def admin_tools(request: Request) -> list[dict[str, object]]:
        require_admin(request)
        configs = {str(item['tool_name']): item for item in tool_configs.list()}
        return [{**configs.get(str(item['name']), {}), **item} for item in describe_tools(set(TOOL_DESCRIPTIONS), configs)]

    @app.put('/api/admin/tools/{tool_name}')
    def update_admin_tool(tool_name: str, value: ToolConfigInput, request: Request) -> dict[str, object]:
        require_admin(request)
        try:
            old = tool_configs.get(tool_name)
            return tool_configs.update(tool_name, value.enabled, bool(old['requires_approval']) if value.requires_approval is None else value.requires_approval, 'tool.invoke')
        except KeyError:
            raise HTTPException(404, '工具不存在') from None

    @app.get('/api/admin/tool-approvals')
    def list_tool_approvals(request: Request) -> list[dict]:
        require_admin(request)
        return tool_approvals.list()

    @app.post('/api/admin/tool-approvals/cleanup')
    def cleanup_tool_approvals(request: Request) -> dict[str, int]:
        user = require_admin(request)
        deleted = tool_approvals.cleanup_expired()
        audit.record(user_id(user), 'delete', 'tool_approval', 'expired', 200)
        return {'deleted': deleted}

    @app.post('/api/admin/tool-approvals/{approval_id}/{decision}')
    def decide_tool_approval(approval_id: str, decision: str, request: Request) -> dict:
        user = require_admin(request)
        if decision not in ('approve', 'reject'):
            raise HTTPException(404, '操作不存在')
        if not tool_approvals.decide(approval_id, user_id(user), decision == 'approve'):
            raise HTTPException(409, '申请不存在、已处理或已过期')
        audit.record(user_id(user), decision, 'tool_approval', approval_id, 200)
        return tool_approvals.get(approval_id)

    @app.get('/api/admin/ldap')
    def ldap_config(request: Request) -> dict[str, object]:
        require_admin(request)
        current = {
            'enabled': settings.ldap_enabled, 'server_url': settings.ldap_server_url,
            'user_dn_template': settings.ldap_user_dn_template, 'base_dn': settings.ldap_base_dn,
            'user_filter': settings.ldap_user_filter, 'bind_dn': settings.ldap_bind_dn,
            'group_base_dn': settings.ldap_group_base_dn, 'group_filter': settings.ldap_group_filter,
            'group_attribute': settings.ldap_group_attribute, 'group_role_map': settings.ldap_group_role_map,
            'timeout_seconds': settings.ldap_timeout_seconds,
            'bind_password_configured': bool(settings.ldap_bind_password),
        }
        return current

    @app.put('/api/admin/ldap')
    def update_ldap_config(value: LdapConfigInput, request: Request) -> dict[str, object]:
        require_admin(request)
        data = ldap_config_store.save(value, settings.ldap_bind_password)
        settings.ldap_enabled = value.enabled
        settings.ldap_server_url = value.server_url.strip()
        settings.ldap_user_dn_template = value.user_dn_template.strip()
        settings.ldap_base_dn = value.base_dn.strip()
        settings.ldap_user_filter = value.user_filter.strip() or '(uid={username})'
        settings.ldap_group_base_dn = value.group_base_dn.strip()
        settings.ldap_group_filter = value.group_filter.strip() or '(member={user_dn})'
        settings.ldap_group_attribute = value.group_attribute.strip() or 'cn'
        settings.ldap_group_role_map = dict(value.group_role_map)
        settings.ldap_bind_dn = value.bind_dn.strip()
        settings.ldap_bind_password = str(data.get('bind_password') or '')
        settings.ldap_timeout_seconds = value.timeout_seconds
        ldap.enabled = settings.ldap_enabled
        ldap.server_url = settings.ldap_server_url
        ldap.user_dn_template = settings.ldap_user_dn_template
        ldap.base_dn = settings.ldap_base_dn
        ldap.user_filter = settings.ldap_user_filter
        ldap.group_base_dn = settings.ldap_group_base_dn
        ldap.group_filter = settings.ldap_group_filter
        ldap.group_attribute = settings.ldap_group_attribute
        ldap.bind_dn = settings.ldap_bind_dn
        ldap.bind_password = settings.ldap_bind_password
        ldap.timeout = settings.ldap_timeout_seconds
        return ldap_config(request)

    @app.post('/api/admin/ldap/test')
    def test_ldap_config(value: LdapTestInput, request: Request) -> dict[str, object]:
        require_admin(request)
        if not settings.ldap_enabled:
            raise HTTPException(400, 'LDAP 尚未启用')
        result = ldap.authenticate_with_groups(value.username.strip(), value.password)
        if result is None and ldap.authenticate(value.username.strip(), value.password):
            from app.auth import LdapAuthentication
            result = LdapAuthentication(value.username.strip())
        return {'ok': result is not None, 'username': value.username.strip(), 'groups': list(result.groups) if result else []}

    @app.put('/api/admin/access/roles')
    def save_access_role(value: RoleInput, request: Request) -> dict:
        require_admin(request)
        try:
            return access.save_role(value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put('/api/admin/access/user-role')
    def assign_access_role(value: UserRoleInput, request: Request) -> dict:
        require_admin(request)
        try:
            return access.assign(value)
        except KeyError:
            raise HTTPException(404, '角色不存在') from None

    @app.get('/api/admin/organizations')
    def organization_overview(request: Request) -> dict:
        require_admin(request)
        return organizations.list()

    @app.post('/api/admin/organizations', status_code=201)
    def create_organization(value: OrganizationInput, request: Request) -> dict:
        require_admin(request)
        try:
            return organizations.create(value)
        except sqlite3.IntegrityError:
            raise HTTPException(409, '租户标识已存在') from None

    @app.put('/api/admin/organizations/members')
    def add_organization_member(value: MemberInput, request: Request) -> dict:
        require_admin(request)
        try:
            return organizations.add_member(value)
        except KeyError:
            raise HTTPException(404, '租户不存在') from None

    @app.delete('/api/admin/organizations/{tenant_key}/members/{member_id}', status_code=204)
    def remove_organization_member(tenant_key: str, member_id: str, request: Request) -> None:
        require_admin(request)
        try:
            organizations.remove_member(tenant_key, member_id)
        except KeyError:
            raise HTTPException(404, '组织成员不存在') from None

    @app.get("/api/admin/audit")
    def audit_events(request: Request, page: int = 1, page_size: int = 30, actor: str = "",
                     action: str = "", resource_type: str = "", start: str = "", end: str = "") -> dict:
        require_admin(request)
        return audit.page(max(1, page), min(max(page_size, 1), 100), actor[:200], action[:50],
                          resource_type[:50], start[:40], end[:40])

    @app.post("/api/admin/audit/prune")
    def prune_audit(request: Request, retention_days: int = 180) -> dict[str, int]:
        user = require_admin(request)
        retention_days = min(max(retention_days, 1), 3650)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        deleted = audit.prune(cutoff)
        audit.record(user_id(user), 'delete', 'audit', f'{retention_days}d', 200)
        return {'deleted': deleted, 'retention_days': retention_days}

    @app.get("/api/admin/monitoring")
    def monitoring(request: Request) -> dict:
        require_admin(request)
        task_items = ingest_queue_repository.task_summaries() + delete_queue_repository.task_summaries()
        return {
            **metrics.summary(),
            'queue': {'running': sum(item.status == 'RUNNING' for item in task_items),
                      'failed': sum(item.status == 'FAILED' for item in task_items),
                      'pending': sum(item.status == 'PENDING' for item in task_items)},
            'tool_calls': tool_calls.summary(),
            'agent_runs': agent_runs.summary(),
            'sandbox_resources': sandbox.resource_summary(),
        }

    @app.get("/api/admin/sandbox/policy", response_model=SandboxPolicy)
    def get_sandbox_policy(request: Request) -> SandboxPolicy:
        require_admin(request)
        return sandbox.policy()

    @app.put("/api/admin/sandbox/policy", response_model=SandboxPolicy)
    def update_sandbox_policy(value: SandboxPolicy, request: Request) -> SandboxPolicy:
        require_admin(request)
        sandbox.set_policy(value)
        return value

    @app.get("/api/sandbox/runs")
    def sandbox_runs(request: Request) -> list[dict]:
        user = current_user(request)
        return sandbox.list(None if user.is_admin else user_id(user))

    @app.get("/api/sandbox/runs/{run_id}")
    def sandbox_run(run_id: str, request: Request) -> dict:
        user = current_user(request)
        try:
            return sandbox.get(run_id, None if user.is_admin else user_id(user))
        except KeyError:
            raise HTTPException(404, "执行记录不存在") from None

    @app.get("/api/sandbox/runs/{run_id}/artifacts/{artifact_id}")
    def download_sandbox_artifact(run_id: str, artifact_id: str, request: Request) -> Response:
        user = current_user(request)
        try:
            name, content = sandbox.artifact(run_id, artifact_id, None if user.is_admin else user_id(user))
        except KeyError:
            raise HTTPException(404, "输出文件不存在") from None
        filename = name.rsplit('/', 1)[-1] or 'output'
        return Response(content, media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename, safe=''),
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
        })

    @app.post("/api/sandbox/runs", status_code=201)
    def create_sandbox_run(value: SandboxRequest, request: Request) -> dict:
        user = current_user(request)
        try:
            require_any_permission(request, 'code.execute', 'sandbox.use')
            sandbox.authorize(value, user.is_admin)
            documents = []
            total = 0
            if value.source_ids:
                if not value.agent_id:
                    raise HTTPException(400, '请选择知识库')
                source_repository = authorize_agent(request, value.agent_id).repository
                for source_id in dict.fromkeys(value.source_ids):
                    try:
                        source = source_repository.get(source_id)
                    except KeyError:
                        raise HTTPException(404, '文档不存在') from None
                    source_dir = Path(source.source_path).resolve().parent
                    path = (source_dir / 'document.md').resolve()
                    if source_dir not in path.parents or not path.is_file():
                        raise HTTPException(404, f'文档尚无可用的解析文本：{source.filename}')
                    with path.open('rb') as file:
                        content = file.read(2 * 1024 * 1024 - total + 1)
                    total += len(content)
                    if total > 2 * 1024 * 1024:
                        raise HTTPException(413, '所选文档的解析文本总量不能超过 2 MB')
                    documents.append({'source_id': source.source_id, 'filename': source.filename,
                                      'markdown': content.decode('utf-8', errors='replace')})
            return sandbox.run(user_id(user), value, is_admin=user.is_admin, documents=documents)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(429, str(exc)) from None

    def user_id(user: AuthenticatedUser) -> str:
        return f"{user.auth_source}:{user.username}"

    def identity_can_use_kb(identity: str, knowledge_base_id: str) -> bool:
        try:
            row = registry.store.get_knowledge_base(knowledge_base_id)
        except KeyError:
            return False
        return str(row.get("owner_user_id") or "") == identity or organizations.is_member(str(row.get("tenant_key") or ""), identity)

    def can_use_kb(request: Request, knowledge_base_id: str) -> bool:
        user = current_user(request)
        if user.is_admin or user.auth_source == "api_key":
            return True
        return identity_can_use_kb(user_id(user), knowledge_base_id)

    def can_write_kb(request: Request, knowledge_base_id: str) -> bool:
        user = current_user(request)
        if user.is_admin:
            return True
        if not can_use_kb(request, knowledge_base_id):
            return False
        try:
            row = registry.store.get_knowledge_base(knowledge_base_id)
        except KeyError:
            return False
        if str(row.get("owner_user_id") or "") == user_id(user):
            return True
        return "document.write" in access.permissions(user_id(user), user.is_admin)

    def require_kb_write(request: Request, knowledge_base_id: str) -> AuthenticatedUser:
        if can_write_kb(request, knowledge_base_id):
            return current_user(request)
        raise HTTPException(403, "缺少权限：document.write")

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

    from app.document_api import document_router
    def authorize_document_read(request: Request, agent_id: str) -> AgentRuntime:
        value = authorize_agent(request, agent_id)
        user = current_user(request)
        if not user.is_admin and user.auth_source != 'api_key' and 'document.read' not in access.permissions(user_id(user), user.is_admin):
            raise HTTPException(403, '缺少权限：document.read')
        return value
    app.include_router(document_router(authorize_document_read, require_kb_write))
    from app.search.api import search_router
    app.include_router(search_router(authorize_document_read, require_kb_write))

    @app.get("/api/agents", response_model=list[AgentSummary])
    def list_agents(request: Request) -> list[AgentSummary]:
        user = current_user(request)
        values = registry.list()
        if user.is_admin or user.auth_source == "api_key":
            return values
        return [item for item in values if can_use_kb(request, registry.get(item.agent_id).profile.knowledge_base_id)]

    @app.get("/api/admin/knowledge-bases", response_model=list[KnowledgeBaseView])
    def list_knowledge_bases(request: Request) -> list[KnowledgeBaseView]:
        user = current_user(request)
        values = registry.list_knowledge_bases()
        return values if user.is_admin else [item for item in values if can_use_kb(request, item.knowledge_base_id)]

    @app.get("/api/knowledge-bases")
    def list_available_knowledge_bases(request: Request) -> list[dict[str, object]]:
        user = current_user(request)
        values = registry.list_knowledge_bases()
        if not user.is_admin:
            values = [item for item in values if can_use_kb(request, item.knowledge_base_id)]
        return [{"knowledge_base_id": item.knowledge_base_id, "name": item.name,
                 "runtime_id": f"@kb:{item.knowledge_base_id}",
                 "can_write_documents": can_write_kb(request, item.knowledge_base_id)} for item in values if item.enabled]

    @app.get('/api/knowledge-bases/statistics')
    def knowledge_base_statistics(request: Request) -> list[dict]:
        current_user(request)
        result = []
        for item in registry.list_knowledge_bases():
            if not can_use_kb(request, item.knowledge_base_id):
                continue
            stats = None
            if item.enabled:
                try:
                    stats = registry.get(f'@kb:{item.knowledge_base_id}').repository.stats()
                except KeyError:
                    pass
            result.append({'knowledge_base_id': item.knowledge_base_id, 'documents': stats, 'chunk_count': None})
        return result

    @app.get("/api/platform/config")
    def public_platform_config() -> dict[str, str]:
        return {"dify_base_url": settings.dify_base_url}

    @app.post("/api/knowledge-bases/{knowledge_base_id}/retrieval-test")
    def test_retrieval(knowledge_base_id: str, value: RetrievalTestRequest, request: Request) -> dict:
        require_permission(request, 'knowledge.read')
        runtime_value = authorize_agent(request, f"@kb:{knowledge_base_id}")
        query = value.query.strip()
        if not query:
            raise HTTPException(422, '查询内容不能为空')
        started = time.monotonic()
        try:
            hits = runtime_value.dify.retrieve(query, value.top_k, value.metadata_filtering_conditions,
                                               search_method=value.search_method)
        except Exception:
            logger.exception('Dify retrieval test failed')
            raise HTTPException(502, 'Dify 检索失败，请检查知识库配置和所选检索模式') from None
        results = []
        unmatched = 0
        for hit in hits:
            source = runtime_value.repository.by_dify_document(hit.document_id) if hit.document_id else None
            if source is None:
                unmatched += 1
                continue
            results.append({'source_id': source.source_id, 'filename': source.filename, 'segment_id': hit.segment_id,
                            'content': hit.content, 'score': hit.score, 'page': None, 'location': None})
        return {'items': results, 'unmatched_count': unmatched, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                'search_method': value.search_method}

    @app.get("/api/knowledge-bases/{knowledge_base_id}/tools")
    def list_knowledge_tools(knowledge_base_id: str, request: Request) -> list[dict[str, object]]:
        runtime_value = authorize_agent(request, f"@kb:{knowledge_base_id}")
        stats = tool_calls.stats(knowledge_base_id)
        configs = {str(item['tool_name']): item for item in tool_configs.list()}
        return [{**item, 'statistics': stats.get(str(item['name']), {'calls': 0, 'succeeded': 0, 'last_called_at': None})} for item in describe_tools(runtime_value.agent.allowed_tools, configs) if bool(configs.get(str(item['name']), {}).get('enabled', True))]

    @app.get("/api/knowledge-bases/{knowledge_base_id}/tools/{tool_name}/calls")
    def recent_tool_calls(knowledge_base_id: str, tool_name: str, request: Request) -> list[dict]:
        runtime_value = authorize_agent(request, f"@kb:{knowledge_base_id}")
        if tool_name not in TOOL_DESCRIPTIONS or tool_name not in runtime_value.agent.allowed_tools:
            raise HTTPException(404, "工具不存在或未授权")
        return tool_calls.recent(knowledge_base_id, tool_name)

    def validate_read_tool(runtime_value: AgentRuntime, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        if tool_name not in TOOL_DESCRIPTIONS or tool_name not in runtime_value.agent.allowed_tools:
            raise HTTPException(404, "工具不存在或未授权")
        if not bool(tool_configs.get(tool_name).get('enabled', True)):
            raise HTTPException(403, "工具已被管理员禁用")
        try:
            values = input_model(tool_name).model_validate(arguments).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, detail=exc.errors(include_url=False)) from None
        if values.get("regex"):
            raise HTTPException(400, "工具测试不支持正则表达式")
        for field, maximum in (("limit", 100), ("top_k", 50), ("max_chars", 30000), ("max_items", 100)):
            if field in values:
                values[field] = min(max(int(values[field]), 1), maximum)
        return values

    def validate_tool(runtime_value: AgentRuntime, tool_name: str, arguments: dict[str, object], request: Request | None = None) -> dict[str, object]:
        values = validate_read_tool(runtime_value, tool_name, arguments)
        if tool_name in WRITE_TOOLS and request is not None:
            require_kb_write(request, runtime_value.profile.knowledge_base_id)
        return values

    def create_approval(runtime_value: AgentRuntime, kb: str, tool_name: str, arguments: dict, actor: str) -> dict:
        values = validate_tool(runtime_value, tool_name, arguments)
        if tool_name not in WRITE_TOOLS and not tool_configs.get(tool_name)['requires_approval']:
            raise HTTPException(409, '此工具当前不需要审批')
        try:
            result = tool_approvals.create(actor, kb, tool_name, values)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit.record(actor, 'create', 'tool_approval', result['id'], 201)
        return result

    def execute_read_tool(runtime_value: AgentRuntime, tool_name: str, arguments: dict[str, object], actor: str, kb: str, approval_id: str | None, request: Request | None = None) -> dict[str, object]:
        values = validate_tool(runtime_value, tool_name, arguments, request)
        needs_approval = tool_name in WRITE_TOOLS or tool_configs.get(tool_name)['requires_approval']
        if needs_approval and not (approval_id and tool_approvals.consume(approval_id, actor, kb, tool_name, values)):
            raise HTTPException(403, '此调用需要有效的人工审批')
        try:
            if tool_name == 'delete_document':
                runtime_value.ingestion.delete_source(values['source_id'])
                return {'status': 'deleted', 'source_id': values['source_id'], 'count': 1}
            if tool_name == 'upload_document':
                filename = Path(values['filename']).name
                if Path(filename).suffix.casefold() not in DOCUMENT_TYPES:
                    raise HTTPException(400, '不支持的文档格式')
                try:
                    content = base64.b64decode(values['content_base64'], validate=True)
                except (ValueError, TypeError):
                    raise HTTPException(422, 'content_base64 不是有效的 Base64') from None
                if not content or len(content) > 25 * 1024 * 1024:
                    raise HTTPException(413, '单个工具上传文件最大 25 MB')
                job = ingest_queue_repository.create(f'@kb:{kb}', BatchIngestCreate(filenames=[filename], file_sizes=[len(content)]), actor)
                item = ingest_queue_repository.item_for_position(job.job_id, 0)
                target_dir = settings.ingest_staging_dir / job.job_id / item['item_id']
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / filename
                target.write_bytes(content)
                ingest_queue_repository.mark_uploaded(item['item_id'], target, target)
                ingest_queue_repository.start(job.job_id)
                return {'status': 'ingest_queued', 'job_id': job.job_id, 'filename': filename, 'size': len(content), 'count': 1}
            if tool_name == 'parse_document':
                source = runtime_value.repository.get(values['source_id'])
                if delete_queue_repository.active_for_knowledge_base(kb):
                    raise HTTPException(409, '知识库正在删除文档，请稍后重试')
                job = ingest_queue_repository.create_reparse(f'@kb:{kb}', [source], actor)
                return {'status': 'reparse_queued', 'source_id': source.source_id, 'job_id': job.job_id, 'count': 1}
            if tool_name == 'rebuild_document_index':
                source = runtime_value.repository.get(values['source_id'])
                job = ingest_queue_repository.create_reparse(f'@kb:{kb}', [source], actor, 'document_reindex')
                return {'status': 'reindex_queued', 'source_id': source.source_id, 'job_id': job.job_id, 'count': 1}
            if tool_name == 'export_csv':
                output = io.StringIO(newline='')
                writer = csv.writer(output)
                writer.writerow(values['columns'])
                for row in values['rows']:
                    if len(row) != len(values['columns']):
                        raise HTTPException(422, '每一行的数据列数必须与 columns 一致')
                    writer.writerow(row)
                content = output.getvalue()
                result = {'filename': 'export.csv', 'content_type': 'text/csv; charset=utf-8',
                          'content': content, 'count': len(values['rows'])}
            elif tool_name == 'export_excel':
                def cell(value: object, ref: str) -> str:
                    text = '' if value is None else str(value)
                    escaped = (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                               .replace('"', '&quot;').replace("'", '&apos;'))
                    return f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>'
                def column_name(index: int) -> str:
                    value = index + 1
                    name = ''
                    while value:
                        value, remainder = divmod(value - 1, 26)
                        name = chr(65 + remainder) + name
                    return name
                rows_xml = []
                all_rows = [values['columns'], *values['rows']]
                for row_index, row in enumerate(all_rows, 1):
                    cells = ''.join(cell(value, f'{column_name(column_index)}{row_index}') for column_index, value in enumerate(row))
                    rows_xml.append(f'<row r="{row_index}">{cells}</row>')
                sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                         + ''.join(rows_xml) + '</sheetData></worksheet>')
                files = {
                    '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
                    '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
                    'xl/workbook.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
                    'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
                    'xl/worksheets/sheet1.xml': sheet,
                }
                binary = io.BytesIO()
                with zipfile.ZipFile(binary, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for filename, content in files.items():
                        archive.writestr(filename, content)
                result = {'filename': Path(values['filename']).name or 'export.xlsx',
                          'content_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          'content_base64': base64.b64encode(binary.getvalue()).decode('ascii'),
                          'count': len(values['rows'])}
            elif tool_name == 'analyze_data':
                columns = values['columns']
                summaries = []
                for index, name in enumerate(columns):
                    values_for_column = [row[index] for row in values['rows'] if len(row) > index and row[index] not in (None, '')]
                    numeric = []
                    for item in values_for_column:
                        if isinstance(item, bool):
                            continue
                        if isinstance(item, (int, float)):
                            numeric.append(float(item))
                        elif isinstance(item, str):
                            try:
                                numeric.append(float(item.strip()))
                            except ValueError:
                                pass
                    summary = {'column': name, 'non_empty': len(values_for_column), 'numeric_count': len(numeric)}
                    if numeric:
                        summary.update({'min': min(numeric), 'max': max(numeric), 'average': sum(numeric) / len(numeric)})
                    summaries.append(summary)
                result = {'row_count': len(values['rows']), 'column_count': len(columns), 'columns': summaries}
            else:
                result = getattr(runtime_value.tools, tool_name)(**values)
        except KeyError:
            raise HTTPException(404, "文档不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        items = result if isinstance(result, list) else [result]
        safe_fields = {"source_id", "filename", "standard_no", "standard_title", "standard_year", "publish_date", "effective_date", "status", "page", "line_start", "line_end", "location", "content", "retrieval_type", "score", "segment_id", "pdf_url"}
        safe_fields.update({'parse_status', 'dify_status', 'file_size', 'page_count', 'filename', 'content_type', 'content_base64', 'count', 'row_count', 'column_count', 'columns', 'non_empty', 'numeric_count', 'min', 'max', 'average', 'column'})
        return {"items": [{key: value for key, value in (item if isinstance(item, dict) else item.model_dump()).items() if key in safe_fields} for item in items], "count": len(items)}

    @app.post('/api/knowledge-bases/{knowledge_base_id}/tools/{tool_name}/approvals', status_code=201)
    def request_tool_approval(knowledge_base_id: str, tool_name: str, arguments: dict[str, object], request: Request) -> dict:
        user = require_permission(request, 'tool.invoke')
        runtime_value = authorize_agent(request, f'@kb:{knowledge_base_id}')
        if tool_name in WRITE_TOOLS:
            require_kb_write(request, knowledge_base_id)
        return create_approval(runtime_value, knowledge_base_id, tool_name, arguments, user_id(user))

    @app.get('/api/tool-approvals')
    def my_tool_approvals(request: Request) -> list[dict]:
        return tool_approvals.list(user_id(current_user(request)))

    @app.post("/api/knowledge-bases/{knowledge_base_id}/tools/{tool_name}/test")
    def test_knowledge_tool(knowledge_base_id: str, tool_name: str, arguments: dict[str, object], request: Request) -> dict[str, object]:
        require_permission(request, 'tool.invoke')
        runtime_value = authorize_agent(request, f"@kb:{knowledge_base_id}")
        if tool_name in WRITE_TOOLS:
            require_kb_write(request, knowledge_base_id)
        started = time.monotonic()
        status = 'failed'
        try:
            result = execute_read_tool(runtime_value, tool_name, arguments, user_id(current_user(request)), knowledge_base_id, request.headers.get('X-Approval-Id'), request)
            status = 'success'
            return result
        finally:
            if tool_name in TOOL_DESCRIPTIONS and tool_name in runtime_value.agent.allowed_tools:
                try:
                    tool_calls.record(knowledge_base_id, tool_name, user_id(current_user(request)), 'test', status, int((time.monotonic() - started) * 1000))
                except Exception:
                    logger.exception('Unable to persist tool call')

    def integration_for_user(request: Request, integration_id: str) -> dict:
        try:
            value = integrations.get(integration_id)
        except KeyError:
            raise HTTPException(404, "接入不存在") from None
        user = current_user(request)
        if not user.is_admin and value["owner"] != user_id(user):
            raise HTTPException(403, "无权管理该接入")
        return value

    @app.get("/api/integrations")
    def list_integrations(request: Request) -> list[dict]:
        user = current_user(request)
        return integrations.list(None if user.is_admin else user_id(user))

    @app.post("/api/integrations", status_code=201)
    def create_integration(value: IntegrationCreate, request: Request) -> dict:
        user = current_user(request)
        if not value.name.strip():
            raise HTTPException(400, "名称不能为空")
        for kb_id in value.knowledge_base_ids:
            runtime_value = authorize_agent(request, f"@kb:{kb_id}")
            if not set(value.tool_names) <= (set(TOOL_DESCRIPTIONS) & runtime_value.agent.allowed_tools):
                raise HTTPException(400, "包含未授权或不可用的工具")
            if set(value.tool_names) & WRITE_TOOLS:
                require_kb_write(request, kb_id)
        record, token = integrations.create(value, user_id(user), user.is_admin)
        return {"integration": record, "token": token}

    @app.put("/api/integrations/{integration_id}/permissions")
    def update_integration_permissions(integration_id: str, value: IntegrationPermissions, request: Request) -> dict:
        integration = integration_for_user(request, integration_id)
        if integration["revoked_at"]:
            raise HTTPException(409, "接入已撤销")
        for kb_id in value.knowledge_base_ids:
            runtime_value = authorize_agent(request, f"@kb:{kb_id}")
            if not integration["owner_is_admin"] and not identity_can_use_kb(integration["owner"], kb_id):
                raise HTTPException(403, "接入所有者无权访问该知识库")
            if not set(value.tool_names) <= (set(TOOL_DESCRIPTIONS) & runtime_value.agent.allowed_tools):
                raise HTTPException(400, "包含未授权或不可用的工具")
        try:
            return integrations.update_permissions(integration_id, value)
        except KeyError:
            raise HTTPException(409, "接入已撤销") from None

    @app.post("/api/integrations/{integration_id}/rotate")
    def rotate_integration(integration_id: str, request: Request) -> dict:
        integration_for_user(request, integration_id)
        try:
            return {"token": integrations.rotate(integration_id)}
        except KeyError:
            raise HTTPException(409, "接入已撤销") from None

    @app.post("/api/integrations/{integration_id}/revoke", status_code=204)
    def revoke_integration(integration_id: str, request: Request) -> None:
        integration_for_user(request, integration_id)
        try:
            integrations.revoke(integration_id)
        except KeyError:
            raise HTTPException(409, "接入已撤销") from None

    @app.get("/api/integrations/{integration_id}/calls")
    def integration_calls(integration_id: str, request: Request) -> list[dict]:
        integration_for_user(request, integration_id)
        return integrations.calls(integration_id)

    def scoped_integration(request: Request) -> dict:
        scheme, separator, token = request.headers.get("authorization", "").partition(" ")
        value = integrations.authenticate(token.strip()) if separator and scheme.casefold() == "bearer" else None
        if value is None:
            raise HTTPException(401, "接入密钥无效或已撤销", headers={"WWW-Authenticate": "Bearer"})
        return value

    @app.get("/api/external/v1/tools")
    def external_tools(request: Request) -> list[dict[str, object]]:
        value = scoped_integration(request)
        configs = {str(item['tool_name']): item for item in tool_configs.list()}
        return [item for item in describe_tools(set(value["tool_names"]), configs) if bool(configs.get(str(item['name']), {}).get('enabled', True))]

    def external_runtime(value: dict, kb: str, tool_name: str) -> AgentRuntime:
        if kb not in value['knowledge_base_ids'] or tool_name not in value['tool_names']:
            raise HTTPException(403, '该接入未获知识库或工具授权')
        runtime_value = runtime(f'@kb:{kb}')
        if not value['owner_is_admin'] and not identity_can_use_kb(value['owner'], kb):
            raise HTTPException(403, '接入所有者不再拥有该知识库')
        if tool_name in WRITE_TOOLS and not value['owner_is_admin']:
            row = registry.store.get_knowledge_base(kb)
            if row.get('owner_user_id') != value['owner'] and 'document.write' not in access.permissions(value['owner'], False):
                raise HTTPException(403, '接入所有者没有文档写权限')
        return runtime_value

    @app.post('/api/external/v1/tools/{tool_name}/approvals', status_code=201)
    def external_request_approval(tool_name: str, call: ExternalToolCall, request: Request) -> dict:
        value = scoped_integration(request)
        runtime_value = external_runtime(value, call.knowledge_base_id, tool_name)
        return create_approval(runtime_value, call.knowledge_base_id, tool_name, call.arguments, f"integration:{value['id']}")

    @app.get('/api/external/v1/approvals/{approval_id}')
    def external_approval_status(approval_id: str, request: Request) -> dict:
        value = scoped_integration(request)
        try:
            approval = tool_approvals.get(approval_id)
        except KeyError:
            raise HTTPException(404, '申请不存在') from None
        if approval['actor'] != f"integration:{value['id']}":
            raise HTTPException(404, '申请不存在')
        external_runtime(value, approval['knowledge_base_id'], approval['tool_name'])
        return approval

    @app.post("/api/external/v1/tools/{tool_name}")
    def external_tool(tool_name: str, call: ExternalToolCall, request: Request) -> dict[str, object]:
        value = scoped_integration(request)
        started = time.monotonic()
        status = "error"
        audit_status = 500
        authorized = False
        try:
            try:
                runtime_value = external_runtime(value, call.knowledge_base_id, tool_name)
            except HTTPException:
                status = "forbidden"
                raise
            authorized = True
            result = execute_read_tool(runtime_value, tool_name, call.arguments, f"integration:{value['id']}", call.knowledge_base_id, call.approval_id)
            status = "success"
            audit_status = 200
            return result
        except HTTPException as exc:
            audit_status = exc.status_code
            raise
        finally:
            integrations.record_call(value["id"], call.knowledge_base_id, tool_name, status, int((time.monotonic() - started) * 1000))
            if authorized and tool_name in TOOL_DESCRIPTIONS:
                try:
                    tool_calls.record(call.knowledge_base_id, tool_name, f"integration:{value['id']}", 'external', status, int((time.monotonic() - started) * 1000))
                except Exception:
                    logger.exception('Unable to persist tool call')
            try:
                audit.record(f"integration:{value['id']}", "invoke", "tool", tool_name if tool_name in TOOL_DESCRIPTIONS else "", audit_status)
            except Exception:
                logger.exception("Unable to persist integration audit event")

    if settings.mcp_enabled:
        try:
            from app.mcp_server import create_mcp_manager
        except ImportError:
            logger.warning("MCP SDK is not installed; MCP transport is unavailable")
        else:
            mcp_manager = create_mcp_manager(scoped_integration, external_tool, settings.mcp_allowed_hosts, settings.mcp_allowed_origins, external_tools)

            async def mcp_endpoint(scope, receive, send):
                request = Request(scope, receive)
                try:
                    scoped_integration(request)
                except HTTPException as exc:
                    await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, send)
                    return
                await mcp_manager.handle_request(scope, receive, send)

            app.mount("/api/mcp", mcp_endpoint)

    if mcp_manager is None:
        @app.api_route("/api/mcp/", methods=["GET", "POST", "DELETE"])
        def unavailable_mcp(request: Request) -> None:
            scoped_integration(request)
            raise HTTPException(503, "MCP 未启用或未安装 MCP 组件")

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
                "tenant_key": "",
                "base_url": settings.dify_base_url or value.base_url,
                "pdf_parser": "default", "qwenvl_base_url": "",
                "qwenvl_api_key": None, "source_platform_mode": "inherit",
                "source_platform_base_url": "", "source_platform_api_key": None,
                "source_platform_command": [], "source_platform_url_template": "",
                "workflow_enabled": False, "workflow_base_url": "", "workflow_api_key": None,
                "use_legacy_data": False,
            })
        try:
            return registry.create_knowledge_base(value, owner_username=user.username, owner_user_id=user_id(user))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/admin/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseView)
    def update_knowledge_base(knowledge_base_id: str, value: KnowledgeBaseInput, request: Request) -> KnowledgeBaseView:
        if value.knowledge_base_id != knowledge_base_id:
            raise HTTPException(400, "Knowledge base id 不可修改")
        if not can_use_kb(request, knowledge_base_id):
            raise HTTPException(403, "无权修改该知识库")
        try:
            existing = registry.store.get_knowledge_base(knowledge_base_id)
        except KeyError:
            raise HTTPException(404, "知识库不存在") from None
        user = current_user(request)
        if not user.is_admin:
            if str(existing.get("owner_user_id") or "") != user_id(user):
                raise HTTPException(403, "只有所有者或管理员可以修改知识库配置")
            value = value.model_copy(update={
                "tenant_key": str(existing.get("tenant_key") or ""),
                "base_url": str(existing["base_url"]),
                "pdf_parser": str(existing["pdf_parser"]), "qwenvl_base_url": str(existing["qwenvl_base_url"]), "qwenvl_api_key": None,
                "source_platform_mode": str(existing["source_platform_mode"]),
                "source_platform_base_url": str(existing["source_platform_base_url"]), "source_platform_api_key": None,
                "source_platform_command": json.loads(str(existing["source_platform_command"])),
                "source_platform_url_template": str(existing["source_platform_url_template"]),
                "workflow_enabled": bool(existing["workflow_enabled"]),
                "workflow_base_url": str(existing["workflow_base_url"]), "workflow_api_key": None,
                "use_legacy_data": bool(existing["use_legacy_data"]),
            })
        try:
            return registry.save_knowledge_base(value, owner_username=str(existing["owner_username"]), owner_user_id=str(existing["owner_user_id"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/admin/knowledge-bases/{knowledge_base_id}", status_code=204)
    def delete_knowledge_base(knowledge_base_id: str, request: Request) -> None:
        if not can_use_kb(request, knowledge_base_id):
            raise HTTPException(403, "无权删除该知识库")
        user = current_user(request)
        if not user.is_admin:
            try:
                existing = registry.store.get_knowledge_base(knowledge_base_id)
            except KeyError:
                raise HTTPException(404, "知识库不存在") from None
            if str(existing.get("owner_user_id") or "") != user_id(user):
                raise HTTPException(403, "只有所有者或管理员可以删除知识库")
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
        return [item for item in values if can_use_kb(request, item.knowledge_base_id)]

    @app.post("/api/admin/agents", response_model=AgentConfigView)
    def create_agent(value: AgentConfigInput, request: Request) -> AgentConfigView:
        require_admin(request)
        if not can_use_kb(request, value.knowledge_base_id):
            raise HTTPException(403, "只能为自己的知识库创建 Agent")
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
        require_admin(request)
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

    async def execute_agent(agent_runtime: AgentRuntime, value: AskRequest, http_request: Request, operation: str, approved_approval_id: str | None = None, integration: dict | None = None) -> AskResponse | RetrievalResponse:
        owner = user_id(current_user(http_request))
        kb_id = agent_runtime.profile.knowledge_base_id
        conversation_key = json.dumps([owner, kb_id, value.conversation_id], ensure_ascii=True, separators=(",", ":"))
        run_id = agent_runs.start(owner, kb_id, agent_runtime.profile.agent_id, agent_runtime.profile.name, operation, value.question.strip(), value.conversation_id, value.history)
        started = time.monotonic()
        user = current_user(http_request)
        def execute_code(code: str) -> str:
            policy = sandbox.policy()
            if not policy.enabled or not policy.allow_python:
                raise PermissionError('Sandbox 策略不允许 Python')
            if not access.permissions(owner, user.is_admin).intersection({'code.execute', 'sandbox.use'}):
                raise PermissionError('当前用户没有 Sandbox 权限')
            result = sandbox.run(owner, SandboxRequest(name='Agent 自动分析', language='python', code=code), is_admin=user.is_admin)
            return f"状态={result['status']}\n标准输出：{result['stdout']}\n错误输出：{result['stderr']}"

        policy = sandbox.policy()
        may_execute = policy.enabled and policy.allow_python and (user.is_admin or policy.allow_users) and bool(
            access.permissions(owner, user.is_admin).intersection({'code.execute', 'sandbox.use'})
        ) and integration is None
        def run_agent():
            def is_enabled(name: str) -> bool:
                if integration is not None:
                    current = integrations.authenticate(integration['_token'])
                    if current is None or kb_id not in current['knowledge_base_ids'] or name not in current['tool_names']:
                        return False
                    if not current['owner_is_admin'] and not identity_can_use_kb(current['owner'], kb_id):
                        return False
                    if name in WRITE_TOOLS and not current['owner_is_admin']:
                        row = registry.store.get_knowledge_base(kb_id)
                        if row.get('owner_user_id') != current['owner'] and 'document.write' not in access.permissions(current['owner'], False):
                            return False
                return (integration is None if name not in TOOL_DESCRIPTIONS else bool(tool_configs.get(name)['enabled']))
            def record_call(name: str, status: str, duration_ms: int) -> None:
                tool_calls.record(kb_id, name, owner, 'agent', status, duration_ms)
                audit.record(owner, 'invoke', 'tool', name, 200 if status == 'success' else 500)
                if integration is not None:
                    integrations.record_call(integration['id'], kb_id, name, status, duration_ms)
            def request_approval(name: str, arguments: dict[str, object]) -> str:
                approval = tool_approvals.create(owner, kb_id, name, arguments)
                audit.record(owner, 'create', 'tool_approval', approval['id'], 201)
                return approval['id']
            def consume_approval(name: str, arguments: dict[str, object]) -> bool:
                return bool(approved_approval_id and tool_approvals.consume(approved_approval_id, owner, kb_id, name, arguments))
            context = {
                'registered_tools': TOOL_DESCRIPTIONS,
                'is_enabled': is_enabled,
                'requires_approval': lambda name: name in WRITE_TOOLS or bool(tool_configs.get(name)['requires_approval']),
                'request_approval': request_approval,
                'consume_approval': consume_approval,
                'record': record_call,
                'code_executor': execute_code if may_execute else None,
            }
            token = agent_execution_context.set(context)
            try:
                method = agent_runtime.agent.ask if operation == 'ask' else agent_runtime.agent.retrieve
                return method(value.question.strip(), _source_base_url(http_request, settings), conversation_key, value.history)
            finally:
                agent_execution_context.reset(token)
        try:
            async with ask_concurrency.acquire(conversation_key):
                result = await asyncio.get_running_loop().run_in_executor(ask_concurrency.executor, run_agent)
            agent_runs.finish(run_id, "succeeded", int((time.monotonic() - started) * 1000), result)
            return result.model_copy(update={"run_id": run_id})
        except (TimeoutError, asyncio.TimeoutError):
            agent_runs.finish(run_id, "rejected", int((time.monotonic() - started) * 1000))
            raise HTTPException(429, "问答请求较多，请稍后重试", headers={"Retry-After": "3"}) from None
        except ToolApprovalRequired as exc:
            agent_runs.waiting(run_id, exc.approval_id, exc.tool_name)
            raise HTTPException(409, {"message": "Agent 等待人工审批", "run_id": run_id, "approval_id": exc.approval_id, "tool_name": exc.tool_name}) from None
        except asyncio.CancelledError:
            agent_runs.finish(run_id, "request_cancelled", int((time.monotonic() - started) * 1000))
            raise
        except Exception:
            agent_runs.finish(run_id, "failed", int((time.monotonic() - started) * 1000))
            raise

    @app.get("/api/agent-runs")
    def list_agent_runs(request: Request, limit: int = 50, offset: int = 0) -> list[dict]:
        user = current_user(request)
        rows = agent_runs.list(None if user.is_admin else user_id(user), min(max(limit, 1), 100), max(offset, 0))
        return [row for row in rows if can_use_kb(request, row["knowledge_base_id"])]

    @app.get("/api/agent-runs/page")
    def page_agent_runs(request: Request, page: int = 1, page_size: int = 30,
                        status: str = "", operation: str = "", knowledge_base_id: str = "") -> dict:
        user = current_user(request)
        if knowledge_base_id and not can_use_kb(request, knowledge_base_id):
            raise HTTPException(403, "无权访问该知识库")
        allowed = None if user.is_admin or user.auth_source == "api_key" else [
            item.knowledge_base_id for item in registry.list_knowledge_bases()
            if can_use_kb(request, item.knowledge_base_id)
        ]
        if knowledge_base_id:
            allowed = [knowledge_base_id]
        page, page_size = max(page, 1), min(max(page_size, 1), 100)
        rows, total = agent_runs.page(None if user.is_admin else user_id(user), allowed,
                                      status, operation, page_size, (page - 1) * page_size)
        return {"items": rows, "page": page, "page_size": page_size, "total": total}

    @app.get("/api/dashboard")
    def dashboard(request: Request) -> dict:
        user = current_user(request)
        bases = [item for item in registry.list_knowledge_bases()
                 if item.enabled and can_use_kb(request, item.knowledge_base_id)]
        ids = [item.knowledge_base_id for item in bases]
        stats = {key: 0 for key in ("total", "parsed", "indexed", "failed", "processing")}
        documents = []
        available_tools: set[str] = set()
        configs = {str(item['tool_name']): item for item in tool_configs.list()}
        for base in bases:
            runtime_value = runtime(f"@kb:{base.knowledge_base_id}")
            for key, value in runtime_value.repository.stats().items():
                stats[key] += value
            sources, _ = runtime_value.repository.list_page(1, 6)
            documents.extend({'source_id': item.source_id, 'filename': item.filename,
                              'knowledge_base': base.name, 'parse_status': item.parse_status,
                              'dify_status': item.dify_status, 'created_at': item.created_at} for item in sources)
            available_tools.update(name for name in runtime_value.agent.allowed_tools
                                   if name in TOOL_DESCRIPTIONS and configs.get(name, {}).get('enabled', True))
        documents.sort(key=lambda item: item['created_at'], reverse=True)
        runs, run_total = agent_runs.page(None if user.is_admin else user_id(user), ids, '', '', 6, 0)
        clients = integrations.list(None if user.is_admin else user_id(user))
        tasks = list_tasks(request, page=1, page_size=6, status='', task_type='', agent_id='')
        return {'knowledge_bases': len(bases), 'documents': stats, 'tools': len(available_tools),
                'active_integrations': sum(not item['revoked_at'] for item in clients),
                'agent_run_total': run_total, 'recent_documents': documents[:6],
                'recent_runs': runs, 'recent_tasks': tasks.items,
                'recent_tool_calls': tool_calls.recent_for_knowledge_bases(ids)}

    @app.get("/api/agent-runs/{run_id}")
    def get_agent_run(run_id: str, request: Request) -> dict:
        try:
            row = agent_runs.get(run_id)
        except KeyError:
            raise HTTPException(404, "执行记录不存在") from None
        user = current_user(request)
        if (not user.is_admin and row["owner_id"] != user_id(user)) or not can_use_kb(request, row["knowledge_base_id"]):
            raise HTTPException(403, "无权访问该执行记录")
        return row

    @app.post("/api/agent-runs/{run_id}/resume")
    async def resume_agent_run(run_id: str, request: Request) -> AskResponse | RetrievalResponse:
        try:
            row = agent_runs.get(run_id)
        except KeyError:
            raise HTTPException(404, "执行记录不存在") from None
        user = current_user(request)
        if (not user.is_admin and row['owner_id'] != user_id(user)) or not can_use_kb(request, row['knowledge_base_id']):
            raise HTTPException(403, "无权恢复该执行记录")
        if row.get('status') != 'waiting_approval' or not row.get('approval_id'):
            raise HTTPException(409, "该执行记录不在等待审批状态")
        try:
            approval = tool_approvals.get(row['approval_id'])
        except KeyError:
            raise HTTPException(404, "审批申请不存在") from None
        if approval['actor'] != row['owner_id'] or approval['status'] != 'approved':
            raise HTTPException(409, "审批尚未批准或已失效")
        resume_data = agent_runs.resume_data(run_id)
        try:
            history = [AskHistoryMessage.model_validate(item) for item in json.loads(resume_data.get('history') or '[]')]
        except (TypeError, json.JSONDecodeError, ValueError):
            history = []
        value = AskRequest(question=resume_data['question'], conversation_id=resume_data['conversation_id'] or 'default', history=history)
        runtime_value = authorize_agent(request, row['agent_id'])
        result = await execute_agent(runtime_value, value, request, row['operation'], row['approval_id'])
        agent_runs.resumed(run_id)
        return result

    @app.post("/api/agents/{agent_id}/ask", response_model=AskResponse)
    async def ask_agent(agent_id: str, request: AskRequest, http_request: Request) -> AskResponse:
        return await execute_agent(authorize_agent(http_request, agent_id), request, http_request, "ask")

    @app.post("/api/agents/{agent_id}/retrieve", response_model=RetrievalResponse)
    async def retrieve_agent(agent_id: str, request: AskRequest, http_request: Request) -> RetrievalResponse:
        return await execute_agent(authorize_agent(http_request, agent_id), request, http_request, "retrieve")

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

    @app.get("/api/agents/{agent_id}/sources/{source_id}/versions", response_model=list[SourceVersion])
    def source_versions(agent_id: str, source_id: str, request: Request) -> list[SourceVersion]:
        runtime_value = authorize_agent(request, agent_id)
        try:
            runtime_value.repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        return [SourceVersion(**item) for item in runtime_value.repository.versions(source_id)]

    @app.get("/api/agents/{agent_id}/sources/{source_id}/versions/{version_no}")
    def source_version_content(agent_id: str, source_id: str, version_no: int, request: Request) -> dict:
        runtime_value = authorize_agent(request, agent_id)
        try:
            runtime_value.repository.get(source_id)
            revision = runtime_value.repository.revision(source_id, version_no)
        except KeyError:
            raise HTTPException(404, "文档版本不存在") from None
        return {'version_no': revision['version_no'], 'markdown': revision['markdown'],
                'content_hash': revision['content_hash'], 'created_at': revision['created_at']}

    @app.get("/api/agents/{agent_id}/sources/{source_id}/segments")
    def source_segments(agent_id: str, source_id: str, request: Request, page: int = 1,
                        page_size: int = 20, keyword: str = "") -> dict:
        agent_runtime = authorize_agent(request, agent_id)
        try:
            source = agent_runtime.repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        page, page_size = max(page, 1), min(max(page_size, 1), 100)
        if not source.dify_document_id:
            return {"items": [], "page": page, "page_size": page_size, "may_have_more": False, "indexed": False}
        try:
            segments = agent_runtime.dify.get_segments(source.dify_document_id, keyword[:200], page, page_size)
        except Exception:
            logger.warning("Failed to retrieve Dify document segments for source %s", source_id)
            raise HTTPException(502, "无法获取 Dify 文档分段，请检查索引服务") from None
        markdown_path = Path(source.local_md_path)
        markdown = markdown_path.read_text(encoding="utf-8", errors="replace") if markdown_path.is_file() else ""
        items = [{"id": str(item.get("id") or ""), "content": str(item.get("content") or ""),
                  "position": item.get("position"), "status": item.get("status"),
                  "word_count": item.get("word_count"), "tokens": item.get("tokens"),
                  "page": item.get("page") or item.get("page_number"),
                  "chapter": item.get("chapter") or item.get("section")}
                 for item in segments]
        for item in items:
            if item["page"] is None or not item["chapter"]:
                page_location, chapter = locate_exact_segment(markdown, item["content"])
                if item["page"] is None:
                    item["page"] = page_location
                if not item["chapter"]:
                    item["chapter"] = chapter
        return {"items": items, "page": page, "page_size": page_size,
                "may_have_more": len(items) == page_size, "indexed": True}

    @app.get("/api/agents/{agent_id}/sources/{source_id}/outline")
    def source_outline(agent_id: str, source_id: str, request: Request) -> dict:
        agent_runtime = authorize_agent(request, agent_id)
        try:
            agent_runtime.repository.get(source_id)
            outline = agent_runtime.tools.get_document_outline(source_id, max_items=300)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except FileNotFoundError:
            raise HTTPException(404, "解析后的 Markdown 不存在") from None
        markdown_path = Path(agent_runtime.repository.get(source_id).local_md_path)
        headings = []
        current_page = None
        if markdown_path.is_file():
            for line_no, line in enumerate(markdown_path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                marker = PAGE_MARKER.search(line)
                if marker:
                    current_page = int(marker.group(1))
                match = re.match(r'^(#{1,6})\s+(.+?)\s*#*$', line)
                if match:
                    headings.append({'level': len(match.group(1)), 'title': match.group(2), 'line': line_no, 'page': current_page})
        return {"outline": outline[0].content if outline else "", "headings": headings}

    @app.get("/api/agents/{agent_id}/sources/{source_id}/lifecycle")
    def source_lifecycle(agent_id: str, source_id: str, request: Request) -> dict:
        runtime_value = authorize_agent(request, agent_id)
        try:
            source = runtime_value.repository.get(source_id)
        except KeyError:
            raise HTTPException(404, '文件不存在') from None
        return {'source_id': source_id, 'stages': [
            {'name': 'upload', 'label': '文件上传', 'status': 'success' if source.local_source_path or source.local_pdf_path else 'pending'},
            {'name': 'parse', 'label': '文档解析', 'status': source.parse_status, 'error': source.error_message},
            {'name': 'metadata', 'label': '元数据提取', 'status': source.metadata_status, 'error': source.metadata_error},
            {'name': 'index', 'label': 'Dify 索引', 'status': source.dify_status, 'error': source.error_message},
            {'name': 'platform', 'label': '文件平台', 'status': source.pdf_platform_status},
        ]}

    @app.get("/api/agents/{agent_id}/sources/{source_id}", response_model=Source)
    def get_agent_source(agent_id: str, source_id: str, request: Request) -> Source:
        try:
            return authorize_agent(request, agent_id).repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/retry", response_model=Source)
    def retry_agent_source(agent_id: str, source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return runtime_value.ingestion.retry_failed(source_id, source_platform_user=current_user(request).username)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/reparse", response_model=Source)
    def reparse_agent_source(agent_id: str, source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return runtime_value.ingestion.reparse_source(source_id, source_platform_user=current_user(request).username)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except Exception as exc:
            raise HTTPException(502, f"重新解析失败：{str(exc)[:500]}") from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/refresh-dify-status", response_model=Source)
    def refresh_agent_dify_status(agent_id: str, source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return runtime_value.ingestion.refresh_dify_status(source_id)
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
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            result = runtime_value.ingestion.regenerate_rewrite_preview(source_id, value.rewrite_prompt)
            return RewritePreviewResponse.model_validate(result)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/confirm-rewrite", response_model=Source)
    def confirm_agent_rewrite(agent_id: str, source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            source = runtime_value.ingestion.confirm_rewrite(source_id, source_platform_user=current_user(request).username)
            ingest_queue_repository.complete_source(source_id)
            return source
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/agents/{agent_id}/sources/{source_id}/use-original", response_model=Source)
    def use_agent_original(agent_id: str, source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            source = runtime_value.ingestion.use_original(source_id, source_platform_user=current_user(request).username)
            ingest_queue_repository.complete_source(source_id)
            return source
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/api/agents/{agent_id}/sources/{source_id}", status_code=204)
    def delete_agent_source(agent_id: str, source_id: str, request: Request) -> None:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            runtime_value.ingestion.delete_source(source_id)
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
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
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
                agent_id, knowledge_base.knowledge_base_id, knowledge_base.name, runtime_value.repository.list_all(),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/agents/{agent_id}/sources/delete-batch', response_model=DeleteJob, status_code=202)
    def delete_batch_agent_sources(agent_id: str, value: BatchReparseCreate, request: Request) -> DeleteJob:
        runtime_value = authorize_agent(request, agent_id)
        kb_id = runtime_value.profile.knowledge_base_id
        require_kb_write(request, kb_id)
        ids = list(dict.fromkeys(value.source_ids))
        if len(ids) > settings.ingest_max_files_per_job:
            raise HTTPException(400, f'单个任务最多 {settings.ingest_max_files_per_job} 个文件')
        related_ids = {item.profile.agent_id for item in registry.runtimes() if item.profile.knowledge_base_id == kb_id}
        if any(ingest_queue_repository.has_active_job(item) for item in related_ids):
            raise HTTPException(409, '知识库有未完成的入库、解析或索引任务，请先完成或取消')
        try:
            sources = [runtime_value.repository.get(source_id) for source_id in ids]
            return delete_queue_repository.create(agent_id, kb_id, runtime_value.profile.name, sources)
        except KeyError:
            raise HTTPException(404, '文件不存在') from None
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

    @app.post("/api/delete-jobs/{job_id}/cancel", response_model=DeleteJob)
    def cancel_delete_job(job_id: str, request: Request) -> DeleteJob:
        try:
            job = delete_queue_repository.get(job_id)
            if not can_use_kb(request, job.knowledge_base_id):
                raise HTTPException(403, "无权访问该清空任务")
            require_kb_write(request, job.knowledge_base_id)
            return delete_queue_repository.cancel(job_id)
        except KeyError:
            raise HTTPException(404, "清空任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.put("/api/agents/{agent_id}/sources/{source_id}/metadata", response_model=Source)
    def update_agent_source_metadata(agent_id: str, source_id: str, value: SourceMetadataUpdate, request: Request) -> Source:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
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
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
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
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
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
    def list_sources(request: Request, limit: int = 100, offset: int = 0) -> list[Source]:
        return authorize_agent(request, registry.default_agent_id).repository.list(min(max(limit, 1), 500), max(offset, 0))

    @app.get("/api/sources/{source_id}", response_model=Source)
    def get_source(source_id: str, request: Request) -> Source:
        try:
            return authorize_agent(request, registry.default_agent_id).repository.get(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/sources/{source_id}/retry", response_model=Source)
    def retry_source(source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, registry.default_agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return runtime_value.ingestion.retry_failed(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.post("/api/sources/{source_id}/refresh-dify-status", response_model=Source)
    def refresh_dify_status(source_id: str, request: Request) -> Source:
        runtime_value = authorize_agent(request, registry.default_agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return runtime_value.ingestion.refresh_dify_status(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None

    @app.delete("/api/sources/{source_id}", status_code=204)
    def delete_source(source_id: str, request: Request) -> None:
        runtime_value = authorize_agent(request, registry.default_agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            runtime_value.ingestion.delete_source(source_id)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except Exception as exc:
            raise HTTPException(502, f"删除失败：{str(exc)[:300]}") from None

    @app.post("/api/ingest", response_model=IngestJob, status_code=202)
    async def ingest_files(background_tasks: BackgroundTasks, request: Request, files: list[UploadFile] = File(...), rewrite_enabled: bool = Form(False), rewrite_prompt: str = Form(""), workflow_enabled: bool = Form(False), rewrite_review_required: bool = Form(False)) -> IngestJob:
        agent_runtime = authorize_agent(request, registry.default_agent_id)
        require_kb_write(request, agent_runtime.profile.knowledge_base_id)
        return await _start_ingest(background_tasks, files, agent_runtime.ingestion, jobs, rewrite_enabled, rewrite_prompt, workflow_enabled, rewrite_review_required, current_user(request).username)

    @app.post("/api/agents/{agent_id}/ingest", response_model=IngestJob, status_code=202)
    async def ingest_agent_files(
        agent_id: str, request: Request, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...), rewrite_enabled: bool = Form(False), rewrite_prompt: str = Form(""), workflow_enabled: bool = Form(False), rewrite_review_required: bool = Form(False)
    ) -> IngestJob:
        agent_runtime = authorize_agent(request, agent_id)
        require_kb_write(request, agent_runtime.profile.knowledge_base_id)
        return await _start_ingest(background_tasks, files, agent_runtime.ingestion, jobs, rewrite_enabled, rewrite_prompt, workflow_enabled, rewrite_review_required, current_user(request).username)

    @app.post("/api/agents/{agent_id}/ingest-jobs", response_model=IngestJob, status_code=201)
    def create_ingest_job(agent_id: str, value: BatchIngestCreate, request: Request) -> IngestJob:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        if delete_queue_repository.active_for_knowledge_base(runtime_value.profile.knowledge_base_id):
            raise HTTPException(409, '知识库正在删除文档，请稍后重试')
        if len(value.filenames) > settings.ingest_max_files_per_job:
            raise HTTPException(400, f"单个任务最多 {settings.ingest_max_files_per_job} 个文件")
        invalid = [name for name in value.filenames if Path(name).suffix.casefold() not in DOCUMENT_TYPES]
        if invalid:
            raise HTTPException(400, f"存在不支持的文件格式：{Path(invalid[0]).name}")
        try:
            return ingest_queue_repository.create(agent_id, value, current_user(request).username)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/agents/{agent_id}/reparse-jobs", response_model=IngestJob, status_code=202)
    def create_reparse_job(agent_id: str, value: BatchReparseCreate, request: Request) -> IngestJob:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        source_ids = list(dict.fromkeys(value.source_ids))
        if len(source_ids) > settings.ingest_max_files_per_job:
            raise HTTPException(400, f"单个任务最多 {settings.ingest_max_files_per_job} 个文件")
        if delete_queue_repository.active_for_knowledge_base(runtime_value.profile.knowledge_base_id):
            raise HTTPException(409, "知识库正在删除文档，请稍后重试")
        try:
            sources = [runtime_value.repository.get(source_id) for source_id in source_ids]
            return ingest_queue_repository.create_reparse(agent_id, sources, current_user(request).username)
        except KeyError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/agents/{agent_id}/reindex-jobs', response_model=IngestJob, status_code=202)
    def create_reindex_job(agent_id: str, value: BatchReparseCreate, request: Request) -> IngestJob:
        runtime_value = authorize_agent(request, agent_id)
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        source_ids = list(dict.fromkeys(value.source_ids))
        if len(source_ids) > settings.ingest_max_files_per_job:
            raise HTTPException(400, f'单个任务最多 {settings.ingest_max_files_per_job} 个文件')
        if delete_queue_repository.active_for_knowledge_base(runtime_value.profile.knowledge_base_id):
            raise HTTPException(409, '知识库正在删除文档，请稍后重试')
        try:
            sources = [runtime_value.repository.get(source_id) for source_id in source_ids]
            return ingest_queue_repository.create_reparse(agent_id, sources, current_user(request).username, 'document_reindex')
        except KeyError:
            raise HTTPException(404, '文件不存在') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

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
        runtime_value = authorize_agent(request, str(job_record["agent_id"]))
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
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
            temporary = target_dir / f".upload-{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as handle:
                    received = 0
                    while chunk := await upload.read(1024 * 1024):
                        received += len(chunk)
                        if item['expected_size'] is not None and received > item['expected_size']:
                            raise HTTPException(400, f"文件大小不匹配：{filename}")
                        handle.write(chunk)
                if item['expected_size'] is not None and received != item['expected_size']:
                    raise HTTPException(400, f"文件大小不匹配：{filename}")
                try:
                    ingest_queue_repository.mark_uploaded(item["item_id"], temporary, target)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from None
            finally:
                temporary.unlink(missing_ok=True)
        return ingest_queue_repository.get(job_id)

    @app.post("/api/jobs/{job_id}/start", response_model=IngestJob)
    def start_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        runtime_value = authorize_agent(request, str(job_record["agent_id"]))
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
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
            item.profile.agent_id for item in registry.runtimes()
            if can_use_kb(request, item.profile.knowledge_base_id)
        }
        if agent_id:
            if agent_id not in visible_agent_ids:
                raise HTTPException(403, "无权访问该 Agent 的任务")
            visible_agent_ids = {agent_id}
        values = []
        if task_type in {"", "document_ingest", "document_reparse", "document_reindex"}:
            ingest_tasks = ingest_queue_repository.task_summaries(visible_agent_ids)
            values.extend(item for item in ingest_tasks if not task_type or item.task_type == task_type)
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
        runtime_value = authorize_agent(request, str(job_record["agent_id"]))
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return ingest_queue_repository.pause(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/jobs/{job_id}/resume", response_model=IngestJob)
    def resume_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        runtime_value = authorize_agent(request, str(job_record["agent_id"]))
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return ingest_queue_repository.resume(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/jobs/{job_id}/retry-failed", response_model=IngestJob)
    def retry_failed_batch_ingest(job_id: str, request: Request) -> IngestJob:
        job_record = ingest_queue_repository.job_record(job_id)
        runtime_value = authorize_agent(request, str(job_record["agent_id"]))
        require_kb_write(request, runtime_value.profile.knowledge_base_id)
        try:
            return ingest_queue_repository.retry_failed(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/jobs/{job_id}/cancel", response_model=IngestJob)
    def cancel_batch_ingest(job_id: str, request: Request) -> IngestJob:
        try:
            record = ingest_queue_repository.job_record(job_id)
            runtime_value = authorize_agent(request, str(record["agent_id"]))
            require_kb_write(request, runtime_value.profile.knowledge_base_id)
            return ingest_queue_repository.cancel(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/jobs/{job_id}/events")
    def get_job_events(job_id: str, request: Request, limit: int = 100, before_id: int | None = None) -> dict:
        try:
            record = ingest_queue_repository.job_record(job_id)
            authorize_agent(request, str(record['agent_id']))
            return ingest_queue_repository.events(job_id, min(max(limit, 1), 200), before_id)
        except KeyError:
            raise HTTPException(404, '任务不存在') from None

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
        return await execute_agent(authorize_agent(http_request, registry.default_agent_id), request, http_request, "ask")

    @app.post("/api/retrieve", response_model=RetrievalResponse)
    async def retrieve(request: AskRequest, http_request: Request) -> RetrievalResponse:
        return await execute_agent(authorize_agent(http_request, registry.default_agent_id), request, http_request, "retrieve")

    def openai_api_user(request: Request) -> dict | None:
        authorization = request.headers.get('authorization', '')
        if settings.external_api_enabled and verify_bearer_key(authorization, settings.external_api_key):
            request.state.auth_user = AuthenticatedUser('api', False, 'api_key')
            return None
        integration = scoped_integration(request)
        integration['_token'] = authorization.partition(' ')[2].strip()
        request.state.auth_user = AuthenticatedUser(integration['id'], False, 'integration')
        return integration

    def openai_integration_runtime(integration: dict, kb_id: str) -> AgentRuntime:
        if kb_id not in integration['knowledge_base_ids']:
            raise HTTPException(403, '该接入未获知识库授权')
        if not integration['owner_is_admin'] and not identity_can_use_kb(integration['owner'], kb_id):
            raise HTTPException(403, '接入所有者不再拥有该知识库')
        return runtime(f'@kb:{kb_id}')

    @app.get('/v1/models')
    def openai_models(request: Request) -> dict[str, object]:
        integration = openai_api_user(request)
        now = int(time.time())
        if integration is not None:
            models = []
            for kb_id in integration['knowledge_base_ids']:
                try:
                    openai_integration_runtime(integration, kb_id)
                except HTTPException:
                    continue
                models.append(f'@kb:{kb_id}')
        else:
            models = [item.profile.agent_id for item in registry.runtimes()]
        return {'object': 'list', 'data': [
            {'id': model, 'object': 'model', 'created': now, 'owned_by': 'agentic-rag'}
            for model in models
        ]}

    @app.post('/v1/chat/completions')
    async def openai_chat_completions(payload: dict[str, object], request: Request) -> dict[str, object]:
        integration = openai_api_user(request)
        if payload.get('stream', False) is not False:
            raise HTTPException(400, '暂不支持流式响应')
        messages = payload.get('messages')
        if not isinstance(messages, list) or not messages:
            raise HTTPException(400, 'messages 必须是非空数组')
        for item in messages:
            if (not isinstance(item, dict) or item.get('role') not in ('user', 'assistant')
                    or not isinstance(item.get('content'), str) or not item['content'].strip()
                    or len(item['content']) > 12000):
                raise HTTPException(400, '仅支持非空文本的 user 和 assistant 消息')
        if messages[-1]['role'] != 'user' or len(messages[-1]['content']) > 4000:
            raise HTTPException(400, '最后一条消息必须是最多 4000 字符的 user 消息')
        model = payload.get('model', registry.default_agent_id)
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(400, 'model 必须是非空字符串')
        conversation_id = payload.get('user', 'openai')
        if not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 120:
            raise HTTPException(400, 'user 必须是 1 至 120 字符的字符串')
        history = [AskHistoryMessage(role=item['role'], content=item['content']) for item in messages[:-1][-12:]]
        if integration is not None:
            if not model.startswith('@kb:') or len(model) <= 4:
                raise HTTPException(403, '请使用已授权知识库的模型 ID')
            runtime_value = openai_integration_runtime(integration, model[4:])
        else:
            runtime_value = authorize_agent(request, model)
        value = AskRequest(question=messages[-1]['content'], conversation_id=conversation_id, history=history)
        result = await execute_agent(runtime_value, value, request, 'ask', integration=integration)
        return {'id': f'chatcmpl-{uuid.uuid4().hex}', 'object': 'chat.completion', 'created': int(time.time()),
                'model': model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': result.answer}, 'finish_reason': 'stop'}],
                'agentic_rag': {'run_id': result.run_id, 'sources': [source.model_dump() for source in result.sources], 'trace': [event.model_dump() for event in result.trace]}}

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
