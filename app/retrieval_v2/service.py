import time
import uuid
from collections import Counter
from .models import RetrievalRequest, RetrievalResponse
from .planner import plan
from . import adapters, coordinator
from .evidence import EvidenceResolver
from .fusion import fuse, diversify
from .reranker import apply_ranking


class UnifiedRetrievalService:
    def __init__(self, repositories, access, vectors=None, reranker=None):
        self.repositories,self.access = repositories,access
        if any(kb!=repo.knowledge_base_id for kb,repo in repositories.items()):
            raise ValueError('Repository must be bound to its actual knowledge base')
        self.vectors,self.reranker = vectors or {},reranker

    def search(self, request: RetrievalRequest):
        started=time.monotonic()
        deadline=started+request.budget.timeout_seconds
        selected=plan(request)
        scopes={}
        for requested in request.scopes:
            kb=requested.knowledge_base_id
            scopes[kb]=self.access.scope(self.repositories[kb],requested,request.filters)
        prepared=time.monotonic()
        calls={}
        for kb,scope in scopes.items():
            repo=self.repositories[kb]
            for channel in selected['channels']:
                if channel=='lexical':
                    calls[kb+':lexical']=lambda r=repo,s=scope: adapters.lexical(r,s,request,selected['candidate_limit'])
                elif channel=='exact':
                    calls[kb+':exact']=lambda r=repo,s=scope: adapters.exact(r,s,request,selected['exact_query'],
                        self.access.principal,deadline,selected['channels']==['exact'])
                elif channel=='vector':
                    def vector(k=kb):
                        if k not in self.vectors:
                            raise adapters.BackendError('vector_unconfigured')
                        return self.vectors[k].search(request.query,selected['candidate_limit'],deadline)
                    calls[kb+':vector']=vector
        # Reserve time for evidence resolution and final permission revalidation.
        backend_deadline=started+request.budget.timeout_seconds*.7
        responses,diagnostics=coordinator.run(calls,backend_deadline)
        channels={c:[] for c in selected['channels']}
        rejected=Counter()
        exact={}
        raw_count=0
        evidence_start=time.monotonic()
        # Round-robin budget avoids starving later KBs/channels with one large backend.
        queues={k:list(v['candidates']) for k,v in sorted(responses.items())}
        bounded={k:[] for k in queues}
        while raw_count<request.budget.total_candidates and any(queues.values()):
            for name,queue in queues.items():
                if queue and raw_count<request.budget.total_candidates:
                    bounded[name].append(queue.pop(0)); raw_count+=1
        for name,body in sorted(responses.items()):
            kb,channel=name.rsplit(':',1)
            resolver=EvidenceResolver(self.repositories[kb],scopes[kb],request,deadline)
            hits=resolver.resolve(bounded[name],channel)
            channels[channel].extend(hits)
            rejected.update(resolver.rejected)
            diagnostics[name]['candidate_count']=len(body['candidates'])
            diagnostics[name]['verified_count']=len(hits)
            diagnostics[name]['candidates_truncated']=len(bounded[name])<len(body['candidates'])
            if 'scope_filter' in body:
                diagnostics[name]['scope_filter']=body['scope_filter']
            if 'exact' in body:
                exact[kb]={**body['exact'],'matches':[h.model_dump() for h in hits]}
        evidence_ms=(time.monotonic()-evidence_start)*1000
        fusion_start=time.monotonic()
        ranked,documents,deduplicated=fuse(channels,request.fusion)
        fusion_ms=(time.monotonic()-fusion_start)*1000
        rerank={'status':'disabled','elapsed_ms':0}
        if self.reranker and ranked and time.monotonic()<deadline:
            count=min(len(ranked),request.budget.rerank_candidates)
            texts=[r.content[:request.budget.rerank_item_chars] for r in ranked[:count]]
            seconds=min(request.budget.rerank_seconds,max(.001,deadline-time.monotonic()))
            output,stats=coordinator.run({'rerank':lambda:self.reranker.rank(request.query,texts,seconds)},
                                        min(deadline,time.monotonic()+seconds))
            rerank=stats['rerank']
            if output:
                try:
                    ranked=apply_ranking(ranked[:count],output['rerank'])+ranked[count:]
                except (ValueError,TypeError):
                    rerank={'status':'invalid_result_fallback'}
        if selected['channels']==['exact']:
            # No per-document diversity cap or rank fusion changes to occurrence semantics.
            chosen=sorted(ranked,key=lambda h:(h.knowledge_base_id,h.document_id,h.locator.get('start',0)))[:request.top_k]
        else:
            chosen=diversify(ranked,request.fusion.max_chunks_per_document,request.top_k)
        remaining=request.budget.context_chars
        results=[]
        for item in chosen:
            if remaining<=0:
                break
            if len(item.content)>remaining:
                item=item.model_copy(update={'content':item.content[:remaining],'content_truncated':True})
            results.append(item); remaining-=len(item.content)
        expansions=[]
        seen={h.key() for h in results}
        if request.expand_neighbors:
            for item in results:
                if not item.chunk_id or remaining<=0 or time.monotonic()>=deadline:
                    continue
                repo=self.repositories[item.knowledge_base_id]
                try:
                    adjacent=repo.adjacent_chunks(item.document_id,item.chunk_id,item.version_id,
                        before=request.expand_neighbors,after=request.expand_neighbors)
                    resolver=EvidenceResolver(repo,scopes[item.knowledge_base_id],request,deadline)
                    hits=resolver.resolve([dict(document_id=item.document_id,version_id=item.version_id,chunk_id=c['id'])
                                           for c in adjacent],'context')
                    for hit in hits:
                        if hit.key() in seen or remaining<=0:
                            continue
                        if len(hit.content)>remaining:
                            hit=hit.model_copy(update={'content':hit.content[:remaining],'content_truncated':True})
                        expansions.append(hit);seen.add(hit.key());remaining-=len(hit.content)
                except (KeyError,ValueError):
                    rejected['expansion_unavailable']+=1
        # Every KB is reauthorized even if it returned no candidates; revocation fails closed.
        changed=set()
        for requested in request.scopes:
            kb=requested.knowledge_base_id
            latest=self.access.scope(self.repositories[kb],requested,request.filters)
            for doc,spec in scopes[kb].items():
                if latest.get(doc)!=spec:
                    changed.add((kb,doc))
        if changed:
            results=[h for h in results if (h.knowledge_base_id,h.document_id) not in changed]
            expansions=[h for h in expansions if (h.knowledge_base_id,h.document_id) not in changed]
            documents=[h for h in documents if (h['knowledge_base_id'],h['document_id']) not in changed]
            for kb in exact:
                exact[kb]['matches']=[h for h in exact[kb]['matches'] if (kb,h['document_id']) not in changed]
                if any(k==kb for k,_ in changed):
                    exact[kb].update(continuation=None,partial=True,reason='scope_changed_restart')
            rejected['concurrent_scope_change']+=len(changed)
        # Only expose metadata from the authorized scope, never adapter-authored titles.
        for doc in documents:
            spec=scopes[doc['knowledge_base_id']][doc['document_id']]
            meta=spec['metadata']
            doc.update({k:meta.get(k) for k in ('filename','standard_title','standard_no','standard_year')})
            doc['document_name']=meta.get('filename') or ''
            doc['metadata_scope']='current_source_metadata'  # Historical identity remains version_id.
        # Raw occurrence bodies share the response budget; results hold their text only once.
        for value in exact.values():
            for hit in value['matches']:
                hit.pop('content',None)
        degraded=(any(d['status']!='ok' or d.get('candidates_truncated') for d in diagnostics.values())
                  or bool(rejected) or rerank['status'] not in {'ok','disabled'} or time.monotonic()>deadline)
        status=('degraded' if degraded else 'ok') if results else ('insufficient_evidence' if degraded else 'empty')
        return RetrievalResponse(trace_id=uuid.uuid4().hex,results=results,documents=documents,
            expansions=expansions,exact=exact,plan=selected,status=status,
            diagnostics=dict(backends=diagnostics,authorization_ms=(prepared-started)*1000,
                evidence_ms=evidence_ms,fusion_ms=fusion_ms,reranker=rerank,candidate_count=raw_count,
                deduplicated=deduplicated,rejected=dict(rejected),total_ms=(time.monotonic()-started)*1000,
                context_chars=request.budget.context_chars-remaining,
                deadline_exceeded=time.monotonic()>deadline))
