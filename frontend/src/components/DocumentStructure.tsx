import { useEffect, useState } from 'react';
import { Button, Input, Spin, Tag, Typography } from '@douyinfe/semi-ui';
import { IconSearch } from '@douyinfe/semi-icons';
import { api, queryString } from '../api/client';

interface Segment {id: string; content: string; position: number | null; status: string | null; word_count: number | null; tokens: number | null; page?: number | null; chapter?: string | null;}
interface SegmentPage {items: Segment[]; page: number; may_have_more: boolean; indexed: boolean;}

export function DocumentOutline({agentId, sourceId}: {agentId: string; sourceId: string}) {
  const [outline, setOutline] = useState<{text: string; headings: {level: number; title: string; line: number; page?: number | null}[]}>({text: '', headings: []});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    api<{outline: string; headings: {level: number; title: string; line: number; page?: number | null}[]}>(`/api/agents/${encodeURIComponent(agentId)}/sources/${encodeURIComponent(sourceId)}/outline`)
      .then(value => {if (active) setOutline({text: value.outline, headings: value.headings || []});})
      .catch(reason => {if (active) setError(reason instanceof Error ? reason.message : '目录加载失败');})
      .finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [agentId, sourceId]);
  if (loading) return <Spin />;
  if (error) return <Typography.Text type="danger">{error}</Typography.Text>;
  return outline.headings.length ? <div className="metadata-list">{outline.headings.map(item => <div key={`${item.line}-${item.title}`} style={{paddingLeft: `${(item.level - 1) * 18}px`}}><strong>{item.title}</strong><small>第 {item.line} 行{item.page ? ` · 第 ${item.page} 页` : ' · 页码未采集'}</small></div>)}</div> : outline.text ? <pre className="trace-json">{outline.text}</pre> : <div className="empty-inline">解析内容中没有识别到目录</div>;
}

export function DocumentSegments({agentId, sourceId}: {agentId: string; sourceId: string}) {
  const [data, setData] = useState<SegmentPage | null>(null);
  const [keyword, setKeyword] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    api<SegmentPage>(`/api/agents/${encodeURIComponent(agentId)}/sources/${encodeURIComponent(sourceId)}/segments${queryString({page, page_size: 20, keyword: query})}`)
      .then(value => {if (active) setData(value);})
      .catch(reason => {if (active) {setData(null); setError(reason instanceof Error ? reason.message : '分段加载失败');}})
      .finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [agentId, sourceId, page, query]);
  function search() {setPage(1); setQuery(keyword);}
  return <div className="page-stack">
    <div className="filter-cluster"><Input prefix={<IconSearch />} value={keyword} onChange={setKeyword} onEnterPress={search} placeholder="搜索分段内容" /><Button onClick={search}>搜索</Button></div>
    {loading && <Spin />}
    {error && <Typography.Text type="danger">{error}</Typography.Text>}
    {!loading && !error && data && <>
      {!data.indexed ? <div className="empty-inline">该文档尚未提交 Dify 索引</div> : data.items.length === 0 ? <div className="empty-inline">没有找到分段</div> : data.items.map((item, index) => <div className="content-section" key={`${item.id}-${index}`}>
        <div className="toolbar-row"><Typography.Text strong>分段 {item.position ?? ((page - 1) * 20 + index + 1)}</Typography.Text>{item.status && <Tag>{item.status}</Tag>}</div>
        <Typography.Text type="tertiary">{item.id || 'ID 未提供'}{item.page != null ? ` · 第 ${item.page} 页` : ' · 页码未采集'}{item.chapter ? ` · ${item.chapter}` : ' · 章节未采集'}{item.word_count != null ? ` · ${item.word_count} 字` : ''}{item.tokens != null ? ` · ${item.tokens} tokens` : ''}</Typography.Text>
        <pre className="trace-json">{item.content}</pre>
      </div>)}
      {data.indexed && <div className="filter-cluster"><Button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button><Typography.Text>第 {page} 页</Typography.Text><Button disabled={!data.may_have_more} onClick={() => setPage(page + 1)}>下一页</Button></div>}
    </>}
  </div>;
}
