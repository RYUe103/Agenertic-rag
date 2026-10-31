"""Opt-in execution API and lightweight SSE, sharing the legacy auth boundary."""
import asyncio
import json
from urllib.parse import quote
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import Field
from app.retrieval_v2.models import StrictModel, Scope
from app.retrieval_v2.access import AccessPolicy
from app.retrieval_v2.adapters import DifyVectorAdapter
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.tools import ToolService
from app.schemas import AskResponse, Evidence, RetrievalSource, AgentTraceEvent
from .runtime import ReActRuntime
from .checkpoint import ExecutionStore
from .state import ExecutionBudget


class RunRequest(StrictModel):
    agent_id: str = Field(min_length=1,max_length=200)
    question: str = Field(default='',max_length=4000)
    conversation_id: str = Field(default='default',min_length=1,max_length=120)
    document_ids: list[str] | None = Field(default=None,max_length=1000)
    versions: dict[str,str] = Field(default_factory=dict,max_length=1000)


class AgentAPI:
    def __init__(self,settings,registry,authorize_read,principal,authorize_tool,configs,audit,approvals,concurrency,reranker=None):
        self.settings,self.registry,self.authorize_read,self.principal=settings,registry,authorize_read,principal
        self.authorize_tool,self.configs,self.audit,self.approvals=authorize_tool,configs,audit,approvals
        self.concurrency,self.reranker=concurrency,reranker
        # Separate new runtime files, initialized lazily; no corpus migration at startup.
        self.directory=settings.app_data_dir/'agent-v2'

    def prepare(self,request,body,emit=None):
        original=self.authorize_read(request,body.agent_id)
        if original.profile.agent_type=='supervisor':
            raise HTTPException(409,'Supervisor 保持 legacy；请选择具体 Agent 运行 react_v2')
        profile=original.profile;kb=profile.knowledge_base_id;owner=self.principal(request)
        scopes=[Scope(knowledge_base_id=kb,document_ids=body.document_ids,versions=body.versions).model_dump()]
        def authorize():
            if self.principal(request)!=owner: raise PermissionError('principal_changed')
            current=self.authorize_read(request,body.agent_id).profile
            if current.knowledge_base_id!=kb or current.react_tools!=profile.react_tools or not current.enabled:
                raise PermissionError('agent_configuration_changed')
        def kb_access(value):
            authorize()
            if value!=kb: raise PermissionError('knowledge_base_outside_scope')
            self.authorize_read(request,'@kb:'+kb)
        access=AccessPolicy(kb_access,owner)
        retrieval=UnifiedRetrievalService({kb:original.repository.documents},access,
                                           {kb:DifyVectorAdapter(original.dify)},self.reranker)
        service=ToolService(retrieval,self.configs,self.audit,lambda p:self.authorize_tool(request,p),
                            owner,'agent-react-v2',self.approvals)
        settings=self.settings
        budget=ExecutionBudget(tool_calls=profile.max_tool_calls,retrieval_rounds=profile.max_retrieval_rounds,
            evidence_count=profile.max_evidence_for_answer,iterations=settings.agent_max_iterations,
            tool_seconds=settings.agent_tool_seconds,total_seconds=settings.agent_total_seconds,
            context_chars=settings.agent_context_chars,output_chars=settings.agent_output_chars,
            consecutive_failures=settings.agent_consecutive_failures)
        return ReActRuntime(ExecutionStore(self.directory),service,original.agent.llm,
            principal=owner,agent_id=body.agent_id,scopes=scopes,allowed_tools=profile.react_tools,
            budget=budget,authorize=authorize,emit=emit)

    def owned(self,request,thread):
        store=ExecutionStore(self.directory)
        with store.connect() as db:
            row=db.execute('SELECT * FROM threads WHERE id=? AND owner=?',(thread,self.principal(request))).fetchone()
        if not row: raise HTTPException(404,'Thread 不存在')
        scopes=json.loads(row['scopes'])
        body=RunRequest(agent_id=row['agent'],conversation_id=row['conversation'],
                        document_ids=scopes[0]['document_ids'],versions=scopes[0]['versions'])
        runtime=self.prepare(request,body)
        store.thread(runtime.principal,runtime.agent_id,runtime.scopes,body.conversation_id,thread)
        return runtime,dict(row)

    async def execute(self,runtime,**kwargs):
        key=json.dumps([runtime.principal,runtime.agent_id,kwargs.get('thread_id') or kwargs.get('conversation','default')])
        try:
            async with self.concurrency.acquire('react_v2:'+key):
                result=await asyncio.get_running_loop().run_in_executor(self.concurrency.executor,lambda:runtime.run(**kwargs))
                result['response']=self.legacy_response(result,self.registry.get(runtime.agent_id).profile).model_dump()
                return result
        except PermissionError:
            raise HTTPException(403,'运行权限或证据范围已变化') from None
        except (ValueError,RuntimeError):
            raise HTTPException(409,'运行状态冲突；请检查审批、Thread 或模型配置') from None
        except TimeoutError:
            raise HTTPException(429,'运行预算或并发额度已用尽') from None

    def legacy_response(self,result,profile):
        final=result.get('result') or {};citations=final.get('citations',{})
        evidence=[];sources=[]
        for ident,item in citations.items():
            loc=item['locator'];url=(f"/api/v2/knowledge-bases/{quote(item['knowledge_base_id'],safe='')}/documents/"
                f"{quote(item['document_id'],safe='')}/content?version_id={quote(item['version_id'],safe='')}")
            evidence.append(Evidence(evidence_id=ident,source_id=item['document_id'],filename=item['document_name'],
                content=item['content'],retrieval_type=item['source_adapter'],page=next(iter(item['page_numbers']),None),
                location=f"version={item['version_id']}; range={loc.get('start')}:{loc.get('end')}",pdf_url=url))
            sources.append(RetrievalSource(source_id=item['document_id'],filename=item['document_name'],url=url,pages=item['page_numbers']))
        trace=[AgentTraceEvent(sequence=i+1,kind='tool' if e['type'].startswith('tool_') else 'observation',
                title=e['type'],tool_name=e.get('tool_name'),status='error' if e['type']=='tool_failed' else 'success')
                for i,e in enumerate(result['events'])]
        return AskResponse(run_id=result['run_id'],thread_id=result['thread_id'],runtime_mode='react_v2',
            agent_id=profile.agent_id,agent_name=profile.name,answer=final.get('answer','等待人工审批。'),
            evidences=evidence,sources=sources,process=[e['title'] for e in [t.model_dump() for t in trace]],trace=trace,
            tool_calls=result['tool_calls'],evidence_insufficient=final.get('evidence_insufficient',True))

    def router(self):
        router=APIRouter(prefix='/api/v2/agent')

        @router.post('/run')
        async def run(body:RunRequest,request:Request):
            runtime=self.prepare(request,body)
            return await self.execute(runtime,question=body.question,conversation=body.conversation_id)

        @router.post('/threads/{thread}/resume')
        async def resume(thread:str,request:Request):
            runtime,_=self.owned(request,thread)
            return await self.execute(runtime,thread_id=thread,resume=True)

        @router.get('/threads/{thread}')
        def status(thread:str,request:Request):
            runtime,row=self.owned(request,thread)
            with runtime.store.lock(thread),runtime.store.saver() as saver:
                snapshot=runtime.build(saver).get_state({'configurable':{'thread_id':thread}})
                pending=[i.value for task in snapshot.tasks for i in task.interrupts]
            # Checkpoints/messages are intentionally never returned by this endpoint.
            return dict(thread_id=thread,status=row['status'],run_id=snapshot.values.get('run_id'),
                        pending_approvals=pending,tool_calls=snapshot.values.get('tool_call_count',0))

        @router.post('/threads/{thread}/cancel')
        def cancel(thread:str,request:Request):
            runtime,_=self.owned(request,thread);runtime.store.cancel(thread)
            return {'status':'cancellation_requested'}

        @router.delete('/threads/{thread}')
        def archive(thread:str,request:Request):
            runtime,_=self.owned(request,thread);runtime.store.archive(thread)
            return {'status':'archived'}

        async def stream_response(request,runtime,kwargs):
            queue=asyncio.Queue(maxsize=512);loop=asyncio.get_running_loop()
            runtime.emit_callback=lambda event:loop.call_soon_threadsafe(queue.put_nowait,event)
            async def worker():
                try:
                    result=await self.execute(runtime,**kwargs)
                    queue.put_nowait({'type':'result','data':result})
                except Exception:
                    queue.put_nowait({'type':'failed','code':'execution_failed'})
                finally: queue.put_nowait(None)
            task=asyncio.create_task(worker())
            async def events():
                try:
                    while True:
                        event=await queue.get()
                        if event is None: break
                        yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                finally:
                    if not task.done() and hasattr(runtime,'thread_id'):
                        runtime.store.cancel(runtime.thread_id)
            return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

        @router.post('/stream')
        async def stream(body:RunRequest,request:Request):
            return await stream_response(request,self.prepare(request,body),
                                         dict(question=body.question,conversation=body.conversation_id))

        @router.post('/threads/{thread}/resume/stream')
        async def resume_stream(thread:str,request:Request):
            runtime,_=self.owned(request,thread)
            return await stream_response(request,runtime,dict(thread_id=thread,resume=True))

        return router
