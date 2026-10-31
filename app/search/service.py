"""Authorization is resolved before candidate generation and rechecked before return."""
from .index_manager import IndexManager
from .exact_search import exact_search
from .tokenization import positions


class LocalSearchService:
    def __init__(self, repository, authorize):
        self.repo = repository
        self.authorize = authorize
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
        adapter,manifest,generation = self.manager.open_current()
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
        return {'generation_id':generation,'results':results,'authorized_documents':len(scope)}

    def exact(self, query, document_ids=None, versions=None, **kwargs):
        # Exact scan does not need a healthy Tantivy index.
        manifest = {}
        for doc in self.repo.index_manifest():
            if doc['deleted'] or not doc['current_version_id']:
                continue
            if document_ids is not None and doc['id'] not in document_ids:
                continue
            try:
                self.authorize(self.repo.knowledge_base_id,doc['id'])
                vid = self.repo.resolve_version_id(doc['id'],(versions or {}).get(doc['id']))
            except (PermissionError,KeyError):
                continue
            builder = self.repo.selected_builder(vid)
            manifest[doc['id']] = dict(version_id=vid,builder_version=builder,metadata={})
            if len(manifest)>100:
                raise ValueError('Exact scan requires a scope of at most 100 documents')
        result = exact_search(self.repo,manifest,query,**kwargs)
        for hit in result['results']:
            self.authorize(self.repo.knowledge_base_id,hit['document_id'])
            self.repo.document(hit['document_id'])
        return result
