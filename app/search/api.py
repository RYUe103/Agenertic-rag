from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from app.chunk_builder import ChunkConfig
from .service import LocalSearchService
from .exact_search import ContinuationInvalid


class SearchInput(BaseModel):
    query: str = Field(min_length=1,max_length=512)
    top_k: int = Field(default=10,ge=1,le=100)
    document_ids: list[str] | None = Field(default=None,max_length=1000)
    filters: dict = Field(default_factory=dict)


class ExactInput(BaseModel):
    query: str = Field(min_length=1,max_length=256)
    document_ids: list[str] | None = Field(default=None,max_length=10000)
    versions: dict[str,str] = Field(default_factory=dict)
    filters: dict = Field(default_factory=dict)
    continuation: str | None = Field(default=None,max_length=4096)
    regex_mode: bool = False
    case_sensitive: bool = True
    limit: int = Field(default=20,ge=1,le=100)
    max_chars: int = Field(default=2_000_000,ge=1,le=20_000_000)
    timeout: float = Field(default=1,gt=0,le=2)


class BuildInput(BaseModel):
    full: bool = False
    chunk_size: int | None = Field(default=None,ge=200,le=16000)
    overlap: int = Field(default=180,ge=0)


class RollbackInput(BaseModel):
    generation_id: str = Field(pattern=r'^[a-f0-9]{32}$')


def search_router(authorize_read, require_write, principal):
    router = APIRouter(prefix='/api/v2/knowledge-bases/{kb}/local-search')

    def service(request,kb):
        runtime = authorize_read(request,f'@kb:{kb}')
        def authorize(knowledge_base_id,document_id):
            if kb != knowledge_base_id:
                raise PermissionError('Knowledge base mismatch')
            current = authorize_read(request,f'@kb:{kb}')
            current.repository.get(document_id)
        return LocalSearchService(runtime.repository.documents,authorize,principal(request))

    def invoke(fn):
        try:
            return fn()
        except ContinuationInvalid as exc:
            raise HTTPException(409,str(exc)) from None
        except (ValueError,KeyError) as exc:
            raise HTTPException(400,str(exc)) from None
        except (RuntimeError,OSError):
            raise HTTPException(503,'本地索引未就绪或需要重建，请查看索引状态') from None

    @router.post('/bm25')
    def bm25(kb: str, request: Request, body: SearchInput):
        return invoke(lambda: service(request,kb).search(**body.model_dump()))

    @router.post('/exact')
    def exact(kb: str, request: Request, body: ExactInput):
        return invoke(lambda: service(request,kb).exact(**body.model_dump()))

    @router.get('/status')
    def status(kb: str, request: Request):
        require_write(request,kb)
        return [{'id':r['id'],'status':r['status'],'error':r['error']} for r in service(request,kb).manager.state()]

    @router.get('/diagnostics')
    def diagnostics(kb: str, request: Request):
        require_write(request,kb)
        return service(request,kb).manager.status()

    @router.post('/cleanup')
    def cleanup(kb: str, request: Request):
        require_write(request,kb)
        return invoke(lambda: service(request,kb).manager.cleanup())

    @router.post('/sync')
    def sync(kb: str, request: Request, body: BuildInput):
        require_write(request,kb)
        return invoke(lambda: service(request,kb).manager.sync(full=body.full,
            config=ChunkConfig(body.chunk_size,body.overlap) if body.chunk_size else None))

    @router.post('/rollback')
    def rollback(kb: str, request: Request, body: RollbackInput):
        require_write(request,kb)
        return invoke(lambda:service(request,kb).manager.rollback(body.generation_id))

    return router
