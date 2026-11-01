import { useEffect, useMemo, useState } from 'react';
import { citationHtml } from './citationHtml';
import { Button, Collapse, Switch, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconSearch, IconExternalOpen } from '@douyinfe/semi-icons';
import { api, agentStream } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import type { AskResponse } from '../types';

export function QaPage() {
  const {activeAgentId, activeAgent} = usePlatform();
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<AskResponse | null>(null);
  const [reactMode, setReactMode] = useState(false);
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [thread, setThread] = useState('');
  const [pending, setPending] = useState('');
  const [conversation, setConversation] = useState(() => crypto.randomUUID());
  useEffect(() => {
    let active = true;
    setThread(''); setPending(''); setConversation(crypto.randomUUID()); setResponse(null); setEvents([]);
    try {
      const saved = JSON.parse(localStorage.getItem(`react-thread:${activeAgentId}`) || 'null');
      if (saved?.thread) void api<{status: string; pending_approvals: Array<{approval_id: string}>}>(`/api/v2/agent/threads/${encodeURIComponent(saved.thread)}`).then(state => {
        if (!active) return;
        setThread(saved.thread); setConversation(saved.conversation); setReactMode(true);
        setPending(state.pending_approvals[0]?.approval_id || '');
      }).catch(() => localStorage.removeItem(`react-thread:${activeAgentId}`));
    } catch { /* Storage may be unavailable; server checkpoints remain accessible. */ }
    return () => {active = false;};
  }, [activeAgentId]);
  const answerHtml = useMemo(() => response ? citationHtml(response) : '', [response]);

  async function runReAct(resume = false) {
    setLoading(true); setPending('');
    try {
      await agentStream(resume ? `/api/v2/agent/threads/${encodeURIComponent(thread)}/resume/stream` : '/api/v2/agent/stream',
        resume ? undefined : {agent_id: activeAgentId, question: question.trim(), conversation_id: conversation}, event => {
          if (event.thread_id) {
            setThread(event.thread_id);
            try {localStorage.setItem(`react-thread:${activeAgentId}`, JSON.stringify({thread: event.thread_id, conversation}));} catch { /* Optional UI convenience. */ }
          }
          if (event.type === 'approval_required') setPending(event.approval_id);
          if (event.type === 'result') {setResponse(event.data.response); if (event.data.status !== 'waiting_approval') setPending('');}
          if (event.type === 'failed') Toast.error('Agent 执行失败，请检查权限、审批或模型状态');
          if (event.type !== 'result' && event.type !== 'answer_delta') setEvents(old => [...old, event].slice(-100));
        });
    } catch (error) {Toast.error(error instanceof Error ? error.message : 'Agent 请求失败');}
    finally {setLoading(false);}
  }

  async function ask() {
    if (!activeAgentId || !question.trim()) return;
    if (reactMode) {setResponse(null); setEvents([]); await runReAct(); return;}
    setLoading(true); setResponse(null);
    try { setResponse(await api<AskResponse>(`/api/agents/${encodeURIComponent(activeAgentId)}/ask`, {method: 'POST', body: JSON.stringify({question: question.trim(), conversation_id: crypto.randomUUID(), history: []})})); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '问答请求失败'); }
    finally { setLoading(false); }
  }

  return <div className="qa-layout">
    <div><Switch checked={reactMode} disabled={loading || !!pending} onChange={setReactMode} /> ReAct V2
      {reactMode && thread && loading && <Button onClick={() => void api(`/api/v2/agent/threads/${encodeURIComponent(thread)}/cancel`, {method:'POST'})}>停止执行</Button>}
      {pending && <span> 等待工具审批：{pending} <Button disabled={loading} onClick={() => void runReAct(true)}>审批处理后继续</Button></span>}
      {reactMode && events.length > 0 && <Collapse><Collapse.Panel header="Agent 运行状态" itemKey="events"><pre className="trace-json">{JSON.stringify(events, null, 2)}</pre></Collapse.Panel></Collapse>}
    </div>
    <section className="query-panel"><div><Typography.Text strong>{activeAgent?.name || '未选择知识库'}</Typography.Text></div><TextArea value={question} onChange={setQuestion} placeholder="输入要检索的文件或具体问题" autosize rows={5} onEnterPress={(event) => {if ((event.metaKey || event.ctrlKey) && !loading) void ask();}} /><div className="query-actions"><span>Ctrl / ⌘ + Enter 提交</span><Button icon={<IconSearch />} theme="solid" type="primary" loading={loading} disabled={!question.trim() || !activeAgentId} onClick={() => void ask()}>开始检索</Button></div></section>
    <section className="answer-panel">{!response && !loading && <div className="empty-answer"><IconSearch size="extra-large" /><strong>等待检索</strong><span>回答、来源文件和执行轨迹将在此显示</span></div>}{loading && <div className="answer-loading"><span />正在定位文件并检索证据…</div>}{response && <><article className="markdown-body answer-body" dangerouslySetInnerHTML={{__html: answerHtml}} /><div className="answer-meta">{response.agent_name} · {response.tool_calls} 次工具调用 · {response.evidences.length} 条证据{response.runtime_mode === 'react_v2' && <span> · {response.citation_validation === 'passed' ? '引用已复核；结论正确性未评定' : '引用未通过，显示安全提示'}</span>}</div><Typography.Title heading={6}>来源文件</Typography.Title><div className="source-list">{response.evidences.map((item, index) => <article id={`citation-${index + 1}`} key={`${item.source_id}-${index}`}><div><span>{index + 1}</span><strong>{item.filename}</strong></div><small>{item.page ? `第 ${item.page} 页` : item.location || '位置未标注'} · {item.retrieval_type}</small><p>{item.content}</p><Button size="small" theme="borderless" icon={<IconExternalOpen />} onClick={() => window.open(item.pdf_url || `/api/files/${encodeURIComponent(item.source_id)}`, '_blank', 'noopener,noreferrer')}>打开原始文件</Button></article>)}</div>{response.trace && response.trace.length > 0 && <Collapse><Collapse.Panel header="Agent 执行轨迹" itemKey="trace"><pre className="trace-json">{JSON.stringify(response.trace, null, 2)}</pre></Collapse.Panel></Collapse>}</>}</section>
  </div>;
}
