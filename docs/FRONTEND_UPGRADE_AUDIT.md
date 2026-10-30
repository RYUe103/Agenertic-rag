# Frontend upgrade audit

## Existing architecture

- Frontend: one native HTML page with one CSS file and one JavaScript file. There is no React, Vue, router, component library, build step, or typed API layer.
- Backend: FastAPI serves the `static/` directory and exposes authenticated JSON APIs.
- Authentication: local administrator plus optional LDAP session login. API key access is separate from browser sessions.
- Storage: SQLite repositories for sources, ingestion jobs, deletion jobs, and control-plane configuration.

## Existing working product areas

- Agent question answering, evidence display, and execution trace.
- Multi-file ingestion with batched upload, background workers, pause/resume, failed-item retry, and optional rewrite review.
- Paginated source list, Markdown preview, metadata editing/extraction, reparse, delete, and full-library deletion.
- Knowledge base, Agent, and model connection configuration.
- MinerU, Qwen-VL, Dify, file platform, workflow, LDAP, and model pool configuration in the backend.

## Backend capabilities without a complete frontend

- Persistent ingestion and deletion job records exist, but there is no unified task-history list API or standalone task center.
- Per-document parser diagnostics exist in `metadata.json`, but only a subset is exposed by the Markdown preview API.
- Health exposes concurrency and service state, but there is no monitoring history or metrics API.

## Missing backend capabilities

- MCP server registry, external integration instances, scoped token lifecycle, and call audit APIs.
- Sandbox/code execution runtime, resource policy, output artifact, and execution audit APIs.
- Unified BM25/vector/hybrid retrieval-test API with timing and metadata filters.
- Chunk list/detail, page/section tree, table extraction, document version, and index-version APIs.
- Generic task cancellation, per-stage pipeline events, structured task logs, and output artifact APIs.
- Organization, tenant, role, permission, audit-log, and historical monitoring data models.

These missing areas must remain explicit empty or unavailable states until corresponding backend contracts exist. The frontend must not fabricate successful data.

## Migration decision

The user explicitly selected Semi Design. The frontend therefore migrates to React + TypeScript + Vite + Semi Design on the isolated `codex/platform-frontend-upgrade` branch. FastAPI APIs and persistence remain authoritative; the Vite build continues to output to the existing `static/` directory.

## Initial delivery scope

1. Platform shell, authentication, navigation, and real status handling.
2. Document management, upload, Markdown preview, batch actions, and parser/index status.
3. Unified task center backed by the existing ingestion and deletion queues.
4. Preserve access to question answering and existing configuration workflows.
5. Record unsupported MCP, Sandbox, monitoring, audit, and advanced document APIs instead of mocking them.
