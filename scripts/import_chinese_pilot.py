"""One-time OFFLINE audit import of the five already-completed initial pilot calls.

Preserves original artifacts. Does not re-run questions or pretend the preliminary
10-file corpus is identical to the corrected independent-passage 50-question pack.
"""
import argparse
import json
import sqlite3
from pathlib import Path
from scripts.chinese_eval.common import sha,write,read_rows
from scripts.chinese_eval.trace import checkpoint_trace
from scripts.chinese_eval.scoring import VERSION
from scripts.evaluate_chinese_agent import KB
from app.db import SourceRepository
from app.search.index_manager import IndexManager


def import_pilot(source,output,rgb_dir):
    if (output/'manifest.json').exists():raise ValueError('import_already_exists')
    original=json.loads((source/'manifest.json').read_text());output.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect('file:'+str((source/'sources.db').resolve())+'?mode=ro',uri=True) as src:
        with sqlite3.connect(output/'sources.db') as dst:src.backup(dst)
    repo=SourceRepository(output/'sources.db',KB)
    corpus=[];texts={}
    for item in original['corpus']:
        text=repo.documents.version(item['document_id']).markdown
        assert sha(text)==item['text_sha256']
        texts[item['document_id']]=text
        corpus.append(dict(item,sha256=sha(text)))
    # Build a separate local index from exactly the original text, no Agent requests.
    manager=IndexManager(repo.documents);manager.sync();generation=manager.current()
    lookup={str(r['id']):r for r in read_rows(rgb_dir/'zh_refine.json')}
    questions=[];raw_hashes={}
    for old in original['questions'][:5]:
        qid=old['question_id'];raw=lookup[str(old['source_id'])];gold=[]
        assert old['question']==raw['query'] and old['reference_answer']==raw['answer']
        for i,positive in enumerate(raw['positive']):
            for doc,text in texts.items():
                offset=0
                while True:
                    start=text.find(positive,offset)
                    if start<0:break
                    gold.append(dict(document_id=doc,start=start,end=start+len(positive),evidence_id='rgb_passage_'+sha(positive),
                        text_sha256=sha(positive),source_file='zh_refine.json',source_record_id=raw['id'],field='positive',index_path=[i],group=0))
                    offset=start+len(positive)
        questions.append(dict(old,gold_evidence=gold,gold_document_ids=sorted({e['document_id'] for e in gold})))
        path=source/(qid+'-private.json');value=json.loads(path.read_text());raw_hashes[path.name]=sha(path.read_bytes())
        result=value['result'];trace=checkpoint_trace(source/'execution',result)
        artifact=dict(question_id=qid,result=result,trace=trace,generation_status='completed',original_artifact_sha256=raw_hashes[path.name])
        write(output/'cases'/(qid+'.json'),artifact)
    manifest=dict(format_version=2,execution_kind='preliminary_pilot_offline_audit_only',corpus=corpus,questions=questions,
        tracked_agent_code_sha=original['code_sha'],evaluation_script_identity='Initial uncommitted script was not hashed before execution; cannot claim exact evaluator source reproduction.',
        original_manifest_sha256=sha((source/'manifest.json').read_bytes()),original_artifacts=raw_hashes,
        configuration=dict(model=original['model'],api_style=original['api_style'],budget=original['budget']),
        knowledge_base_id=KB,index_generation=generation['id'],index_manifest_sha256=sha(generation['manifest']),scorer_version=VERSION,
        limitations=['Five real calls only; no rerun.', 'Initial ten synthetic bundles differ from corrected 50-question corpus.',
                    'Integration passages in preliminary bundles used Python list representation; none of the five evaluated questions are integration questions.',
                    'Initial substring-based grades and final-citation-as-retrieval metrics were invalid and superseded by this offline audit.',
                    'No end-to-end claim for corrected corpus; full 50 remains prepared only.'])
    write(output/'manifest.json',manifest)
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--rgb-dir',type=Path,required=True)
    a=p.parse_args();m=import_pilot(a.source,a.output,a.rgb_dir);print(len(m['questions']))
