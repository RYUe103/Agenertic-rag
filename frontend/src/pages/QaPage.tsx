import { useMemo, useState } from 'react';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { Button, Collapse, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconSearch, IconExternalOpen } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import type { AskResponse } from '../types';

export function QaPage() {
  const {activeAgentId, activeAgent} = usePlatform();
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<AskResponse | null>(null);
  const answerHtml = useMemo(() => response ? DOMPurify.sanitize(marked.parse(response.answer) as string) : '', [response]);

  async function ask() {
    if (!activeAgentId || !question.trim()) return;
    setLoading(true); setResponse(null);
    try { setResponse(await api<AskResponse>(`/api/agents/${encodeURIComponent(activeAgentId)}/ask`, {method: 'POST', body: JSON.stringify({question: question.trim(), conversation_id: crypto.randomUUID(), history: []})})); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '问答请求失败'); }
    finally { setLoading(false); }
  }

  return <div className="qa-layout">
    <section className="query-panel"><div><Typography.Text strong>{activeAgent?.name || '未选择 Agent'}</Typography.Text><Typography.Text type="tertiary">{activeAgent?.description}</Typography.Text></div><TextArea value={question} onChange={setQuestion} placeholder="输入要检索的文件或具体问题" autosize rows={5} onEnterPress={(event) => {if ((event.metaKey || event.ctrlKey) && !loading) void ask();}} /><div className="query-actions"><span>Ctrl / ⌘ + Enter 提交</span><Button icon={<IconSearch />} theme="solid" type="primary" loading={loading} disabled={!question.trim() || !activeAgentId} onClick={() => void ask()}>开始检索</Button></div></section>
    <section className="answer-panel">{!response && !loading && <div className="empty-answer"><IconSearch size="extra-large" /><strong>等待检索</strong><span>回答、来源文件和执行轨迹将在此显示</span></div>}{loading && <div className="answer-loading"><span />正在定位文件并检索证据…</div>}{response && <><article className="markdown-body answer-body" dangerouslySetInnerHTML={{__html: answerHtml}} /><div className="answer-meta">{response.agent_name} · {response.tool_calls} 次工具调用 · {response.evidences.length} 条证据</div><Typography.Title heading={6}>来源文件</Typography.Title><div className="source-list">{response.evidences.map((item, index) => <article key={`${item.source_id}-${index}`}><div><span>{index + 1}</span><strong>{item.filename}</strong></div><small>{item.page ? `第 ${item.page} 页` : item.location || '位置未标注'} · {item.retrieval_type}</small><p>{item.content}</p><Button size="small" theme="borderless" icon={<IconExternalOpen />} onClick={() => window.open(`/api/files/${encodeURIComponent(item.source_id)}`, '_blank')}>打开原始文件</Button></article>)}</div>{response.trace && response.trace.length > 0 && <Collapse><Collapse.Panel header="Agent 执行轨迹" itemKey="trace"><pre className="trace-json">{JSON.stringify(response.trace, null, 2)}</pre></Collapse.Panel></Collapse>}</>}</section>
  </div>;
}
