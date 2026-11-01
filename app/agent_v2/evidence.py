"""Compact run-local references; every reuse reads and authorizes Repository again."""
import hashlib
import re
import time
from app.retrieval_v2.models import Scope, Filters, RetrievalRequest
from app.retrieval_v2.evidence import EvidenceResolver
from .checkpoint import digest


class EvidenceRegistry:
    def __init__(self, service, scopes):
        self.service = service
        self.scopes = {s['knowledge_base_id']: Scope.model_validate(s) for s in scopes}

    def scope(self, kb, doc, version):
        declared = self.scopes[kb]
        if declared.document_ids is not None and doc not in declared.document_ids:
            raise PermissionError('document_outside_scope')
        versions = {doc: declared.versions[doc]} if doc in declared.versions else {}
        requested = Scope(knowledge_base_id=kb, document_ids=[doc], versions=versions)
        repo = self.service.retrieval.repositories[kb]
        scope = self.service.retrieval.access.scope(repo, requested, Filters())
        if doc not in scope or scope[doc]['version_id'] != version:
            raise PermissionError('evidence_stale_or_unavailable')
        return repo, requested, scope

    def resolve(self, ref, deadline=None):
        kb, doc, vid = (ref[k] for k in ('knowledge_base_id','document_id','version_id'))
        repo, requested, scope = self.scope(kb, doc, vid)
        request = RetrievalRequest(query='evidence', scopes=[requested], budget={'item_chars':12000})
        loc = ref['locator']
        candidate = dict(document_id=doc, version_id=vid, chunk_id=ref.get('chunk_id'),
                         start=loc.get('start'), end=loc.get('end'))
        hits = EvidenceResolver(repo, scope, request, deadline if deadline is not None else time.monotonic()+5).resolve([candidate], 'repository')
        if len(hits) != 1:
            raise ValueError('evidence_unavailable')
        return hits[0], scope[doc]

    def load(self, ref):
        hit, spec = self.resolve(ref)
        if hit.locator != ref['locator'] or digest(spec) != ref['scope_digest']:
            raise PermissionError('evidence_changed')
        content = hit.content[:ref['content_length']]
        if hashlib.sha256(content.encode()).hexdigest() != ref['content_hash']:
            raise ValueError('evidence_content_changed')
        return hit.model_copy(update={'content':content,'source_adapter':ref['source_adapter'],
            'provenance':ref.get('provenance',[]),'source_rank':ref['source_rank'],
            'fused_score':ref.get('fused_score',0),'index_generation':ref.get('index_generation'),
            'content_truncated':ref.get('content_truncated',False)})

    def collect(self, output, limit, diagnostics=None, item_chars=4000, *, max_candidates_examined=100, seconds=5):
        diagnostics = diagnostics if diagnostics is not None else {}
        max_candidates_examined=max(0,min(200,max_candidates_examined))
        deadline=time.monotonic()+max(0,min(5,seconds))
        diagnostics.update(tool_candidate_count=0, registered_evidence_count=0, registration_rejections={},
            examined=0,rejected=0,duplicate=0,registered=0,budget_exhausted=False,budget_reason=None,
            max_candidates_examined=max_candidates_examined,max_evidence_registered=limit)
        seen=set()
        if output.get('status') != 'success':
            return {}, []
        data = output.get('data') or {}
        candidates = list(data.get('results', [])) + list(data.get('expansions', []))
        if data.get('document_id'):
            base = {k:data[k] for k in ('knowledge_base_id','document_id','version_id')}
            if 'content' in data:
                # Do not split a table into an arbitrary prefix. Only whole tables or
                # explicit row pages become evidence; deferred tables remain navigable.
                repo,_,_=self.scope(base['knowledge_base_id'],base['document_id'],base['version_id'])
                start=data['start'];end=start+len(data['content']);cursor=start
                with repo.sources.connect() as conn:
                    import json
                    tables=[json.loads(r[0]) for r in conn.execute(
                        "SELECT payload FROM document_nodes WHERE version_id=? AND kind='blocks' AND json_extract(payload,'$.kind') IN ('table_html','table_markdown') AND json_extract(payload,'$.locator.start')<? AND json_extract(payload,'$.locator.end')>? ORDER BY ordinal",
                        (base['version_id'],end,start))]
                def append_range(a,b):
                    for pos in range(a,b,item_chars):
                        candidates.append({**base,'locator':{'start':pos,'end':min(b,pos+item_chars)}})
                if len(data['content'])<=item_chars and all(t['locator']['start']>=start and t['locator']['end']<=end for t in tables):
                    append_range(start,end);cursor=end;tables=[]
                for table in tables:
                    a,b=table['locator']['start'],table['locator']['end']
                    append_range(cursor,max(cursor,a))
                    if a>=start and b<=end and b-a<=item_chars:
                        candidates.append({**base,'locator':{'start':a,'end':b}})
                    else:
                        output.setdefault('_deferred_tables',[]).append({**base,'table_ids':table['locator']['table_ids']})
                    cursor=max(cursor,min(end,b))
                append_range(cursor,end)
            for item in data.get('items', []):
                if item.get('verification_status'):
                    candidates.append(item)
                elif item.get('locator'):
                    candidates.append({**base, 'locator':item['locator']})
                elif item.get('block_id'):
                    repo, _, _ = self.scope(base['knowledge_base_id'], base['document_id'], base['version_id'])
                    block = repo.node(base['document_id'], item['block_id'], base['version_id'])
                    candidates.append({**base, 'locator':block['locator']})
                elif item.get('table_id'):
                    repo, _, _ = self.scope(base['knowledge_base_id'], base['document_id'], base['version_id'])
                    table=repo.node(base['document_id'],item['table_id'],base['version_id'])
                    block=repo.node(base['document_id'],table['block_id'],base['version_id'])
                    candidates.append({**base,'locator':block['locator']})
        output['_navigation']=list(output.get('_deferred_tables',[]))
        refs, snippets = {}, []
        diagnostics['tool_candidate_count'] = len(candidates)
        def rejected(reason):
            counts = diagnostics['registration_rejections']
            counts[reason] = counts.get(reason, 0) + 1
            diagnostics['rejected']+=1
        for item in candidates:
            if len(refs)>=limit: break
            if diagnostics['examined']>=max_candidates_examined or time.monotonic()>=deadline:
                diagnostics.update(budget_exhausted=True,budget_reason='candidate_count' if diagnostics['examined']>=max_candidates_examined else 'deadline')
                break
            diagnostics['examined']+=1
            identity=digest({k:item.get(k) for k in ('knowledge_base_id','document_id','version_id','chunk_id','locator','content','verification_status')})
            if identity in seen:
                diagnostics['duplicate']+=1
                continue
            seen.add(identity)
            if item.get('verification_status') not in (None,'verified_current','verified_historical'):
                rejected('unverified_candidate')
                continue
            try:
                hit, spec = self.resolve(item,deadline)
            except (KeyError, ValueError, PermissionError, TimeoutError) as exc:
                rejected('scope_or_version_invalid' if isinstance(exc,PermissionError) else
                    'resolution_timeout' if isinstance(exc,TimeoutError) else
                    'missing_identity' if isinstance(exc,KeyError) else 'content_or_locator_invalid')
                if isinstance(exc,TimeoutError) or time.monotonic()>=deadline:
                    diagnostics.update(budget_exhausted=True,budget_reason='deadline');break
                continue
            if time.monotonic()>=deadline:
                rejected('resolution_timeout');diagnostics.update(budget_exhausted=True,budget_reason='deadline');break
            output['_navigation'].append({k:v for k,v in hit.model_dump().items() if k in {
                'knowledge_base_id','document_id','version_id','chunk_id','locator','table_ids'}})
            content=hit.content[:min(item_chars,len(item.get('content',hit.content)))]
            # A prefix is a new exact source range, never the original full locator.
            partial=hit.content_truncated or len(content)<len(hit.content)
            if partial and hit.content_is_projection:
                rejected('projection_requires_full_read')
                continue
            if partial and hit.table_ids:
                rejected('table_requires_structured_read')
                continue
            if not hit.content_is_projection and (partial or hit.locator.get('end',0)-hit.locator.get('start',0)!=len(content)):
                original_end=hit.locator['end']
                exact={**item,'chunk_id':None,'locator':{'start':hit.locator['start'],'end':hit.locator['start']+len(content)}}
                try: hit,spec=self.resolve(exact,deadline)
                except (KeyError,ValueError,PermissionError,TimeoutError):
                    rejected('prefix_verification_failed');continue
                if time.monotonic()>=deadline:
                    rejected('resolution_timeout');diagnostics.update(budget_exhausted=True,budget_reason='deadline');break
                content=hit.content
            else:
                original_end=hit.locator.get('end')
            value = hit.model_dump(exclude={'content'})
            value.update(content_length=len(content), content_hash=hashlib.sha256(content.encode()).hexdigest(),
                         scope_digest=digest(spec), trace_id=output['trace_id'],
                         source_adapter=item.get('source_adapter','repository'), provenance=item.get('provenance',[]),
                         source_rank=item.get('source_rank',hit.source_rank),fused_score=item.get('fused_score',0),
                         index_generation=item.get('index_generation'),
                         content_truncated=partial,
                         table_context=data.get('table_page'),
                         continuation={'start':hit.locator['end'],'end':original_end} if partial and not hit.content_is_projection else None)
            ident = 'ev_' + digest([hit.key(), value['content_hash']])[:16]
            if ident in refs:
                diagnostics['duplicate']+=1;continue
            refs[ident] = value
            snippets.append(dict(evidence_id=ident, content=content, **value))
        diagnostics['registered_evidence_count'] = diagnostics['registered'] = len(refs)
        diagnostics['quota_filled']=len(refs)>=limit
        diagnostics['unexamined']=len(candidates)-diagnostics['examined']
        return refs, snippets

    def validate_answer(self, text, refs, *, allow_unreferenced=False):
        cited = list(dict.fromkeys(re.findall(r'\[(ev_[^\]\s]+)\]', text)))
        verified, errors = {}, []
        for ident in cited:
            try:
                verified[ident] = self.load(refs[ident]).model_dump()
            except (KeyError, ValueError, PermissionError, TimeoutError):
                errors.append('invalid_or_revoked_citation')
        pages = {p for hit in verified.values() for p in hit['page_numbers']}
        claimed_pages = {int(p) for p in re.findall(r'(?:第\s*(\d+)\s*页|\bpage\s+(\d+))', text, re.I) for p in p if p}
        if not claimed_pages <= pages:
            errors.append('unverified_page')
        location_claims=re.findall(r'(?:第\s*[0-9一二三四五六七八九十]+\s*[章节]|(?:表|Table|Section)\s*[A-Za-z]?\d+(?:[.-]\d+)*)',text,re.I)
        if any(not any(claim in hit['content'] for hit in verified.values()) for claim in location_claims):
            errors.append('unverified_structure_label')
        if re.search(r'https?://|file://', text):
            errors.append('model_generated_source_url')
        if not cited and not allow_unreferenced:
            errors.append('no_cited_evidence')
        return {'answer':text if not errors else '证据不足或引用未通过复核，无法给出可靠结论。',
                'citations':verified if not errors else {}, 'validation_errors':errors,
                'citation_validation':'passed' if not errors else 'rejected',
                'answer_correctness':'not_assessed', 'evidence_insufficient':bool(errors)}
