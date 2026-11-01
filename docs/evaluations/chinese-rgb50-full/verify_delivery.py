import json,hashlib,re
from pathlib import Path
from collections import Counter
root=Path('docs/evaluations/chinese-rgb50-full');report=json.loads(Path('docs/CHINESE_AGENT_50_RESULTS.json').read_text());md=Path('docs/CHINESE_AGENT_50_RESULTS.md').read_text();audit=json.loads((root/'audit.json').read_text());assert len(report['cases'])==50
ids=[f'rgb_zh_{i:04d}' for i in range(1,51)];assert [r['question_id'] for r in report['cases']]==ids
assert len(re.findall(r'^### \d\d · rgb_zh_',md,re.M))==50
checks=0
for row,record in zip(report['cases'],audit['artifacts']):
 qid=row['question_id'];rawpath=root/'cases'/f'{qid}.json';raw=json.loads(rawpath.read_text());assert hashlib.sha256(rawpath.read_bytes()).hexdigest()==record['case_sha256'];assert row['agent_answer']==raw['result']['result']['answer'];assert row['provider_usage']==raw['result']['usage'];assert row['tool_calls']==len(row['tool_steps'])==len(raw['trace']['tools']);assert json.loads((root/'attempts'/f'{qid}.json').read_text())['artifact_sha256']==record['case_sha256']
 transcripts=root/'messages'/f'{qid}.json';assert hashlib.sha256(transcripts.read_bytes()).hexdigest()==record['messages_sha256']
 for cite in row['citations']:
  hit=cite['evidence'];source=Path('benchmarks/chinese_rgb50/files')/f"{hit['document_id']}.txt";text=source.read_bytes().decode();assert text[hit['locator']['start']:hit['locator']['end']]==hit['content'];assert hit['verification_status']=='verified_current';checks+=1
 assert row['agent_answer'] in md
counts=Counter(x['codex_judgment']['grade'] for x in report['cases']);assert counts==report['summary']['grades'];assert sum(x['provider_usage']['total_tokens'] for x in report['cases'])==1436465
patterns=[re.compile(r'\bsk-[A-Za-z0-9]{20,}'),re.compile(r'\bdataset-[A-Za-z0-9]{20,}'),re.compile(r'Bearer [A-Za-z0-9._-]{20,}')]
files=[*root.rglob('*.json'),Path('docs/CHINESE_AGENT_50_RESULTS.json'),Path('docs/CHINESE_AGENT_50_RESULTS.md')]
for f in files:
 t=f.read_text()
 assert not any(p.search(t) for p in patterns),f'secret-like content in {f}'
print(json.dumps({'cases':50,'exact_final_answers':50,'raw_artifact_hashes':50,'message_archive_hashes':50,'original_citation_ranges_checked':checks,'token_sum':1436465,'grade_counts':counts,'secret_pattern_scan':'passed','files_scanned':len(files)},ensure_ascii=False))
