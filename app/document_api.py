"""Phase 1 read API. Authorization is shared with existing tenant/knowledge-base policy."""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
import json
from app.document_repository import DocumentReadService


def document_router(authorize_agent, require_write):
    router = APIRouter(prefix='/api/v2/knowledge-bases/{kb}/documents')

    def service(request, kb):
        runtime = authorize_agent(request, f'@kb:{kb}')
        def check(knowledge_base_id, document_id):
            if knowledge_base_id != kb:
                raise HTTPException(403, '知识库不匹配')
            current = authorize_agent(request, f'@kb:{kb}')
            current.repository.get(document_id)
        return DocumentReadService(runtime.repository.documents, check)

    def read(request, kb, doc, operation, **kwargs):
        try:
            result=service(request, kb).read(operation, doc, **kwargs)
            if operation != 'artifact' and len(json.dumps(jsonable_encoder(result),ensure_ascii=False))>2_000_000:
                raise HTTPException(413,'结果过大，请使用 text/chunks/blocks 分页读取')
            return result
        except KeyError:
            raise HTTPException(404, '文档、版本或结构不存在') from None

    @router.post('/backfill')
    def backfill(kb: str, request: Request):
        runtime = authorize_agent(request, f'@kb:{kb}')
        require_write(request, kb)
        return runtime.repository.documents.backfill_all()

    @router.get('/{doc}')
    def document(kb: str, doc: str, request: Request):
        return read(request, kb, doc, 'document')

    @router.get('/{doc}/versions')
    def versions(kb: str, doc: str, request: Request):
        return read(request, kb, doc, 'versions')

    @router.get('/{doc}/content')
    def content(kb: str, doc: str, request: Request, version_id: str | None = None):
        return read(request, kb, doc, 'version', version_id=version_id)

    @router.get('/{doc}/structure')
    def structure(kb: str, doc: str, request: Request, version_id: str | None = None):
        # All subreads go through the same boundary; IDs remain version scoped.
        version_id = read(request, kb, doc, 'version', version_id=version_id).id
        return {kind: read(request, kb, doc, 'nodes', kind=kind, version_id=version_id)
                for kind in ('pages', 'sections', 'blocks', 'chunks', 'tables')}

    @router.get('/{doc}/nodes/{node_id}')
    def node(kb: str, doc: str, node_id: str, request: Request, version_id: str | None = None):
        return read(request, kb, doc, 'node', node_id=node_id, version_id=version_id)

    @router.get('/{doc}/chunks/{chunk_id}/adjacent')
    def adjacent(kb: str, doc: str, chunk_id: str, request: Request, version_id: str | None = None,
                 before: int = 1, after: int = 1):
        return read(request, kb, doc, 'adjacent_chunks', chunk_id=chunk_id,
                    version_id=version_id, before=before, after=after)

    @router.get('/{doc}/blocks')
    def blocks(kb: str, doc: str, request: Request, version_id: str | None = None,
               page: int | None = None, section_id: str | None = None, after: int = -1, limit: int = 200):
        return read(request, kb, doc, 'located_blocks', version_id=version_id, page=page, section_id=section_id,after=after,limit=limit)

    @router.get('/{doc}/chunks')
    def chunks(kb: str, doc: str, request: Request, version_id: str | None = None,
               builder_version: str | None = None, after: int = -1, limit: int = 100):
        return read(request,kb,doc,'chunks_page',version_id=version_id,builder_version=builder_version,after=after,limit=limit)

    @router.get('/{doc}/text')
    def text(kb: str, doc: str, request: Request, version_id: str | None = None, start: int = 0, limit: int = 100000):
        if start<0 or not 1<=limit<=100000:
            raise HTTPException(400,'start 必须非负，limit 必须为 1..100000')
        return read(request,kb,doc,'text_window',version_id=version_id,start=start,limit=limit)

    @router.get('/{doc}/tables')
    def tables(kb: str, doc: str, request: Request, version_id: str | None = None, query: str = ''):
        tables = read(request, kb, doc, 'nodes', kind='tables', version_id=version_id)
        return [table for table in tables if not query or query.casefold() in table['raw'].casefold()]

    @router.get('/{doc}/outline')
    def outline(kb: str, doc: str, request: Request, version_id: str | None = None):
        return read(request, kb, doc, 'nodes', kind='sections', version_id=version_id)

    @router.get('/{doc}/mappings')
    def mappings(kb: str, doc: str, request: Request, version_id: str | None = None):
        return read(request, kb, doc, 'mappings', version_id=version_id)

    @router.get('/{doc}/artifacts')
    def artifacts(kb: str, doc: str, request: Request, version_id: str | None = None):
        return read(request, kb, doc, 'artifacts', version_id=version_id)

    @router.get('/{doc}/artifacts/{name}')
    def artifact(kb: str, doc: str, name: str, request: Request, version_id: str | None = None):
        return Response(read(request, kb, doc, 'artifact', name=name, version_id=version_id), media_type='application/octet-stream')

    return router
