"""Upload clients are explicit contract fakes; real HTTP acceptance is a separate script."""
import json
import time
import pytest
from app.db import SourceRepository
from app.document_models import ExternalIndexMapping
from app.dify_mapping import SubmissionUncertain
from app.retrieval_v2.models import RetrievalRequest
from app.retrieval_v2.service import UnifiedRetrievalService
from app.retrieval_v2.access import AccessPolicy
from app.search.index_manager import IndexManager
from tests.test_document_repository import source
from tests.test_retrieval_v2 import FakeVector


@pytest.fixture
def mapped(tmp_path):
    repo=SourceRepository(tmp_path/'db','kb')
    source(repo,tmp_path/'files','# Report\n\nUnique fuel evidence.\n\nRepeated.\n\nRepeated.','s')
    repo.documents.backfill_all()
    return repo


def info(doc='remote',status='completed',file='file-v1'):
    return {'id':doc,'indexing_status':status,'enabled':True,'archived':False,'data_source_detail_dict':{'upload_file':{'id':file}}}


def submit(repo,doc='s',remote='remote'):
    version=repo.documents.version(doc)
    attempt=repo.documents.begin_dify_submission(doc,'ds',version.markdown.encode())
    repo.documents.acknowledge_dify_submission(doc,attempt.id,remote)
    repo.documents.observe_dify_status(doc,'ds',remote,info(remote))
    return attempt


def vector(repo,text='Unique fuel evidence.',segment='seg',remote='remote',**options):
    adapter=FakeVector([{'dataset_id':'ds','external':{'score':.8,'segment':{
        'document_id':remote,'id':segment,'enabled':True,'status':'completed','content':text}}}])
    service=UnifiedRetrievalService({'kb':repo.documents},AccessPolicy(lambda k:None,'user'),{'kb':adapter})
    return service.search(RetrievalRequest(query='fuel evidence',mode=options.pop('mode','vector'),
        scopes=[{'knowledge_base_id':'kb'}],**options))


def mapping(repo,attempt):
    return next(m for m in repo.documents.mappings('s',attempt.version_id) if m['id']==attempt.id)


def test_ingestion_receipt_matches_actual_submitted_bytes(ingestion,sample_txt):
    src,_=ingestion.ingest(sample_txt)
    repo=ingestion.repository.documents
    m=next(m for m in repo.mappings(src.source_id) if m.get('submission_id'))
    version=repo.version(src.source_id)
    assert m['binding_method']=='upload_ack' and m['status']=='completed'
    assert m['source_content_hash']==m['submitted_content_hash']==version.content_hash
    assert m['external_document_id']==src.dify_document_id
    assert m['dataset_id']==ingestion.dify.dataset_id


def test_repeated_upload_supersedes_only_previous_attempt(mapped):
    first=submit(mapped);second=submit(mapped)
    assert first.id!=second.id
    assert mapping(mapped,first)['status']=='superseded'
    assert mapping(mapped,second)['status']=='completed'
    assert len(vector(mapped).results)==1
    mapped.documents.acknowledge_dify_submission('s',second.id,'remote')
    assert len(mapped.documents.mappings('s'))==2


@pytest.mark.parametrize('status,expected',[('indexing','processing'),('waiting','processing'),('error','failed'),('completed','completed')])
def test_delayed_and_failed_indexing(mapped,status,expected):
    a=submit(mapped)
    mapped.documents.observe_dify_status('s','ds','remote',info(status=status))
    assert mapping(mapped,a)['status']==expected
    assert bool(vector(mapped).results)==(status=='completed')


def test_generic_status_cannot_bind_new_version(mapped):
    a=submit(mapped)
    mapped.save_revision(mapped.get('s'),'replacement current source','local','reparse')
    mapped.update('s',dify_status='success')
    assert not vector(mapped).results
    assert not any(m.get('binding_method')=='upload_ack' for m in mapped.documents.mappings('s'))
    assert mapping(mapped,a)['status']=='superseded'


def test_unknown_upload_outcome_blocks_repeated_create_and_recovers(mapped):
    version=mapped.documents.version('s')
    a=mapped.documents.begin_dify_submission('s','ds',version.markdown.encode())
    mapped.documents.mark_dify_uncertain(a.id)
    restarted=SourceRepository(mapped.db_path,'kb')
    with pytest.raises(SubmissionUncertain):restarted.documents.begin_dify_submission('s','ds',version.markdown.encode())
    with pytest.raises(ValueError):restarted.documents.reconcile_dify_submission('s',a.id,'remote',info(),b'wrong')
    restarted.documents.reconcile_dify_submission('s',a.id,'remote',info(),version.markdown.encode())
    assert vector(restarted).results


def test_conflicting_acknowledgment_and_document_id(mapped):
    a=submit(mapped)
    with pytest.raises(ValueError):mapped.documents.acknowledge_dify_submission('s',a.id,'wrong')
    assert not vector(mapped,remote='wrong').results
    mapped.documents.observe_dify_status('s','ds','remote',info(doc='wrong'))
    assert mapping(mapped,a)['status']=='failed'
    assert not vector(mapped).results


def test_concurrent_version_update_during_upload(mapped):
    version=mapped.documents.version('s')
    a=mapped.documents.begin_dify_submission('s','ds',version.markdown.encode())
    mapped.save_revision(mapped.get('s'),'new data','local','race')
    m=mapped.documents.acknowledge_dify_submission('s',a.id,'remote')
    assert m['status']=='superseded'
    assert mapped.get('s').dify_document_id is None
    assert not vector(mapped).results


def test_remote_original_revision_changed_never_repromotes(mapped):
    a=submit(mapped)
    for _ in range(2):mapped.documents.observe_dify_status('s','ds','remote',info(file='other-file'))
    assert mapping(mapped,a)['status']=='stale'
    assert not vector(mapped).results


def test_same_name_different_content_not_identity(mapped,tmp_path):
    submit(mapped)
    other=source(mapped,tmp_path/'other','different text','other')
    mapped.update('other',filename=mapped.get('s').filename)
    mapped.documents.backfill('other')
    attempt=mapped.documents.begin_dify_submission('other','ds',b'different text')
    with pytest.raises(ValueError):mapped.documents.acknowledge_dify_submission('other',attempt.id,'remote')
    assert vector(mapped).results[0].document_id=='s'


@pytest.mark.parametrize('text,valid',[('Unique fuel evidence.',True),('Repeated.',False),('rewritten fuel evidence',False)])
def test_segment_chunk_difference_and_unique_range(mapped,text,valid):
    submit(mapped)
    result=vector(mapped,text)
    assert bool(result.results)==valid
    if valid:
        hit=result.results[0];a=hit.matched_ranges[0]['start'];b=hit.matched_ranges[0]['end']
        assert mapped.documents.version('s').markdown[a:b]==text and hit.chunk_id!='seg'


def test_verified_explicit_range_can_disambiguate_repeated_text(mapped):
    attempt=submit(mapped);m=mapping(mapped,attempt)
    text=mapped.documents.version('s').markdown;start=text.index('Repeated.')
    m.update(id='explicit-seg',external_segment_id='seg',range_start=start,range_end=start+len('Repeated.'))
    mapped.documents.put_mapping('s',ExternalIndexMapping(**m))
    result=vector(mapped,'Repeated.')
    assert result.results and result.results[0].locator['start']==start
    m['range_end']+=1;mapped.documents.put_mapping('s',ExternalIndexMapping(**m))
    assert not vector(mapped,'Repeated.').results


def test_legacy_source_pointer_requires_remote_original_proof(mapped):
    mapped.update('s',dify_document_id='remote',dify_dataset_id='ds',dify_status='success')
    assert not vector(mapped).results
    version=mapped.documents.version('s')
    with pytest.raises(ValueError):mapped.documents.verify_legacy_dify_file('s','ds','remote',info(),b'not the source')
    for _ in range(2):mapped.documents.verify_legacy_dify_file('s','ds','remote',info(),version.markdown.encode())
    assert len([m for m in mapped.documents.mappings('s') if m.get('binding_method')=='remote_file_sha256'])==1
    assert vector(mapped).results


def test_hybrid_actual_two_adapters_contract_and_deletion(mapped):
    a=submit(mapped);IndexManager(mapped.documents).sync()
    result=vector(mapped,mode='hybrid')
    assert {'lexical','vector'}=={p['adapter'] for p in result.results[0].provenance}
    assert result.results[0].fused_score==pytest.approx(2/61)
    mapped.delete('s')
    assert not vector(mapped).results
    with mapped.connect() as conn:
        assert json.loads(conn.execute('SELECT payload FROM external_index_mappings WHERE id=?',(a.id,)).fetchone()[0])['status']=='deleted'


def test_content_budget_and_large_version_bound(mapped):
    submit(mapped)
    assert len(vector(mapped,budget={'context_chars':5}).results[0].content)==5
    with mapped.connect() as conn:
        conn.execute("UPDATE document_versions SET markdown=markdown || ?",('x'*4_000_001,))
    assert not vector(mapped).results


def test_submission_uses_immutable_bytes(mapped):
    with pytest.raises(ValueError):mapped.documents.begin_dify_submission('s','ds',b'new mutable file content')


def test_list_and_detail_original_file_shapes():
    from app.dify_mapping import remote_file_info
    assert remote_file_info({'data_source_detail_dict':{'upload_file':{'id':'file'}}})['id']=='file'
    assert remote_file_info({'data_source_info':{'upload_file':{'id':'file'}}})['id']=='file'
    assert remote_file_info({'data_source_info':'{"upload_file": {"id": "file"}}'})['id']=='file'
    assert remote_file_info({'data_source_info':'bad json'})=={}


def test_missing_remote_document_invalidates_receipt(mapped):
    a=submit(mapped)
    mapped.documents.observe_dify_status('s','ds','remote',None)
    assert mapping(mapped,a)['status']=='failed' and not vector(mapped).results


def test_explicit_chunk_mapping_must_equal_content(mapped):
    a=submit(mapped);m=mapping(mapped,a)
    chunk=mapped.documents.nodes('s','chunks')[0]
    m.update(id='segment-chunk',external_segment_id='seg',chunk_id=chunk['id'])
    mapped.documents.put_mapping('s',ExternalIndexMapping(**m))
    assert vector(mapped,chunk['content']).results
    assert not vector(mapped,'Unique fuel evidence.').results


def test_cross_chunk_vector_range_and_timeout(mapped):
    from app.retrieval_v2.evidence import text_window
    from app.chunk_builder import ChunkConfig
    text=' '.join(f'Unique sentence {i} has technical detail.' for i in range(50))
    mapped.save_revision(mapped.get('s'),text,'local','long')
    mapped.documents.rebuild_chunks('s',config=ChunkConfig(300,20))
    submit(mapped)
    segment=text[100:800]
    hit=vector(mapped,segment).results[0]
    assert hit.chunk_id is None and hit.locator['start']==100 and hit.locator['end']==800
    with mapped.connect() as conn:
        with pytest.raises(TimeoutError):text_window(conn,hit.version_id,0,300,time.monotonic()-1)


def test_ingestion_network_ambiguity_does_not_duplicate_upload(ingestion,sample_txt):
    calls=[]
    def lost(*args):calls.append(True);raise TimeoutError('lost upload response')
    ingestion.dify.upload_document=lost
    src,_=ingestion.ingest(sample_txt)
    assert src.dify_status=='failed'
    ingestion.retry_failed(src.source_id)
    assert len(calls)==1
    assert any(m['status']=='uncertain' for m in ingestion.repository.documents.mappings(src.source_id))


def test_wrong_dataset_never_maps(mapped):
    submit(mapped)
    adapter=FakeVector([{'dataset_id':'wrong-dataset','external':{'segment':{'id':'seg','document_id':'remote',
        'content':'Unique fuel evidence.','enabled':True,'status':'completed'}}}])
    service=UnifiedRetrievalService({'kb':mapped.documents},AccessPolicy(lambda k:None,'user'),{'kb':adapter})
    assert not service.search(RetrievalRequest(query='fuel',mode='vector',scopes=[{'knowledge_base_id':'kb'}])).results


@pytest.mark.parametrize('change',[{'enabled':False},{'archived':True}])
def test_parent_document_disabled_even_when_segments_completed(mapped,change):
    a=submit(mapped)
    mapped.documents.observe_dify_status('s','ds','remote',{**info(),**change})
    assert mapping(mapped,a)['status']=='disabled'
    assert not vector(mapped).results
    mapped.documents.observe_dify_status('s','ds','remote',info())
    assert vector(mapped).results


def test_legacy_file_proof_does_not_enable_remote_document(mapped):
    mapped.update('s',dify_document_id='remote',dify_dataset_id='ds',dify_status='success')
    receipt=mapped.documents.verify_legacy_dify_file('s','ds','remote',{**info(),'enabled':False},
                                                     mapped.documents.version('s').markdown.encode())
    assert receipt.status=='disabled' and not vector(mapped).results


def test_missing_visibility_is_not_sufficient_evidence(mapped):
    submit(mapped)
    body=info();body.pop('enabled');body.pop('archived')
    mapped.documents.observe_dify_status('s','ds','remote',body)
    assert not vector(mapped).results


@pytest.mark.parametrize('change',[{'dify_document_id':'different'}, {'dify_document_id':None}, {'dify_dataset_id':'different'}, {'dify_status':'failed'}])
def test_source_identity_or_failure_change_revokes_existing_receipt(mapped,change):
    a=submit(mapped)
    mapped.update('s',**change)
    assert mapping(mapped,a)['status'] in {'superseded','failed'}
    assert not vector(mapped).results
