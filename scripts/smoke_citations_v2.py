"""Real, isolated citation comparison; only counts/hashes enter the public report."""
import argparse
import json
import re
from pathlib import Path
from langchain_core.messages import ToolMessage
from scripts import smoke_react_v2 as smoke


class TracedModel:
    def __init__(self, model):
        self.model = model
        self.calls = []

    def react(self, messages, tools, **kwargs):
        observations = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        visible = {e['evidence_id'] for o in observations for e in o.get('evidence', [])}
        repair_sources = []
        if not tools and messages:
            try:
                payload = json.loads(messages[-1].content)
                if isinstance(payload,dict) and {'question','draft','evidence'}<=set(payload):
                    repair_sources = [e['source_id'] for e in payload['evidence']]
            except (ValueError,TypeError,KeyError):
                pass
        response = self.model.react(messages, tools, **kwargs)
        self.calls.append(dict(context_chars=sum(len(str(m.content)) for m in messages),
            tool_message_chars=sum(len(m.content) for m in messages if isinstance(m, ToolMessage)),
            model_visible_evidence_count=len(visible) or len(set(repair_sources)),
            output_has_ev=bool(re.search(r'\[ev_', str(response.content))),
            output_has_short=bool(re.search(r'\[S\d+\]', str(response.content))),
            output_has_numeric=bool(re.search(r'\[\d+\]', str(response.content))),
            output_has_claims='"claims"' in str(response.content),
            usage=response.response_metadata.get('usage', {})))
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    directory = args.output
    directory.mkdir(parents=True, exist_ok=True)
    if (directory/'sources.db').exists():
        raise ValueError('fresh directory required')
    payload = Path('data/dify-formal-closure-accepted-20261101/submitted.md').read_bytes()
    copy = directory/'source.md'; copy.write_bytes(payload)
    repo = smoke.SourceRepository(directory/'sources.db', 'smoke')
    source = repo.create(dict(source_id='existing-source', filename='existing-source.md',
        normalized_filename='existing-source', file_hash=smoke.sha(payload),
        local_pdf_path=str(copy.resolve()), local_md_path=str(copy.resolve()), parse_status='success'))
    repo.save_revision(source, payload.decode(), 'mineru', 'Isolated citation comparison')
    smoke.IndexManager(repo.documents).sync()
    qa = next(json.loads(line) for line in Path('data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl').read_text().splitlines()
              if json.loads(line)['id'] == 'tatqa_test_9063')
    cases = {
        'simple': qa['question'],
        'section': '授权文档 existing-source。请阅读文档，使用其中真实的章节 ID 读取相关章节，说明数值适用年份并引用。证据没有年份时明确说明。',
        'degraded': 'Search with hybrid for operating income in document existing-source. If vector is unavailable, continue with verifiable local evidence and state the limitation.',
        'table': '授权文档 existing-source。请用 read_document_table 读取结构化表格，选取同一行、单位一致的两个年度数值，计算百分比变化并引用原表。无法确认表头或口径时说明不足。',
        'table_calculation': 'Read structured tables in existing-source using read_document_table with max_chars=100000 and limit=1. Read further tables if necessary. Compute the change between two comparable annual values with calculate. Cite the exact evidence_id from tool output; do not guess units or applicability.',
        'no_evidence': '请查找授权文档中没有记载的 2099 年火星殖民地预算。如果没有原文证据，明确拒绝给出数值。',
        'stale': qa['question'],
    }
    report = dict(model=smoke.Settings().llm_model, source_sha256=smoke.sha(payload),
                  vector='unconfigured local degradation; not cloud outage', cases={})
    for name, question in cases.items():
        runner = smoke.runtime(directory)
        traced = TracedModel(runner.model); runner.model = traced
        if name == 'stale':
            original = runner.validate
            def invalidate(state):
                repo.save_revision(repo.get('existing-source'), payload.decode()+'\nChanged version.\n', 'local', 'isolated stale test')
                return original(state)
            runner.validate = invalidate
        try:
            result = runner.run(question, conversation=name)
            (directory/(name+'-private-result.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
            entry = smoke.redacted(result)
            entry['metrics'] = result.get('metrics', {})
            entry['repair_attempted'] = result['result'].get('repair_attempted', False)
            entry['repair_success'] = result['result'].get('repair_success', False)
            entry['tool_metrics'] = [{k:v for k,v in e.items() if k in {
                'tool_candidate_count','registered_evidence_count','registration_rejections',
                'model_observation_evidence_count','output_truncated','internal_chars','visible_chars'}}
                for e in result['events'] if e['type']=='tool_evidence_metrics']
            with runner.store.connect() as db:
                outputs = [json.loads(r[0]) for r in db.execute("SELECT output FROM calls WHERE run=? AND call!='__citation_repair__'", (result['run_id'],))]
            entry['receipts'] = [dict(status=o.get('status'), registered_evidence_count=len(o.get('_evidence', {})),
                tool_message_evidence_count=len(o.get('evidence', [])),
                output_chars=len(json.dumps(o, ensure_ascii=False)),
                partial=(o.get('data') or {}).get('partial', False),
                error_code=(o.get('error') or {}).get('code')) for o in outputs]
        except Exception as exc:
            entry = dict(status='failed', error_type=type(exc).__name__)
        entry['model_calls'] = traced.calls
        entry['question_sha256'] = smoke.sha(question)
        report['cases'][name] = entry
        (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(name, entry.get('citation_validation', entry['status']), flush=True)


if __name__ == '__main__':
    main()
