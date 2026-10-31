from fastapi import APIRouter, Request
from pydantic import Field
from .models import StrictModel, RetrievalRequest
from .access import AccessPolicy
from .adapters import DifyVectorAdapter
from .service import UnifiedRetrievalService
from .tools import ToolService, catalog


class Call(StrictModel):
    arguments: dict
    approval_id: str | None = Field(default=None,max_length=200)


def retrieval_router(authorize_read, principal, authorize_tool, configurations, audit, approvals, reranker=None):
    router=APIRouter(prefix='/api/v2/retrieval')

    def tools(request, arguments):
        kbs=([s['knowledge_base_id'] for s in arguments.get('scopes',[])]
             if 'scopes' in arguments else [arguments.get('knowledge_base_id')])
        repos,vectors={},{}
        for kb in kbs:
            if not isinstance(kb,str): continue
            runtime=authorize_read(request,'@kb:'+kb)
            repos[kb]=runtime.repository.documents
            vectors[kb]=DifyVectorAdapter(runtime.dify)
        access=AccessPolicy(lambda kb:authorize_read(request,'@kb:'+kb),principal(request))
        service=UnifiedRetrievalService(repos,access,vectors,reranker)
        return ToolService(service,configurations,audit,lambda permission:authorize_tool(request,permission),
                           principal(request),'rest-v2',approvals)

    @router.get('/tools')
    def list_tools(request: Request):
        authorize_tool(request,'tool.invoke')
        return [{**item,'governance':configurations.get(item['name'])} for item in catalog()]

    @router.post('/search')
    def search(request: Request, body: RetrievalRequest):
        return tools(request,body.model_dump()).invoke('v2.search_knowledge',body.model_dump())

    @router.post('/tools/{name}')
    def invoke(request: Request, name: str, body: Call):
        # Validate before resolving any scope supplied by the caller.
        from .tools import input_schema
        from fastapi import HTTPException
        from pydantic import ValidationError
        try:
            arguments=input_schema(name).model_validate(body.arguments).model_dump()
        except (KeyError,ValidationError):
            raise HTTPException(422,'Invalid V2 tool or arguments') from None
        return tools(request,arguments).invoke(name,arguments,body.approval_id)

    return router
