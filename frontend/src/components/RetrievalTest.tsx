import { useState } from 'react';
import { Button, Form, InputNumber, Select, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconSearch } from '@douyinfe/semi-icons';
import { api } from '../api/client';

interface Result {
  items: {source_id: string; filename: string; segment_id: string | null; content: string; score: number | null; page: number | null; location: string | null}[];
  elapsed_ms: number;
  unmatched_count: number;
}

export function RetrievalTest({knowledgeBaseId}: {knowledgeBaseId: string}) {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState('semantic_search');
  const [topK, setTopK] = useState<number | undefined>(10);
  const [filters, setFilters] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  async function search() {
    if (!query.trim()) return;
    setLoading(true);
    setResult(null);
    try {
      let conditions: unknown = null;
      if (filters.trim()) {
        conditions = JSON.parse(filters);
        if (!conditions || typeof conditions !== 'object' || Array.isArray(conditions)) throw new Error('元数据过滤条件必须是 JSON 对象');
      }
      setResult(await api<Result>(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/retrieval-test`, {
        method: 'POST', body: JSON.stringify({query: query.trim(), search_method: mode, top_k: topK, metadata_filtering_conditions: conditions}),
      }));
    } catch (error) { Toast.error(error instanceof Error ? error.message : '检索失败'); }
    finally { setLoading(false); }
  }

  return <div className="page-stack">
    <Form layout="vertical" onSubmit={() => void search()}>
      <Form.Slot label="查询"><TextArea value={query} onChange={setQuery} rows={3} maxCount={4000} /></Form.Slot>
      <Form.Slot label="检索模式"><Select value={mode} onChange={value => setMode(String(value))} style={{width: '100%'}}><Select.Option value="semantic_search">向量检索</Select.Option><Select.Option value="full_text_search">全文检索</Select.Option><Select.Option value="hybrid_search">混合检索</Select.Option></Select></Form.Slot>
      <Form.Slot label="Top-K"><InputNumber value={topK} onChange={value => setTopK(typeof value === 'number' ? value : undefined)} min={1} max={50} /></Form.Slot>
      <Form.Slot label="元数据过滤条件（Dify JSON，可选）"><TextArea value={filters} onChange={setFilters} rows={3} placeholder={'{"logical_operator":"and","conditions":[]}'} /></Form.Slot>
      <Button icon={<IconSearch />} htmlType="submit" theme="solid" type="primary" loading={loading} disabled={!query.trim() || !topK}>检索</Button>
    </Form>
    {result && <section><Typography.Title heading={6}>检索结果</Typography.Title><Typography.Text type="tertiary">{result.items.length} 条结果 · {result.elapsed_ms} ms{result.unmatched_count ? ` · ${result.unmatched_count} 条 Dify 记录未关联本地文档` : ''}</Typography.Text>
      {!result.items.length && <div className="empty-inline">没有匹配的本地文档</div>}
      <div className="source-list">{result.items.map((item, index) => <article key={`${item.source_id}-${item.segment_id || index}`}><div><strong>{item.filename}</strong></div><small>分段 {item.segment_id || '未提供'} · 分数 {item.score == null ? '未提供' : item.score.toFixed(4)} · {item.page ? `第 ${item.page} 页` : '页码未采集'} · {item.location || '章节未采集'}</small><p>{item.content}</p><Button size="small" theme="borderless" onClick={() => window.open(`/api/files/${encodeURIComponent(item.source_id)}`, '_blank')}>打开文档</Button></article>)}</div>
    </section>}
  </div>;
}
