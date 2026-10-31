"""Durable upload receipts. An ambiguous network outcome is never silently retried."""
import hashlib
import json
import uuid
from .document_models import ExternalIndexMapping


class SubmissionUncertain(RuntimeError):
    pass


def remote_file_info(info):
    """Dify list and detail endpoints use different containers for the same file."""
    for key in ('data_source_detail_dict','data_source_info'):
        value=(info or {}).get(key) or {}
        if isinstance(value,str):
            try:value=json.loads(value)
            except ValueError:continue
        file=value.get('upload_file',{}) if isinstance(value,dict) else {}
        if isinstance(file,dict) and file.get('id'):return file
    return {}


class DifyMappingRepository:
    def begin_dify_submission(self, document_id, dataset_id, submitted: bytes):
        self.document(document_id)
        with self.sources.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('''SELECT v.* FROM documents d JOIN document_versions v ON v.id=d.current_version_id
                WHERE d.id=? AND d.deleted=0 AND NOT EXISTS(SELECT 1 FROM document_deletions WHERE document_id=d.id)''',
                (document_id,)).fetchone()
            if row is None: raise KeyError(document_id)
            digest=hashlib.sha256(submitted).hexdigest()
            if digest!=row['content_hash']:
                raise ValueError('Submission bytes must equal immutable Repository version')
            previous=[]
            for old in conn.execute('''SELECT m.id,m.payload FROM external_index_mappings m
                    JOIN document_versions v ON v.id=m.version_id WHERE v.document_id=?''',(document_id,)):
                value=json.loads(old['payload'])
                if value.get('adapter')!='dify':continue
                if value.get('status') in {'submitting','uncertain'}:
                    raise SubmissionUncertain('Upload receipt missing; reconcile remote identity before retry')
                previous.append((old['id'],value))
            # Invalidate earlier receipts BEFORE mutating a reused remote document.
            for ident,value in previous:
                value['status']='superseded'
                conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(value),ident))
            attempt=uuid.uuid4().hex
            mapping=ExternalIndexMapping(id=f'{row["id"]}:dify:{attempt}',version_id=row['id'],adapter='dify',
                index_version=row['id'],status='submitting',dataset_id=dataset_id,submission_id=attempt,
                source_content_hash=row['content_hash'],submitted_content_hash=digest)
            conn.execute('INSERT INTO external_index_mappings VALUES(?,?,?)',(mapping.id,mapping.version_id,mapping.model_dump_json()))
            return mapping

    def acknowledge_dify_submission(self, document_id, mapping_id, remote_document_id):
        if not remote_document_id:raise ValueError('Missing remote Document ID')
        with self.sources.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('''SELECT m.payload FROM external_index_mappings m JOIN document_versions v ON v.id=m.version_id
                WHERE m.id=? AND v.document_id=?''',(mapping_id,document_id)).fetchone()
            if row is None:raise KeyError(mapping_id)
            m=json.loads(row[0])
            if m.get('external_document_id'):
                if m['external_document_id']!=remote_document_id:raise ValueError('Conflicting upload receipt')
                return m
            if m['status'] not in {'submitting','uncertain'}:raise ValueError('Submission no longer active')
            current=conn.execute('''SELECT current_version_id FROM documents WHERE id=? AND deleted=0
                AND EXISTS(SELECT 1 FROM sources WHERE source_id=documents.id)
                AND NOT EXISTS(SELECT 1 FROM document_deletions WHERE document_id=documents.id)''',(document_id,)).fetchone()
            active=bool(current and current[0]==m['version_id'])
            # A remote ID cannot simultaneously acknowledge two local identities.
            for other in conn.execute('''SELECT m.id,m.payload,v.document_id FROM external_index_mappings m
                    JOIN document_versions v ON v.id=m.version_id WHERE m.id!=?''',(mapping_id,)):
                value=json.loads(other['payload'])
                if (value.get('adapter')=='dify' and value.get('dataset_id')==m['dataset_id'] and
                    value.get('external_document_id')==remote_document_id and value.get('status') not in {'superseded','failed'}):
                    if other['document_id']!=document_id:
                        raise ValueError('Remote Document ID already belongs to another local document')
                    value['status']='superseded'
                    conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(value),other['id']))
            m.update(external_document_id=remote_document_id,binding_method='upload_ack',
                     status='processing' if active else 'superseded',remote_status='pending')
            conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(m),mapping_id))
            if active:
                conn.execute("UPDATE sources SET dify_dataset_id=?,dify_document_id=?,dify_status='processing' WHERE source_id=?",
                             (m['dataset_id'],remote_document_id,document_id))
            return m

    def mark_dify_uncertain(self, mapping_id):
        with self.sources.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT payload FROM external_index_mappings WHERE id=?',(mapping_id,)).fetchone()
            if row:
                value=json.loads(row[0])
                if value['status']=='submitting':
                    value['status']='uncertain'
                    conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(value),mapping_id))

    def reconcile_dify_submission(self, document_id, mapping_id, remote_document_id, info, original_bytes):
        """Recover a lost upload receipt after authenticated read-only original-file verification."""
        with self.sources.connect() as conn:
            row=conn.execute('''SELECT m.payload FROM external_index_mappings m JOIN document_versions v ON v.id=m.version_id
                WHERE m.id=? AND v.document_id=?''',(mapping_id,document_id)).fetchone()
        if not row:raise KeyError(mapping_id)
        m=json.loads(row[0])
        if (hashlib.sha256(original_bytes).hexdigest()!=m.get('submitted_content_hash') or
            not info or info.get('id')!=remote_document_id or info.get('indexing_status')!='completed' or
            not remote_file_info(info).get('id')):
            raise ValueError('Recovery requires exact original bytes and completed remote identity')
        self.acknowledge_dify_submission(document_id,mapping_id,remote_document_id)
        return self.observe_dify_status(document_id,m['dataset_id'],remote_document_id,info)

    def observe_dify_status(self, document_id, dataset_id, remote_document_id, info):
        """Status-only refresh cannot invent a version receipt or promote legacy mappings."""
        with self.sources.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current=conn.execute('''SELECT d.current_version_id FROM documents d JOIN sources s ON s.source_id=d.id
                WHERE d.id=? AND d.deleted=0 AND s.dify_document_id=? AND s.dify_dataset_id=?
                AND NOT EXISTS(SELECT 1 FROM document_deletions WHERE document_id=d.id)''',
                (document_id,remote_document_id,dataset_id)).fetchone()
            if not current:raise KeyError(document_id)
            remote_status=str((info or {}).get('indexing_status','missing'))
            identity_ok=bool(info and info.get('id')==remote_document_id)
            state=('completed' if remote_status=='completed' else 'failed' if remote_status in {'error','missing'} else 'processing') if identity_ok else 'failed'
            if state=='completed' and (info.get('enabled') is False or info.get('archived') is True):state='disabled'
            for row in conn.execute('SELECT id,payload FROM external_index_mappings WHERE version_id=?',(current[0],)):
                m=json.loads(row['payload'])
                if (m.get('adapter')=='dify' and m.get('dataset_id')==dataset_id and
                    m.get('external_document_id')==remote_document_id and m.get('binding_method') in {'upload_ack','remote_file_sha256'}
                    and m.get('status') not in {'superseded','uncertain','submitting','stale','deleted'}):
                    revision=remote_file_info(info).get('id')
                    receipt_state='stale' if identity_ok and m.get('remote_revision') and revision!=m['remote_revision'] else state
                    m.update(status=receipt_state,remote_status=remote_status,remote_revision=revision,
                             remote_enabled=(info or {}).get('enabled'),remote_archived=(info or {}).get('archived'))
                    conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(m),row['id']))
            conn.execute('UPDATE sources SET dify_status=? WHERE source_id=?',
                         ({'completed':'success','disabled':'success','failed':'failed','processing':'processing'}[state],document_id))
        return self.sources.get(document_id)

    def verify_legacy_dify_file(self, document_id, dataset_id, remote_document_id, info, original_bytes):
        """Explicit reconciliation with downloaded remote ORIGINAL bytes, never segment similarity.

        Caller supplies a read-only authenticated download and must enforce its byte budget.
        This is opt-in; production migration is not performed by normal reads/startup.
        """
        version=self.version(document_id)
        source=self.sources.get(document_id)
        if source.dify_dataset_id!=dataset_id or source.dify_document_id!=remote_document_id:
            raise ValueError('Remote identity differs from persisted upload identity')
        if not info or info.get('id')!=remote_document_id or info.get('indexing_status')!='completed':
            raise ValueError('Remote document not completed')
        digest=hashlib.sha256(original_bytes).hexdigest()
        if digest!=version.content_hash:raise ValueError('Remote original bytes differ from current Repository version')
        file_info=remote_file_info(info)
        if not file_info.get('id'):raise ValueError('Missing remote original file identity')
        with self.sources.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            live=conn.execute('''SELECT d.current_version_id,s.dify_document_id,s.dify_dataset_id FROM documents d
                JOIN sources s ON s.source_id=d.id WHERE d.id=? AND d.deleted=0
                AND NOT EXISTS(SELECT 1 FROM document_deletions WHERE document_id=d.id)''',(document_id,)).fetchone()
            if not live or tuple(live)!=(version.id,remote_document_id,dataset_id):raise ValueError('Concurrent source change')
            for old in conn.execute('''SELECT m.id,m.version_id,m.payload,v.document_id FROM external_index_mappings m
                    JOIN document_versions v ON v.id=m.version_id'''):
                value=json.loads(old['payload'])
                if value.get('adapter')=='dify' and value.get('dataset_id')==dataset_id and value.get('external_document_id')==remote_document_id:
                    if old['document_id']!=document_id and value.get('status') not in {'deleted','superseded'}:
                        raise ValueError('Remote identity belongs to another document')
                    if old['version_id']!=version.id:
                        value['status']='superseded'
                        conn.execute('UPDATE external_index_mappings SET payload=? WHERE id=?',(json.dumps(value),old['id']))
            mapping=ExternalIndexMapping(id=f'{version.id}:dify:verified-file:{digest}',version_id=version.id,
                adapter='dify',index_version=version.id,
                status='completed' if info.get('enabled') is True and info.get('archived') is False else 'disabled',
                external_document_id=remote_document_id,
                dataset_id=dataset_id,binding_method='remote_file_sha256',source_content_hash=digest,
                submitted_content_hash=digest,remote_revision=file_info['id'],remote_status='completed',
                remote_enabled=info.get('enabled'),remote_archived=info.get('archived'))
            conn.execute('INSERT OR REPLACE INTO external_index_mappings VALUES(?,?,?)',(mapping.id,version.id,mapping.model_dump_json()))
        return mapping
