"""Independent retrieval backends. External text is never final evidence."""
import json
import time
import httpx
from app.search.index_manager import IndexManager
from app.search.exact_search import exact_search


class BackendError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def lexical(repo, scope, request, limit):
    manager = getattr(repo,'_local_index_manager',None)
    if manager is None:
        repo._local_index_manager = manager = IndexManager(repo)
    with manager.reader() as (adapter, manifest, generation):
        allowed = {doc:spec for doc,spec in manifest.items() if doc in scope}
        filters = request.filters.model_dump(exclude_none=True)
        if request.filters.section_id:
            with repo.sources.connect() as conn:
                rows = conn.execute('''SELECT id FROM retrieval_chunks WHERE
                    EXISTS(SELECT 1 FROM json_each(payload,'$.locator.section_ids') WHERE value=?)''',
                    (request.filters.section_id,)).fetchall()
                filters['chunk_ids'] = [r[0] for r in rows]
        hits = adapter.search(request.query, allowed, limit, filters)
        for hit in hits:
            spec = allowed[hit['document_id']]
            hit.update(index_generation=generation,builder_version=spec['builder_version'])
        return {'candidates':hits}


def exact(repo, scope, request, query, principal, deadline, occurrences=False):
    result = exact_search(repo, scope, query, principal=principal,
        continuation=request.continuations.get(repo.knowledge_base_id),
        binding_options={'filters':request.filters.model_dump(),'scopes':[s.model_dump() for s in request.scopes]},
        regex_mode=request.regex_mode,case_sensitive=request.case_sensitive,
        limit=request.budget.backend_candidates,max_chars=request.budget.exact_scan_chars,
        timeout=min(2,max(.01,deadline-time.monotonic())))
    candidates = []
    for hit in result['results']:
        base = dict(document_id=hit['document_id'],version_id=hit['version_id'],
                    builder_version=hit['builder_version'],matched_ranges=[{'start':hit['start'],'end':hit['end']}])
        if not occurrences and hit['chunk_ids']:
            candidates.extend({**base,'chunk_id':cid} for cid in hit['chunk_ids'][:5])
        else:
            candidates.append({**base,'start':hit['start'],'end':hit['end']})
    # Return verified occurrences later; do not return unchecked Exact context.
    result.pop('results')
    return {'candidates':candidates,'exact':result}


class DifyVectorAdapter:
    def __init__(self, client):
        self.client = client

    def _json(self, method, path, deadline, **kwargs):
        remaining = deadline-time.monotonic()
        if remaining<=0:
            raise BackendError('deadline')
        # No retries; finite response size and absolute deadline in addition to HTTP timeouts.
        with httpx.Client(timeout=min(remaining,5),follow_redirects=False) as client:
            with client.stream(method,self.client.base_url+path,headers=self.client.headers,**kwargs) as response:
                if response.status_code>=400:
                    raise BackendError(f'dify_http_{response.status_code}')
                if response.status_code>=300:
                    raise BackendError('dify_redirect_rejected')
                body = bytearray()
                for part in response.iter_bytes():
                    if time.monotonic()>deadline:
                        raise BackendError('deadline')
                    body.extend(part)
                    if len(body)>2_000_000:
                        raise BackendError('dify_response_budget')
                return json.loads(body)

    def search(self, query, limit, deadline):
        from app.clients.dify import DifyClient
        if not isinstance(self.client,DifyClient) or not self.client.api_key or not self.client.dataset_id:
            raise BackendError('vector_unconfigured_or_mock')
        if len(query)>250:
            raise BackendError('dify_query_exceeds_250_characters')
        path = '/datasets/'+self.client.dataset_id
        info = self._json('GET',path,deadline)
        if info.get('indexing_technique') != 'high_quality' or info.get('provider','vendor') != 'vendor':
            raise BackendError('semantic_capability_unverified')
        data = self._json('POST',path+'/retrieve',deadline,json={
            'query':query,'retrieval_model':{'search_method':'semantic_search','reranking_enable':False,
                'top_k':limit,'score_threshold_enabled':False}})
        if not isinstance(data.get('records'),list):
            raise BackendError('dify_invalid_response')
        # Dify retrieve has no pagination/document allowlist. Local scope is mandatory
        # after retrieval; filtered underfill is explicit, never filled with unrelated text.
        return {'candidates':[{'external':r,'dataset_id':self.client.dataset_id}
                              for r in data['records'][:limit]],
                'capability':'high_quality/semantic_search',
                'scope_filter':'local_verified_mapping_after_remote_top_k'}
