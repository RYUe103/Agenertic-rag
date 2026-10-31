"""Only Repository bytes and structured locators can become trusted evidence."""
import codecs
import json
import time
from collections import Counter
from .models import Evidence


class VerificationError(ValueError):
    pass


def text_window(conn, vid, start, limit, deadline=None):
    """Bounded Unicode window over SQLite BLOB IO, including embedded NULs."""
    row = conn.execute('SELECT rowid FROM document_versions WHERE id=?',(vid,)).fetchone()
    if row is None:
        raise KeyError(vid)
    decoder = codecs.getincrementaldecoder('utf-8')()
    position, result, finished = 0, [], False
    with conn.blobopen('document_versions','markdown',row[0],readonly=True) as blob:
        while blob.tell()<len(blob):
            if deadline is not None and time.monotonic()>=deadline:
                raise TimeoutError('Source read deadline')
            data = decoder.decode(blob.read(65536), final=blob.tell()==len(blob))
            low,high = max(0,start-position),min(len(data),start+limit-position)
            if high>low:
                result.append(data[low:high])
            position += len(data)
            if position>=start+limit:
                finished = blob.tell()==len(blob) and position==start+limit
                break
        else:
            finished=True
    content = ''.join(result)
    return content, finished


class EvidenceResolver:
    def __init__(self, repo, scope, request, deadline):
        self.repo,self.scope,self.request,self.deadline = repo,scope,request,deadline
        self.rejected = Counter()

    def resolve(self, candidates, adapter):
        output = []
        with self.repo.sources.connect() as conn:
            conn.execute('BEGIN')
            mappings = None
            for rank,candidate in enumerate(candidates,1):
                if time.monotonic()>=self.deadline:
                    self.rejected['deadline']+=1
                    break
                try:
                    if adapter=='vector':
                        if mappings is None:
                            mappings = [dict(row) for row in conn.execute('''SELECT m.payload,v.document_id
                                FROM external_index_mappings m JOIN document_versions v ON v.id=m.version_id''')]
                        candidate = self._external(conn,candidate,mappings)
                    item = self._local(conn,candidate,adapter,rank)
                    if item:
                        output.append(item)
                except (KeyError,ValueError,TypeError,TimeoutError) as exc:
                    self.rejected['unverified_external' if adapter=='vector' else 'unavailable']+=1
                    if isinstance(exc,VerificationError):
                        self.rejected['external_'+str(exc)]+=1
        return output

    def _external(self, conn, candidate, mappings):
        record = candidate['external']
        seg = record.get('segment') or {}
        ext_doc = seg.get('document_id') or (seg.get('document') or {}).get('id')
        nested = (seg.get('document') or {}).get('id')
        if not ext_doc or not seg.get('id') or (nested and nested!=ext_doc):
            raise VerificationError('missing_or_conflicting_source_id')
        if seg.get('enabled') is not True or seg.get('status')!='completed' or seg.get('error'):
            raise VerificationError('segment_not_ready')
        matches = []
        for row in mappings:
            doc = row['document_id']
            if doc not in self.scope:
                continue
            mapping = json.loads(row['payload'])
            if (mapping.get('adapter')=='dify' and mapping.get('dataset_id')==candidate['dataset_id']
                and mapping.get('external_document_id')==ext_doc
                and mapping.get('status') in {'completed','ready','indexed','success'}
                and mapping.get('version_id')==self.scope[doc]['version_id']):
                if mapping.get('external_segment_id') not in (None,seg['id']):
                    continue
                matches.append((doc,mapping))
        identities = {(doc,m['version_id']) for doc,m in matches}
        if len(identities)!=1:
            raise VerificationError('ambiguous_or_missing_version_mapping')
        doc,vid = next(iter(identities))
        content = seg.get('content')
        if not isinstance(content,str) or not content.strip() or len(content)>12000:
            raise VerificationError('invalid_segment_content')
        explicit = {m['chunk_id'] for _,m in matches if m.get('chunk_id') and m.get('external_segment_id')==seg['id']}
        base = dict(document_id=doc,version_id=vid,score=record.get('score'))
        if len(explicit)==1:
            cid = next(iter(explicit))
            row = conn.execute('SELECT payload FROM retrieval_chunks WHERE id=? AND version_id=?',(cid,vid)).fetchone()
            if row and json.loads(row[0])['content']==content:
                return {**base,'chunk_id':cid}
            raise VerificationError('explicit_mapping_content_mismatch')
        if self.scope[doc]['markdown_bytes']>4_000_000:
            raise VerificationError('verification_budget_exceeded')
        text,_ = text_window(conn,vid,0,4_000_001,self.deadline)
        start = text.find(content)
        if start<0 or text.find(content,start+1)>=0:
            raise VerificationError('text_not_unique_or_absent')
        end = start+len(content)
        # Segment may be only part of a chunk; use the smallest containing unit,
        # but return Repository content, preserving the exact matched range.
        rows = conn.execute('''SELECT id FROM retrieval_chunks WHERE version_id=? AND builder_version=?
            AND start<=? AND end>=? ORDER BY end-start,id LIMIT 2''',
            (vid,self.scope[doc]['builder_version'],start,end)).fetchall()
        return {**base,'start':start,'end':end,'chunk_id':rows[0][0] if rows else None,
                'matched_ranges':[dict(start=start,end=end)]}

    def _local(self, conn, candidate, adapter, rank):
        doc,vid = candidate['document_id'],candidate['version_id']
        spec = self.scope.get(doc)
        if spec is None:
            self.rejected['unavailable']+=1
            return None
        if vid!=spec['version_id'] or candidate.get('builder_version',spec['builder_version'])!=spec['builder_version']:
            self.rejected['stale']+=1
            return None
        cid = candidate.get('chunk_id')
        projection=False
        if cid:
            row=conn.execute('SELECT payload FROM retrieval_chunks WHERE id=? AND version_id=? AND builder_version=?',
                             (cid,vid,spec['builder_version'])).fetchone()
            if row is None:
                raise KeyError(cid)
            chunk = json.loads(row[0])
            locator,content = chunk['locator'],chunk['content']
            self._validate_locator(conn,vid,locator)
            projection = chunk.get('content_is_projection',False)
            if not projection:
                a,b=locator.get('start'),locator.get('end')
                if a is None or b is None or b<a or b-a>100000:
                    raise ValueError('Invalid chunk range')
                original,_ = text_window(conn,vid,a,b-a,self.deadline)
                if original!=content:
                    raise ValueError('Chunk/raw text mismatch')
        else:
            a,b=candidate['start'],candidate['end']
            if a<0 or b<=a:
                raise ValueError('Invalid range')
            content,_=text_window(conn,vid,a,min(b-a,self.request.budget.item_chars),self.deadline)
            if not content:
                raise ValueError('Range outside source')
            locator=dict(version_id=vid,start=a,end=b,block_ids=[],page_numbers=[],section_ids=[],table_ids=[])
            for row in conn.execute("SELECT payload FROM document_nodes WHERE version_id=? AND kind='blocks' AND json_extract(payload,'$.locator.start')<? AND json_extract(payload,'$.locator.end')>?",(vid,b,a)):
                block=json.loads(row[0])
                locator['block_ids'].append(block['id'])
                for name in ('page_numbers','section_ids','table_ids'):
                    locator[name]=sorted(set(locator[name]+block['locator'].get(name,[])))
        f=self.request.filters
        if (f.page is not None and f.page not in locator.get('page_numbers',[]) or
            f.section_id and f.section_id not in locator.get('section_ids',[]) or
            f.table_id and f.table_id not in locator.get('table_ids',[])):
            self.rejected['location_filter']+=1
            return None
        limit=self.request.budget.item_chars
        return Evidence(knowledge_base_id=self.repo.knowledge_base_id,document_id=doc,version_id=vid,
            document_name=spec['metadata'].get('filename',''),chunk_id=cid,source_adapter=adapter,
            source_rank=rank,source_score=candidate.get('score'),content=content[:limit],
            content_truncated=len(content)>limit or (not cid and b-a>limit),content_is_projection=projection,
            locator=locator,page_numbers=locator.get('page_numbers',[]),section_ids=locator.get('section_ids',[]),
            table_ids=locator.get('table_ids',[]),matched_ranges=candidate.get('matched_ranges',[]),
            index_generation=candidate.get('index_generation'),
            verification_status='verified_current' if vid==spec['current_version_id'] else 'verified_historical')

    def _validate_locator(self, conn, vid, locator):
        if locator.get('version_id')!=vid:
            raise ValueError('Locator version mismatch')
        blocks=locator.get('block_ids',[])
        if not blocks or len(blocks)>500:
            raise ValueError('Missing or excessive source blocks')
        marks=','.join('?' for _ in blocks)
        rows=conn.execute(f"SELECT id,payload FROM document_nodes WHERE version_id=? AND kind='blocks' AND id IN ({marks})",
                          (vid,*blocks)).fetchall()
        if {r['id'] for r in rows}!=set(blocks):
            raise ValueError('Block ownership mismatch')
        locators=[json.loads(r['payload'])['locator'] for r in rows]
        for key in ('page_numbers','section_ids','table_ids'):
            known={value for loc in locators for value in loc.get(key,[])}
            if not set(locator.get(key,[]))<=known:
                raise ValueError('Unverified source geometry')
        for key,kind in [('section_ids','sections'),('table_ids','tables')]:
            ids=locator.get(key,[])
            if ids:
                marks=','.join('?' for _ in ids)
                count=conn.execute(f'SELECT count(*) FROM document_nodes WHERE version_id=? AND kind=? AND id IN ({marks})',
                                   (vid,kind,*ids)).fetchone()[0]
                if count!=len(set(ids)):
                    raise ValueError('Structure ownership mismatch')
