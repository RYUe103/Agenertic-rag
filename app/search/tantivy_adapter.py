"""Real Tantivy BM25 with shared, versioned Python preprocessing."""
import tantivy
from .tokenization import tokens, identifiers

FIELDS = {'title':3.0,'identifiers':4.0,'section':2.0,'content':1.0}
IDENTITY_FIELDS = ('knowledge_base_id','document_id','version_id','chunk_id','builder_version','table_id','page','document_type')


class TantivyAdapter:
    def __init__(self,path,create=False):
        if create:
            schema = tantivy.SchemaBuilder()
            for name in IDENTITY_FIELDS:
                schema.add_text_field(name,stored=True,tokenizer_name='raw')
            for name in FIELDS:
                schema.add_text_field(name,tokenizer_name='rag_tokens')
            self.index = tantivy.Index(schema.build(),path=str(path))
        else:
            self.index = tantivy.Index.open(str(path))
        analyzer = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.whitespace()).build()
        self.index.register_tokenizer('rag_tokens',analyzer)
        self.schema = self.index.schema

    def update(self,repo,manifest,changed,removed):
        writer = self.index.writer(heap_size=32_000_000,num_threads=1)
        for document_id in sorted(changed|removed):
            writer.delete_documents_by_term('document_id',document_id)
        for document_id in sorted(changed):
            spec = manifest[document_id]
            meta = spec['metadata']
            title = ' '.join(str(meta.get(k) or '') for k in ('filename','standard_title','standard_no'))
            for chunk in repo.iter_chunks(document_id,spec['version_id'],spec['builder_version']):
                values = dict(knowledge_base_id=repo.knowledge_base_id,document_id=document_id,
                    version_id=spec['version_id'],chunk_id=chunk['id'],builder_version=spec['builder_version'],
                    title=' '.join(tokens(title)),section=' '.join(tokens(' '.join(chunk.get('section_titles',[]))+' '+(chunk.get('table_caption') or ''))),
                    content=' '.join(tokens(chunk['content'])),identifiers=identifiers(title+' '+chunk['content']),
                    document_type=str(meta.get('document_type') or ''),
                    table_id=chunk['locator'].get('table_ids',[]),
                    page=[str(p) for p in chunk['locator'].get('page_numbers',[])])
                writer.add_document(tantivy.Document.from_dict(values,self.schema))
        writer.commit()
        writer.wait_merging_threads()
        self.index.reload()

    def count(self):
        return self.index.searcher().num_docs

    def count_document(self,document_id):
        query = tantivy.Query.term_query(self.schema,'document_id',document_id)
        return self.index.searcher().search(query,limit=1).count

    def search(self,query,scope,top_k,filters):
        terms = list(dict.fromkeys(tokens(query)))
        if len(terms)>64:
            raise ValueError('Query exceeds 64 distinct normalized terms')
        if not terms or not scope:
            return []
        alternatives = []
        for name,boost in FIELDS.items():
            for term in terms:
                q = tantivy.Query.term_query(self.schema,name,term)
                alternatives.append((tantivy.Occur.Should,tantivy.Query.boost_query(q,boost)))
        clauses = [(tantivy.Occur.Must,tantivy.Query.boolean_query(alternatives)),
                   (tantivy.Occur.Must,tantivy.Query.term_set_query(self.schema,'document_id',list(scope)))]
        for name in ('page','table_id'):
            if filters.get(name) is not None:
                clauses.append((tantivy.Occur.Must,tantivy.Query.term_query(self.schema,name,str(filters[name]))))
        if 'chunk_ids' in filters:
            if not filters['chunk_ids']:
                return []
            clauses.append((tantivy.Occur.Must,tantivy.Query.term_set_query(self.schema,'chunk_id',filters['chunk_ids'])))
        searcher = self.index.searcher()
        result = searcher.search(tantivy.Query.boolean_query(clauses),limit=top_k)
        hits = []
        for score,address in result.hits:
            doc = searcher.doc(address).to_dict()
            hits.append(dict(document_id=doc['document_id'][0],version_id=doc['version_id'][0],
                chunk_id=doc['chunk_id'][0],score=score))
        return sorted(hits,key=lambda h:(-h['score'],h['chunk_id']))
