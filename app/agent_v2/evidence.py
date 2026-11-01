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

    def resolve(self, ref):
        kb, doc, vid = (ref[k] for k in ('knowledge_base_id','document_id','version_id'))
        repo, requested, scope = self.scope(kb, doc, vid)
        request = RetrievalRequest(query='evidence', scopes=[requested], budget={'item_chars':6000})
        loc = ref['locator']
        candidate = dict(document_id=doc, version_id=vid, chunk_id=ref.get('chunk_id'),
                         start=loc.get('start'), end=loc.get('end'))
        hits = EvidenceResolver(repo, scope, request, time.monotonic()+5).resolve([candidate], 'repository')
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

    def collect(self, output, limit, diagnostics=None):
        diagnostics = diagnostics if diagnostics is not None else {}
        diagnostics.update(tool_candidate_count=0, registered_evidence_count=0, registration_rejections={})
        if output.get('status') != 'success':
            return {}, []
        data = output.get('data') or {}
        candidates = list(data.get('results', [])) + list(data.get('expansions', []))
        if data.get('document_id'):
            base = {k:data[k] for k in ('knowledge_base_id','document_id','version_id')}
            if 'content' in data:
                candidates.append({**base, 'locator':{'start':data['start'], 'end':data['start']+len(data['content'])}})
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
        refs, snippets = {}, []
        diagnostics['tool_candidate_count'] = len(candidates)
        def rejected(reason):
            counts = diagnostics['registration_rejections']
            counts[reason] = counts.get(reason, 0) + 1
        if len(candidates)>limit:
            diagnostics['registration_rejections']['evidence_budget'] = len(candidates)-limit
        for item in candidates[:limit]:
            if item.get('verification_status') not in (None,'verified_current','verified_historical'):
                rejected('unverified_candidate')
                continue
            try:
                hit, spec = self.resolve(item)
            except (KeyError, ValueError, PermissionError, TimeoutError) as exc:
                rejected({KeyError:'missing_identity', ValueError:'content_or_locator_invalid',
                    PermissionError:'scope_or_version_invalid', TimeoutError:'resolution_timeout'}[type(exc)])
                continue
            content=hit.content[:min(2000,len(item.get('content',hit.content)))]
            value = hit.model_dump(exclude={'content'})
            value.update(content_length=len(content), content_hash=hashlib.sha256(content.encode()).hexdigest(),
                         scope_digest=digest(spec), trace_id=output['trace_id'],
                         source_adapter=item.get('source_adapter','repository'), provenance=item.get('provenance',[]),
                         source_rank=item.get('source_rank',hit.source_rank),fused_score=item.get('fused_score',0),
                         index_generation=item.get('index_generation'),
                         content_truncated=hit.content_truncated or len(content)<len(hit.content))
            ident = 'ev_' + digest([hit.key(), value['content_hash']])[:16]
            refs[ident] = value
            snippets.append(dict(evidence_id=ident, content=content, **value))
        diagnostics['registered_evidence_count'] = len(refs)
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
