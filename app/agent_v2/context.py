"""Bounded provider input views; never mutate checkpoint evidence or tool receipts."""
import json
import math
from langchain_core.messages import ToolMessage
from langchain_core.messages.utils import convert_to_openai_messages

TOOL_OUTPUT_PROFILE = {
    'search': dict(item_chars=4000, output_chars=16000, navigation_limit=8),
    'navigation': dict(item_chars=4000, output_chars=6000, navigation_limit=20),
    'read': dict(item_chars=12000, output_chars=24000, navigation_limit=12),
    'table': dict(item_chars=12000, output_chars=24000, navigation_limit=12),
    'calculate': dict(item_chars=0, output_chars=6000, navigation_limit=0),
}


def profile(name):
    name = name.replace('v2__', '').removeprefix('v2.')
    kind = ('calculate' if name == 'calculate' else 'table' if 'table' in name else
            'navigation' if name in {'find_document', 'get_document_outline'} else
            'search' if name in {'search_knowledge','search_bm25','search_vector','find_exact_text','grep_chunks'} else 'read')
    return kind, TOOL_OUTPUT_PROFILE[kind]


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


class TokenBudgetManager:
    """Tokenizer unavailable: explicit mixed-text estimate + 25% safety margin.

    ASCII runs / 3, non-ASCII up to UTF-8 byte count (rare scripts), message
    framing and schemas included. This is NOT provider usage or an exact tokenizer.
    A bounded observed underestimate factor is retained in checkpoints.
    """
    estimator = 'mixed_text_v1_25pct_margin_not_model_tokenizer'

    def __init__(self, budget, factor=1):
        self.budget = budget
        self.factor = max(1, min(4, factor))

    def tokens(self, value):
        text = value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        ascii_count = sum(ord(c)<128 for c in text)
        other = sum(1.5 if '\u4e00'<=c<='\u9fff' else len(c.encode()) for c in text if ord(c)>=128)
        return math.ceil((ascii_count/3 + other + 16)*1.25*self.factor)

    def measure(self, messages, tools, used):
        schema = self.tokens(tools) if tools else 0
        estimate = self.tokens(convert_to_openai_messages(messages)) + schema
        observations = sum(self.tokens(m.content) for m in messages if isinstance(m,ToolMessage))
        evidence_tokens=0
        for message in messages:
            if isinstance(message,ToolMessage):
                try: evidence_tokens+=sum(self.tokens(e['content']) for e in json.loads(message.content).get('evidence',[]))
                except (ValueError,TypeError,KeyError): pass
        return dict(estimated_input_tokens=estimate,tool_schema_tokens=schema,
                    observation_tokens=observations,evidence_tokens=evidence_tokens,history_tokens=max(0,estimate-schema-observations),
                    remaining_run_tokens=max(0,self.budget.total_tokens-used),
                    context_window_tokens=self.budget.context_tokens,output_reserved_tokens=self.budget.completion_tokens,
                    estimator=self.estimator,estimator_factor=self.factor)

    def fits(self, messages, tools, used):
        stats = self.measure(messages,tools,used)
        total = stats['estimated_input_tokens']+self.budget.completion_tokens
        return (sum(len(str(m.content)) for m in messages)<=self.budget.context_chars and
                total<=self.budget.context_tokens and total<=stats['remaining_run_tokens'])

    def pack(self, messages, tools, used):
        view=list(messages);before=sum(len(str(m.content)) for m in view);count=0
        indices=[i for i,m in enumerate(view) if isinstance(m,ToolMessage)]
        # Keep call/result pairs; replace only old observation bodies with navigation.
        # Calculations are retained, and never become new Evidence.
        for pos,i in enumerate(indices):
            if self.fits(view,tools,used) and (pos>=len(indices)-2 or len(str(view[i].content))<12000): continue
            message=view[i]
            try: body=json.loads(message.content)
            except (TypeError,ValueError): continue
            if body.get('data',{}).get('operation'): continue
            navigation=[{k:e[k] for k in ('source_id','knowledge_base_id','document_id','version_id','locator') if k in e}
                        for e in body.get('evidence',[])]
            compact=dict(status=body.get('status'),evidence=[],navigation=[{'action':'read_document',**n} for n in navigation],
                         requires_reread=True,data={k:v for k,v in body.get('data',{}).items()
                         if k in {'document_id','version_id','knowledge_base_id','next_start','next_after','operation','result','unit'}})
            if body.get('error'): compact['error']=body['error']
            content=json.dumps(compact,ensure_ascii=False)
            if len(content)<len(message.content):
                view[i]=message.model_copy(update={'content':content});count+=1
        after=sum(len(str(m.content)) for m in view)
        return view,dict(context_compaction_count=count,context_chars_before=before,context_chars_after=after,
                         **self.measure(view,tools,used))

    def usage(self, response, estimate):
        raw=response.response_metadata.get('usage') or {}
        prompt=raw.get('prompt_tokens',raw.get('input_tokens'))
        completion=raw.get('completion_tokens',raw.get('output_tokens'))
        # Do not add a reservation to real usage. Missing fields use bounded estimates.
        p=prompt if isinstance(prompt,int) and prompt>=0 else estimate
        c=completion if isinstance(completion,int) and completion>=0 else self.tokens(str(response.content)+str(response.tool_calls))
        total=raw.get('total_tokens')
        return dict(prompt_tokens=p,completion_tokens=c,total_tokens=max(p+c,total or 0)),prompt


def pack_observation(visible, cap, kind):
    """Whole verified snippets, independent navigation budget, bounded best-fit.

    Search keeps first ranked evidence then alternates documents before additional
    hits from the same source. No rewriting of original text or table projections.
    """
    original=visible
    result={'status':visible.get('status'),'evidence':[],'data':{}}
    if visible.get('error'): result['error']=visible['error']
    data=visible.get('data',{})
    metadata_budget=min(3000,max(100,cap//5))
    navigation=[];metadata_omitted=False
    for key,value in data.items():
        if key in {'documents','outline','tables'}:
            result['data'][key]=[]
            for item in value:
                proposed={**result['data'],key:[*result['data'][key],item]}
                if size(proposed)>metadata_budget:
                    metadata_omitted=True;break
                result['data']=proposed
            if len(result['data'][key])<len(value): metadata_omitted=True
        elif key=='table_page':
            # Header/units/cell coordinates are inseparable from row evidence.
            if size(value)+size(result)+128>cap:
                metadata_omitted=True
            else: result['data'][key]=value
        elif key in {'operation','result','unit','precision','operands'}:
            result['data'][key]=value
        elif size({**result['data'],key:value})<=metadata_budget:
            result['data'][key]=value
        else: metadata_omitted=True
    if 'outline' in data and len(result['data'].get('outline',[]))<len(data['outline']):
        start=data.get('outline_start',0)
        result['data']['outline_next_after']=start+len(result['data'].get('outline',[]))-1
    if 'documents' in data and len(result['data'].get('documents',[]))<len(data['documents']):
        start=data.get('document_start',data.get('next_document_start',len(data['documents']))-len(data['documents']))
        result['data']['next_document_start']=max(0,start)+len(result['data'].get('documents',[]))
    if metadata_omitted:
        navigation.append({'action':'repeat_read_with_pagination',**{k:data[k] for k in (
            'knowledge_base_id','document_id','version_id','next_start','next_after','outline_next_after') if k in data}})
    snippets=visible.get('evidence',[])
    if kind=='search':
        first=[];extra=[];docs=set()
        for snippet in snippets:
            doc=(snippet.get('knowledge_base_id'),snippet.get('document_id'))
            (extra if doc in docs else first).append(snippet);docs.add(doc)
        ordered=first+extra
    else: ordered=snippets
    reserved=min(1200,cap//8)
    for snippet in ordered:
        proposed={**result,'evidence':[*result['evidence'],snippet]}
        # A table page must retain its structural companion or be reread as a unit.
        structure_ok='table_page' not in data or 'table_page' in result['data']
        if structure_ok and size(proposed)<=cap-reserved:
            result['evidence'].append(snippet)
        else:
            navigation.append({'action':'read_document','source_id':snippet['source_id'],
                **{k:snippet[k] for k in ('knowledge_base_id','document_id','version_id') if snippet.get(k)}})
    navigation.extend(visible.get('navigation',[]))
    for item in navigation:
        proposed={**result,'navigation':[*result.get('navigation',[]),item]}
        if size(proposed)+180<=cap:result=proposed
        else: break
    omitted=len(snippets)-len(result['evidence'])
    if omitted or metadata_omitted:
        result['partial']=True
        result['selection']={'evidence_omitted':omitted,'metadata_omitted':metadata_omitted}
    if size(result)>cap:
        # Calculation receipt too large: no invented result, exact retry guidance.
        result={'status':'error','error':{'code':'observation_budget'},'evidence':[],
                'data':{},'navigation':[{'action':'narrow_read_or_operands'}]}
    return result,bool(omitted or metadata_omitted or result.get('status')!=original.get('status'))
