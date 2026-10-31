"""Authorization is resolved before candidate generation and rechecked before return."""
from .index_manager import IndexManager
from .exact_search import exact_search
from .tokenization import positions
from .snapshot import snapshot
from .exact_search import ContinuationInvalid
import secrets
import time


class LocalSearchService:
    def __init__(self, repository, authorize, principal=None):
        self.repo = repository
        self.authorize = authorize
        self.principal = principal or secrets.token_hex(24)
        manager = getattr(repository,'_local_index_manager',None)
        if manager is None:
            repository._local_index_manager = manager = IndexManager(repository)
        self.manager = manager

    def scope(self, manifest, document_ids=None, filters=None):
        filters = filters or {}
        allowed_filters = {'document_type','page','table_id','metadata'}
        if set(filters)-allowed_filters:
            raise ValueError('Unsupported filter')
        if not isinstance(filters.get('metadata',{}),dict):
            raise ValueError('Metadata filters must be an object')
        if 'page' in filters and (not isinstance(filters['page'],int) or isinstance(filters['page'],bool) or filters['page']<1):
            raise ValueError('Page must be a positive integer')
        if 'table_id' in filters and not isinstance(filters['table_id'],str):
            raise ValueError('Table ID must be a string')
        if document_ids is not None and len(document_ids)>1000:
            raise ValueError('At most 1000 explicit documents')
        wanted = set(document_ids) if document_ids is not None else None
        scopes = {}
        for document_id, spec in manifest.items():
            if wanted is not None and document_id not in wanted:
                continue
            try:
                self.authorize(self.repo.knowledge_base_id,document_id)
                self.repo.document(document_id)
            except (PermissionError,KeyError):
                continue
            metadata = spec['metadata']
            if filters.get('document_type') and metadata.get('document_type') != filters['document_type']:
                continue
            if any(metadata.get(k)!=v for k,v in filters.get('metadata',{}).items()):
                continue
            scopes[document_id] = spec
        return scopes

    def search(self, query, top_k=10, document_ids=None, filters=None):
        if not query.strip() or len(query)>512:
            raise ValueError('Query must contain 1..512 characters')
        with self.manager.reader() as opened:
            return self._search(opened, query, top_k, document_ids, filters)

    def _search(self, opened, query, top_k, document_ids, filters):
        adapter,manifest,generation = opened
        scope = self.scope(manifest,document_ids,filters)
        hits = adapter.search(query,scope,min(max(top_k,1),100),filters or {})
        results = []
        for hit in hits:
            document_id = hit['document_id']
            # A concurrent revocation fails the whole request; never silently underfill Top-K.
            self.authorize(self.repo.knowledge_base_id,document_id)
            chunk = self.repo.node(document_id,hit['chunk_id'],hit['version_id'])
            spec = scope[document_id]
            if hit['version_id'] != spec['version_id'] or chunk['builder_version'] != spec['builder_version']:
                raise RuntimeError('Index version mismatch; rebuild required')
            cell_matches=[]
            if chunk.get('table_id'):
                table=self.repo.node(document_id,chunk['table_id'],hit['version_id'])
                selected=set(chunk.get('cell_ids',[]))
                for cell in table['cells']:
                    matches=positions(cell['content'],query)
                    if cell['id'] in selected and matches:
                        cell_matches.append({'cell_id':cell['id'],'row':cell['row'],'column':cell['column'],
                            'row_span':cell['row_span'],'column_span':cell['column_span'],
                            'bbox':cell.get('bbox'),'matches':matches})
                        if len(cell_matches)>=100:
                            break
            results.append({**hit,'knowledge_base_id':self.repo.knowledge_base_id,
                'content':chunk['content'][:12000], 'content_truncated':len(chunk['content'])>12000,
                'locator':chunk['locator'],'cell_ids':chunk.get('cell_ids',[])[:200],
                'cell_matches':cell_matches,
                'matches':positions(chunk['content'],query),
                'match_coordinate':'chunk_projection' if chunk.get('content_is_projection') else 'chunk_text',
                'builder_version':chunk['builder_version']})
        latest, dirty = self.manager._snapshot()
        for hit in results:
            if hit['document_id'] not in latest['documents']:
                raise RuntimeError('Document removed during search; retry')
            self.authorize(self.repo.knowledge_base_id, hit['document_id'])
        return {'generation_id':generation,'results':results,'authorized_documents':len(scope),
                'stale':manifest != latest['documents'] or bool(dirty)}

    def _exact_scope(self, document_ids, versions, filters):
        if set(filters or {})-{'document_type', 'metadata'}:
            raise ValueError('Exact filters support document_type and metadata')
        with self.repo.sources.connect() as conn:
            conn.execute('BEGIN')
            manifest = snapshot(conn, self.repo.knowledge_base_id, bounded_scope=True, document_ids=document_ids)['documents']
        wanted = set(document_ids) if document_ids is not None else None
        allowed = {}
        for doc, spec in manifest.items():
            if wanted is not None and doc not in wanted:
                continue
            try:
                self.authorize(self.repo.knowledge_base_id, doc)
            except (PermissionError,KeyError):
                continue
            meta = spec['metadata']
            if filters and filters.get('document_type') and meta['document_type'] != filters['document_type']:
                continue
            if any(meta.get(k) != v for k, v in (filters or {}).get('metadata', {}).items()):
                continue
            if versions and doc in versions:
                vid = self.repo.resolve_version_id(doc, versions[doc])
                spec = {**spec, 'version_id':vid, 'builder_version':self.repo.selected_builder(vid)}
            allowed[doc] = spec
        return allowed

    def exact(self, query, document_ids=None, versions=None, filters=None, **kwargs):
        # Scope preparation is reported separately from the bounded scan budget.
        if not isinstance((filters or {}).get('metadata', {}), dict):
            raise ValueError('Metadata filters must be an object')
        started = time.monotonic()
        manifest = self._exact_scope(document_ids, versions, filters)
        prepared = time.monotonic()-started
        result = exact_search(self.repo, manifest, query, principal=self.principal,
            binding_options=dict(document_ids=sorted(set(document_ids)) if document_ids is not None else None,
                                 versions=versions or {}, filters=filters or {}), **kwargs)
        # Catch concurrent mutation/revocation, including changes to non-hit scope.
        if self._exact_scope(document_ids, versions, filters) != manifest:
            raise ContinuationInvalid('Scope or permissions changed during scan; restart search')
        for hit in result['results']:
            self.authorize(self.repo.knowledge_base_id,hit['document_id'])
            self.repo.document(hit['document_id'])
        result['scope_seconds'] = prepared
        result['elapsed_seconds'] = time.monotonic()-started
        return result
