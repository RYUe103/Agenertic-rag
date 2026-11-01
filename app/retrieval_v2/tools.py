"""One callable tool boundary for REST and future Agent/MCP consumers."""
import json
import time
import uuid
from typing import Any
from pydantic import Field
from .models import StrictModel, Scope, Filters, RetrievalRequest
from .evidence import EvidenceResolver, text_window
from .calculations import CalculationInput, calculate


class ReadInput(StrictModel):
    knowledge_base_id: str = Field(min_length=1,max_length=200)
    document_id: str = Field(min_length=1,max_length=200)
    version_id: str | None = Field(default=None,max_length=300)
    node_id: str | None = Field(default=None,max_length=400)
    section_id: str | None = Field(default=None,max_length=400)
    page: int | None = Field(default=None,ge=1)
    start: int = Field(default=0,ge=0,le=20_000_000)
    after: int = Field(default=-1,ge=-1)
    limit: int = Field(default=50,ge=1,le=100)
    max_chars: int = Field(default=12000,ge=1,le=100000)
    outline_after: int = Field(default=-1,ge=-1)
    neighbors: int = Field(default=1,ge=0,le=5)
    row: int | None = Field(default=None,ge=0)
    column: int | None = Field(default=None,ge=0)


class ToolOutput(StrictModel):
    version: str = '2'
    trace_id: str
    status: str
    data: dict[str,Any] | None = None
    error: dict[str,str] | None = None
    elapsed_ms: float


HIGH = {
    'calculate':'使用已验证 evidence.content 或本轮用户给定数字进行 Decimal 运算。值唯一时省略 start/end，由服务器精确定位；重复值才提供偏移。不知道单位时不要编造。',
    'search_knowledge':'自动规划并检索知识，返回已验证原文证据',
    'find_document':'检索并聚合候选文档，返回来源排名',
    'read_document':'按 Unicode 偏移分页读取指定文档版本',
    'read_document_section':'读取指定章节的原文 Block',
    'read_document_table':'查找或读取版本化结构表格',
    'find_exact_text':'精确原文搜索，保留匹配位置和续查令牌',
    'expand_evidence':'按已验证 Chunk 扩展相邻原文',
}
LOW = {
    'search_bm25':'使用本地 Tantivy BM25', 'search_vector':'使用 Dify semantic_search 并校验证据',
    'grep_chunks':'受限正则或文字原文查找', 'get_chunk':'读取并校验单个本地 Chunk',
    'get_adjacent_chunks':'读取同版本同构建的相邻 Chunk', 'get_document_outline':'读取版本化章节目录',
    'get_document_page':'读取可靠页码关联的原文 Block', 'get_table':'读取结构化表格',
    'get_table_cells':'分页读取单元格及行列坐标', 'get_evidence_context':'恢复 Chunk 相邻上下文',
}
DESCRIPTIONS={'v2.'+k:v for k,v in {**HIGH,**LOW}.items()}
SEARCH={'search_knowledge','find_document','find_exact_text','search_bm25','search_vector','grep_chunks'}


def input_schema(name):
    if name not in DESCRIPTIONS:
        raise KeyError(name)
    return CalculationInput if name == 'v2.calculate' else RetrievalRequest if name[3:] in SEARCH else ReadInput


def catalog():
    return [dict(name=name,version='2',description=desc,
        layer='high' if name[3:] in HIGH else 'low',risk='read',
        required_permission='tool.invoke' if name[3:] in HIGH else 'tool.advanced',
        input_schema=input_schema(name).model_json_schema(),output_schema=ToolOutput.model_json_schema(),
        timeout_seconds=30 if name[3:] in SEARCH else 5,max_output_bytes=1_000_000,
        errors=['invalid_input','forbidden','approval_required','not_found','output_budget','timeout','internal_error'])
        for name,desc in DESCRIPTIONS.items()]


class ToolService:
    def __init__(self, retrieval, configurations, audit, authorize_tool, actor, channel='internal', approvals=None):
        self.retrieval,self.configurations,self.audit = retrieval,configurations,audit
        self.authorize_tool,self.actor,self.channel,self.approvals=authorize_tool,actor,channel,approvals

    def preflight(self, name, arguments):
        model=input_schema(name).model_validate(arguments)
        kbs=[s.knowledge_base_id for s in model.scopes] if isinstance(model,RetrievalRequest) else [model.knowledge_base_id]
        config=self.configurations.get(name)
        if not config['enabled']:
            raise PermissionError('tool_disabled')
        self.authorize_tool('tool.invoke' if name[3:] in HIGH else 'tool.advanced')
        self.authorize_tool(str(config.get('permission','tool.invoke')))
        for kb in kbs:
            self.retrieval.access.authorize_kb(kb)
        return model, kbs, config

    def invoke(self, name, arguments, approval_id=None, *, calculation_context=None):
        started=time.monotonic(); status='failed'; kbs=[]
        try:
            model, kbs, config=self.preflight(name,arguments)
            if config.get('requires_approval'):
                if len(kbs)!=1 or not self.approvals or not approval_id or not self.approvals.consume(
                        approval_id,self.actor,kbs[0],name,model.model_dump()):
                    raise PermissionError('approval_required')
            tool=name[3:]
            if isinstance(model,CalculationInput):
                data=calculate(model,calculation_context)
            elif isinstance(model,RetrievalRequest):
                modes={'search_bm25':'lexical','search_vector':'vector','find_exact_text':'exact','grep_chunks':'exact'}
                if tool in modes:
                    model=RetrievalRequest.model_validate({**model.model_dump(),'mode':modes[tool]})
                response=self.retrieval.search(model)
                data=response.model_dump()
                if tool=='find_document':
                    data={'documents':data['documents'],'document_start':model.document_start,'diagnostics':data['diagnostics'],'plan':data['plan'],'status':data['status']}
            else:
                data=self.read(tool,model,started+5)
            for kb in kbs:
                self.retrieval.access.authorize_kb(kb)
            self.authorize_tool('tool.invoke' if tool in HIGH else 'tool.advanced')
            if not self.configurations.get(name)['enabled']:
                raise PermissionError('tool_disabled')
            if len(json.dumps(data,ensure_ascii=False).encode())>1_000_000:
                raise OverflowError('output_budget')
            status='success'
            return ToolOutput(trace_id=uuid.uuid4().hex,status='success',data=data,elapsed_ms=(time.monotonic()-started)*1000)
        except (ValueError,TypeError,PermissionError,KeyError,OverflowError,TimeoutError) as exc:
            code=('forbidden' if isinstance(exc,PermissionError) else 'not_found' if isinstance(exc,KeyError)
                  else 'output_budget' if isinstance(exc,OverflowError) else 'timeout' if isinstance(exc,TimeoutError) else 'invalid_input')
            if isinstance(exc,PermissionError) and str(exc)=='approval_required':
                code='approval_required'
            return ToolOutput(trace_id=uuid.uuid4().hex,status='error',error={'code':code,**({'reason':str(exc),'hint':'使用当前可见 source_id；value 必须逐字复制原文含千分位。重复值需准确偏移；先精读，不要重试相同参数。'} if name=='v2.calculate' and str(exc) in {
                'operand_not_unique_supply_offsets','operand_source_mismatch','incomplete_range','partial_number',
                'unit_not_unique_supply_offsets','unit_source_mismatch','invalid_number','numeric_budget',
                'two_operands_required','incompatible_units_convert_explicitly','compound_units_not_supported',
                'unsupported_unit_conversion','incompatible_dimensions','invalid_arithmetic'} else {})},elapsed_ms=(time.monotonic()-started)*1000)
        except (RuntimeError,OSError):
            return ToolOutput(trace_id=uuid.uuid4().hex,status='error',error={'code':'internal_error'},
                              elapsed_ms=(time.monotonic()-started)*1000)
        finally:
            for kb in kbs:
                self.audit.record(kb,name,self.actor,self.channel,status,int((time.monotonic()-started)*1000))

    def read(self, tool, value, deadline):
        repo=self.retrieval.repositories[value.knowledge_base_id]
        requested=Scope(knowledge_base_id=value.knowledge_base_id,document_ids=[value.document_id],
                        versions={value.document_id:value.version_id} if value.version_id else {})
        scope=self.retrieval.access.scope(repo,requested,Filters())
        spec=scope.get(value.document_id)
        if spec is None:
            raise KeyError(value.document_id)
        vid=spec['version_id']
        result={'knowledge_base_id':repo.knowledge_base_id,'document_id':value.document_id,'version_id':vid,
                'verification_status':'verified_current' if vid==spec['current_version_id'] else 'verified_historical'}
        if tool in {'get_chunk','expand_evidence','get_adjacent_chunks','get_evidence_context'}:
            if not value.node_id:
                raise ValueError('node_id required')
            nodes=repo.adjacent_chunks(value.document_id,value.node_id,vid,
                before=0 if tool=='get_chunk' else value.neighbors,after=0 if tool=='get_chunk' else value.neighbors)
            request=RetrievalRequest(query='read',scopes=[requested],budget={'context_chars':value.max_chars})
            resolver=EvidenceResolver(repo,scope,request,deadline)
            hits=resolver.resolve([dict(document_id=value.document_id,version_id=vid,chunk_id=n['id']) for n in nodes],'repository')
            if resolver.rejected:
                raise ValueError('Evidence verification failed')
            remaining=value.max_chars;items=[]
            for hit in hits:
                if remaining<=0: break
                if len(hit.content)>remaining:
                    hit=hit.model_copy(update={'content':hit.content[:remaining],'content_truncated':True})
                items.append(hit.model_dump());remaining-=len(hit.content)
            result.update(items=items,partial=len(items)<len(hits) or any(h['content_truncated'] for h in items))
        else:
            with repo.sources.connect() as conn:
                conn.execute('BEGIN')
                if tool=='read_document':
                    text,finished=text_window(conn,vid,value.start,value.max_chars,deadline)
                    result.update(content=text,start=value.start,next_start=None if finished else value.start+len(text))
                    # Navigation handles let high-level Agents select a section/table.
                    outline=[]
                    for row in conn.execute("SELECT id,ordinal,kind,substr(json_extract(payload,'$.title'),1,200) AS title,json_extract(payload,'$.block_id') AS block_id FROM document_nodes WHERE version_id=? AND kind IN ('sections','tables') ORDER BY ordinal,id LIMIT ? OFFSET ?",(vid,min(value.limit,12)+1,value.outline_after+1)):
                        if time.monotonic()>=deadline: raise TimeoutError()
                        outline.append({'id':row['id'],'kind':row['kind'],'title':row['title'] or '',
                                        'block_id':row['block_id'],'ordinal':row['ordinal']})
                    result.update(outline=outline[:min(value.limit,12)],outline_partial=len(outline)>min(value.limit,12),
                        outline_next_after=value.outline_after+min(value.limit,12) if len(outline)>min(value.limit,12) else None)
                else:
                    kind={'read_document_section':'blocks','get_document_page':'blocks',
                          'get_document_outline':'sections','get_table_cells':'cells',
                          'read_document_table':'tables','get_table':'tables'}[tool]
                    clauses=['version_id=?','kind=?','ordinal>?'];args=[vid,kind,value.after]
                    if tool=='read_document_section':
                        if not value.section_id: raise ValueError('section_id required')
                        if not conn.execute("SELECT 1 FROM document_nodes WHERE id=? AND version_id=? AND kind='sections'",
                                            (value.section_id,vid)).fetchone():
                            raise KeyError(value.section_id)
                        clauses.append("EXISTS(SELECT 1 FROM json_each(payload,'$.locator.section_ids') WHERE value=?)")
                        args.append(value.section_id)
                    if tool=='get_document_page':
                        if not value.page: raise ValueError('page required')
                        clauses.append("EXISTS(SELECT 1 FROM json_each(payload,'$.locator.page_numbers') WHERE value=?)")
                        args.append(value.page)
                    if tool in {'get_table','get_table_cells'} and not value.node_id:
                        raise ValueError('node_id required')
                    if value.node_id:
                        clauses.append("json_extract(payload,'$.table_id')=?" if kind=='cells' else 'id=?')
                        args.append(value.node_id)
                    for key in ('row','column'):
                        val=getattr(value,key)
                        if val is not None and kind=='cells':
                            clauses.append(f"json_extract(payload,'$.{key}')=?");args.append(val)
                    if kind=='tables' and value.node_id:
                        from .table_page import table_page
                        table_size=conn.execute("SELECT length(payload) FROM document_nodes WHERE id=? AND version_id=? AND kind='tables'",(value.node_id,vid)).fetchone()
                        if not table_size: raise KeyError(value.node_id)
                        if table_size[0]>1_000_000: raise OverflowError('table_materialization_budget')
                        table=repo.node(value.document_id,value.node_id,vid)
                        block=repo.node(value.document_id,table['block_id'],vid)
                        # Existing small-table callers retain the complete table object.
                        if value.start or len(json.dumps(table,ensure_ascii=False))>value.max_chars or len(table.get('cells',[]))>value.limit:
                            result.update(table_page(table,block,value.start,value.limit,value.max_chars))
                            if self.retrieval.access.scope(repo,requested,Filters())!=scope:
                                raise PermissionError('scope_changed')
                            if time.monotonic()>deadline: raise TimeoutError()
                            return result
                    # Check sizes before materializing large tables/blocks.
                    order="json_extract(payload,'$.row'),json_extract(payload,'$.column'),id" if kind=='cells' else 'ordinal,id'
                    rows=conn.execute('SELECT id,ordinal,length(payload) AS chars FROM document_nodes WHERE '+
                        ' AND '.join(clauses)+' ORDER BY '+order+' LIMIT ? OFFSET ?',
                        (*args,value.limit+1,value.start if kind=='cells' else 0)).fetchall()
                    items=[];size=0;budget_partial=False
                    for row in rows[:value.limit]:
                        if time.monotonic()>=deadline: raise TimeoutError()
                        if kind=='tables' and row['chars']>value.max_chars and not items:
                            item=json.loads(conn.execute('SELECT payload FROM document_nodes WHERE id=?',(row['id'],)).fetchone()[0])
                            handle={k:item[k] for k in ('id','block_id','format','unit') if k in item}
                            handle['table_id']=handle['id']
                            items.append(handle)
                            continue
                        size+=row['chars']
                        if size>value.max_chars:
                            if not items: raise OverflowError('output_budget')
                            budget_partial=True
                            break
                        item=json.loads(conn.execute('SELECT payload FROM document_nodes WHERE id=?',(row['id'],)).fetchone()[0])
                        items.append(item)
                    partial=len(rows)>value.limit or budget_partial
                    result.update(items=items,partial=partial,
                        next_after=rows[len(items)-1]['ordinal'] if partial and kind!='cells' else None,
                        next_start=value.start+len(items) if partial and kind=='cells' else None)
        if self.retrieval.access.scope(repo,requested,Filters())!=scope:
            raise PermissionError('scope_changed')
        if time.monotonic()>deadline: raise TimeoutError()
        return result
