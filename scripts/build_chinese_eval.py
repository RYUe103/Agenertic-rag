"""Pinned RGB -> one KB, one independent source passage per content-addressed file."""
import argparse
import json
import urllib.request
from collections import Counter
from pathlib import Path

from scripts.chinese_eval.common import sha, write, read_rows

RGB_REV = '65ec39e40e7dc9abb50e9bf1b4f32be3f6f16615'
SOURCE = 'https://github.com/chen700564/RGB'
FILES = ('zh_refine.json', 'zh_int.json', 'zh_fact.json')


def passages(value, prefix=()):
    """Nested integration groups remain identifiable; never stringify Python lists."""
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from passages(item, (*prefix, i))
    else:
        raise ValueError('unexpected_passage_type')


def acquire(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in (*FILES, 'LICENSE.txt', 'readme.md', 'evalue.py'):
        path = directory/name
        remote = 'data/'+name if name in FILES else name
        with urllib.request.urlopen(f'https://raw.githubusercontent.com/chen700564/RGB/{RGB_REV}/{remote}', timeout=60) as response:
            data = response.read(15_000_001)
        if len(data) > 15_000_000:
            raise ValueError('download_budget_exceeded')
        path.write_bytes(data)


def build(output, rgb_dir, source_revision=RGB_REV):
    if (output/'manifest.json').exists():
        raise ValueError('immutable_pack_exists_use_new_directory')
    output.mkdir(parents=True, exist_ok=True)
    documents, questions, mappings = {}, [], []
    counts = (20, 15, 15)
    for filename, count, task in zip(FILES, counts, ('factual_with_noise', 'information_integration', 'counterfactual')):
        for row in read_rows(rgb_dir/filename)[:count]:
            qid = f'rgb_zh_{len(questions)+1:04d}'
            gold, roles = [], {'positive': [], 'negative': [], 'positive_wrong': []}
            for role in roles:
                for path, text in passages(row.get(role, [])):
                    if not text.strip():
                        continue
                    digest = sha(text)
                    doc = 'rgb_doc_'+digest[:24]
                    name = f'files/{doc}.txt'
                    documents.setdefault(doc, {'document_id': doc, 'filename': name, 'sha256': digest, 'bytes': len(text.encode())})
                    target = output/name
                    target.parent.mkdir(exist_ok=True)
                    if not target.exists():
                        target.write_text(text)
                    record = dict(question_id=qid, source_file=filename, source_record_id=row['id'], field=role,
                                  index_path=list(path), document_id=doc, start=0, end=len(text), text_sha256=digest)
                    mappings.append(record)
                    roles[role].append(doc)
                    if role == 'positive':
                        gold.append(dict(record, evidence_id='rgb_passage_'+digest,
                                         group=path[0] if filename=='zh_int.json' else 0))
            questions.append(dict(question_id=qid, dataset='RGB', source_dataset=filename, source_id=row['id'],
                task_type=task, question=row['query'], reference_answer=row['answer'],
                fake_answer=row.get('fakeanswer'), gold_document_ids=sorted(set(roles['positive'])), gold_evidence=gold,
                document_roles={k:sorted(set(v)) for k,v in roles.items()}, answerable=True,
                metadata=dict(source_url=f'{SOURCE}/blob/{source_revision}/data/{filename}',
                              source_row_sha256=sha(json.dumps(row, ensure_ascii=False, sort_keys=True)),
                              label_scope='dataset labels; negatives are not necessarily false; positives are alternative evidence')))
    (output/'questions.jsonl').write_text(''.join(json.dumps(q, ensure_ascii=False)+'\n' for q in questions))
    write(output/'passage_mapping.json', mappings)
    (output/'LICENSE.txt').write_bytes((rgb_dir/'LICENSE.txt').read_bytes())
    (output/'ATTRIBUTION.md').write_text(f'# RGB derived evaluation subset\n\nSource: {SOURCE}\nRevision: {source_revision}\nLicense: CC BY-NC-SA 4.0 (see LICENSE.txt).\n\nSelection: first 20 refined, 15 integration, 15 counterfactual Chinese records in source order. Original questions and answers are unchanged. Each distinct source passage becomes one TXT file. Repeated passages share a file; all origins remain in passage_mapping.json. No answer labels enter searchable file content. This corpus-pooling protocol differs from RGB supplied-context evaluation.\n')
    files = {str(p.relative_to(output)):sha(p.read_bytes()) for p in sorted(output.rglob('*')) if p.is_file()}
    manifest = dict(format_version=2, dataset='RGB', source_revision=source_revision, source_url=SOURCE,
        source_files={name:dict(sha256=sha((rgb_dir/name).read_bytes()),bytes=(rgb_dir/name).stat().st_size,
                               records=len(read_rows(rgb_dir/name))) for name in FILES},
        license='CC BY-NC-SA 4.0; noncommercial, attribution, share-alike',
        selection='zh_refine[:20], zh_int[:15], zh_fact[:15]; source order',
        question_count=len(questions), document_count=len(documents), corpus=sorted(documents.values(), key=lambda x:x['document_id']),
        task_counts=dict(Counter(q['task_type'] for q in questions)), files=files,
        gaps=['No native unanswerable labels; pooled corpus invalidates noise_rate=1 refusal assumptions.',
              'No synthetic paraphrase or terminology categories.',
              'Integration groups may have overlapping or individually sufficient passages; no enforced multi-file reasoning claim.',
              'Relevance judgments are incomplete across pooled questions; no exact-span relevance annotations.'])
    write(output/'manifest.json', manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rgb-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Fetch only three pinned Chinese JSONL files, license and protocol')
    args=parser.parse_args()
    if args.download:
        acquire(args.rgb_dir)
    manifest=build(args.output, args.rgb_dir)
    print(json.dumps({k:manifest[k] for k in ('question_count','document_count','task_counts')},ensure_ascii=False))


if __name__=='__main__': main()
