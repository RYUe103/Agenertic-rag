import json
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver


def checkpoint_trace(directory, result):
    path=directory/'checkpoints.db'
    if not path.exists():
        return dict(available=False,tools=[],visible_evidence=[],evidence={},search_count=0)
    with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True) as connection:
        saver=SqliteSaver(connection)
        cp=saver.get_tuple({'configurable':{'thread_id':result['thread_id']}})
    if not cp:
        return dict(available=False,tools=[],visible_evidence=[],evidence={},search_count=0)
    state=cp.checkpoint['channel_values'];calls={};outputs={};order=[]
    for message in state.get('messages',[]):
        for call in getattr(message,'tool_calls',[]):
            if call['id'] not in calls:
                order.append(call['id']);calls[call['id']]=call
        ident=getattr(message,'tool_call_id',None)
        if ident:
            outputs[ident]=getattr(message,'artifact',None) or {}
    tools=[];seen_searches=set();duplicates=0
    for ident in order:
        call=calls[ident];output=outputs.get(ident,{})
        name=call['name'].replace('v2__','v2.')
        is_search=name in {'v2.search_knowledge','v2.find_document','v2.find_exact_text','v2.search_bm25','v2.search_vector','v2.grep_chunks'}
        fingerprint=json.dumps([name,call['args']],ensure_ascii=False,sort_keys=True)
        repeated=is_search and fingerprint in seen_searches
        if is_search: seen_searches.add(fingerprint)
        duplicates+=int(repeated)
        data=output.get('data') or {}
        tools.append(dict(call_id=ident,name=name,arguments=call['args'],status=output.get('status'),error=output.get('error'),
            repeated_search=repeated,is_search=is_search,results=data.get('results',[]),documents=data.get('documents',[]),
            diagnostics=data.get('diagnostics',{}),registered_evidence=list(output.get('_evidence',{})),
            registration=output.get('_metrics',{}),observation_evidence=output.get('evidence',[])))
    return dict(available=True,tools=tools,visible_evidence=state.get('visible_evidence',[]),evidence=state.get('evidence',{}),
                sources=state.get('sources',{}),search_count=sum(t['is_search'] for t in tools),
                read_count=sum(t['name'].startswith(('v2.read_','v2.get_','v2.expand_')) for t in tools),
                repeated_searches=duplicates,stop_reason=state.get('stop_reason'))


def relevant(hit, question):
    doc=hit.get('document_id');locator=hit.get('locator',{})
    start,end=locator.get('start'),locator.get('end')
    result=set()
    for gold in question['gold_evidence']:
        if doc!=gold['document_id']:
            continue
        if start is not None and end is not None and max(start,gold['start'])<min(end,gold['end']):
            result.add(gold['evidence_id'])
    return result


def rank_metrics(hits, question):
    gold_docs=set(question['gold_document_ids']);gold_spans={g['evidence_id'] for g in question['gold_evidence']}
    ids=[h.get('document_id') for h in hits]
    out={'returned':len(hits),'mrr':next((1/(i+1) for i,d in enumerate(ids) if d in gold_docs),0) if gold_docs else None}
    for k in (5,10):
        docs=set(ids[:k]) & gold_docs
        spans=set().union(*(relevant(h,question) for h in hits[:k])) if hits else set()
        out[f'document_hit_at_{k}']=bool(docs) if gold_docs else None
        out[f'document_recall_at_{k}']=len(docs)/len(gold_docs) if gold_docs else None
        out[f'passage_hit_at_{k}']=bool(spans) if gold_spans else None
        out[f'passage_recall_at_{k}']=len(spans)/len(gold_spans) if gold_spans else None
    return out


def retrieval_metrics(trace, question):
    searches=[t for t in trace['tools'] if t.get('is_search')]
    ranked=[dict(call_id=t['call_id'],**rank_metrics(t['results'],question)) for t in searches]
    all_hits=[h for t in searches for h in t['results']]
    return dict(searches=ranked, first_search=ranked[0] if ranked else None,
                any_gold_document_retrieved=any(h.get('document_id') in question['gold_document_ids'] for h in all_hits) if searches else None,
                any_gold_passage_retrieved=any(relevant(h,question) for h in all_hits) if searches else None,
                judgment_scope='Annotated positive passage overlap, NOT answer-span recall; cross-question relevance may be incomplete.')
