import { useEffect, useMemo, useRef, useState } from 'react';
import { citationHtml } from './citationHtml';
import { Button, Select, Tabs, TabPane, Tag, TextArea, Toast } from '@douyinfe/semi-ui';
import { IconSearch, IconExternalOpen } from '@douyinfe/semi-icons';
import { api, agentStream } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import type { AskResponse } from '../types';

export function QaPage() {
  const {activeAgentId, activeAgent} = usePlatform();
  const requestVersion = useRef(0);
  const [error, setError] = useState('');
  const [elapsed, setElapsed] = useState(0);
  const [submitted, setSubmitted] = useState('');
  const [tab, setTab] = useState('sources');
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<AskResponse | null>(null);
  const [reactMode, setReactMode] = useState(true);
  const [events, setEvents] = useState<Array<Record<string, any>>>([]);
  const [thread, setThread] = useState('');
  const [pending, setPending] = useState('');
  const [conversation, setConversation] = useState(() => crypto.randomUUID());
  useEffect(() => {
    let active = true;
    requestVersion.current += 1; setLoading(false); setError(''); setElapsed(0); setSubmitted('');
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
    const version = ++requestVersion.current; const started = performance.now();
    setLoading(true); setError('');
    if (!resume) setPending('');
    try {
      await agentStream(resume ? `/api/v2/agent/threads/${encodeURIComponent(thread)}/resume/stream` : '/api/v2/agent/stream',
        resume ? undefined : {agent_id: activeAgentId, question: question.trim(), conversation_id: conversation}, event => {
          if (version !== requestVersion.current) return;
          if (event.thread_id) {
            setThread(event.thread_id);
            try {localStorage.setItem(`react-thread:${activeAgentId}`, JSON.stringify({thread: event.thread_id, conversation}));} catch { /* Optional UI convenience. */ }
          }
          if (event.type === 'approval_required') setPending(event.approval_id);
          if (event.type === 'result') {setResponse(event.data.response); if (event.data.status !== 'waiting_approval') setPending('');}
          if (event.type === 'failed') setError('执行失败，请检查权限、审批或模型状态。');
          if (event.type !== 'result' && event.type !== 'answer_delta') setEvents(old => [...old, event].slice(-100));
        });
    } catch (error) {if (version === requestVersion.current) setError(error instanceof Error ? error.message : '请求失败');}
    finally {if (version === requestVersion.current) {setLoading(false); setElapsed((performance.now() - started) / 1000);}}
  }

  async function ask() {
    if (loading || pending || !activeAgentId || !question.trim()) return;
    setSubmitted(question.trim()); setError(''); setElapsed(0); setTab('sources');
    if (reactMode) {setResponse(null); setEvents([]); await runReAct(); return;}
    const version = ++requestVersion.current; const started = performance.now();
    setLoading(true); setResponse(null); setEvents([]);
    try { const result = await api<AskResponse>(`/api/agents/${encodeURIComponent(activeAgentId)}/ask`, {method: 'POST', body: JSON.stringify({question: question.trim(), conversation_id: crypto.randomUUID(), history: []})}); if (version === requestVersion.current) setResponse(result); }
    catch (error) { if (version === requestVersion.current) setError(error instanceof Error ? error.message : '问答请求失败'); }
    finally { if (version === requestVersion.current) {setLoading(false); setElapsed((performance.now() - started) / 1000);} }
  }

  const suggestions = ['这份资料的主要内容是什么？', '相关要求和适用范围是什么？', '不同文件的规定有哪些差异？'];
  return <div className="qa-workspace">
    <section className="query-panel qa-composer">
      <div className="qa-mode-row"><strong>检索模式</strong><Select aria-label="检索模式" value={reactMode ? 'reasoning' : 'standard'} disabled={loading || !!pending} onChange={value => setReactMode(value === 'reasoning')} optionList={[{label: '智能推理（推荐）', value: 'reasoning'}, {label: '标准问答', value: 'standard'}]} /><Tag color="green">{activeAgent?.name || '未选择知识库'}</Tag></div>
      <p className="qa-help">{reactMode ? '根据问题自主检索、补读原文，并提供来源证据。' : '通过标准问答接口检索知识库并生成回答。'}</p>
      <TextArea aria-label="知识库问题" value={question} onChange={setQuestion} disabled={loading || !!pending} placeholder="输入具体问题，例如：不同文件对验收流程有哪些要求？" rows={4} onEnterPress={event => {if ((event.metaKey || event.ctrlKey) && !loading) {event.preventDefault(); void ask();}}} />
      <div className="qa-suggestions"><span>试着问</span>{suggestions.map(text => <Button key={text} size="small" theme="borderless" disabled={loading || !!pending} onClick={() => setQuestion(text)}>{text}</Button>)}</div>
      <div className="query-actions"><span>Ctrl / ⌘ + Enter 提交</span><Button icon={<IconSearch />} theme="solid" type="primary" loading={loading} disabled={!question.trim() || !activeAgentId || !!pending} onClick={() => void ask()}>{loading ? '正在检索' : '开始检索'}</Button></div>
    </section>
    <section className="answer-panel qa-result" aria-live="polite" aria-busy={loading}>
      {!response && !loading && !error && !pending && <div className="empty-answer"><div className="qa-empty-icon"><IconSearch size="extra-large" /></div><h2>{activeAgentId ? '开始提问，获取有据可查的答案' : '先选择一个知识库'}</h2><p>{activeAgentId ? '回答、引用来源和执行轨迹会展示在这里。可以先点击上方的问题示例。' : '在顶部选择有权访问的知识库；暂无知识库时，可前往“知识库”添加连接。'}</p></div>}
      {loading && <div className="answer-loading" role="status"><span />正在检索与整理证据…{reactMode && thread && <Button onClick={() => void api(`/api/v2/agent/threads/${encodeURIComponent(thread)}/cancel`, {method:'POST'}).catch(() => Toast.error('停止请求失败，请重试'))}>停止执行</Button>}</div>}
      {error && <div className="qa-error" role="alert"><strong>本次请求未完成</strong><p>{error}</p><span>问题已保留。请确认连接后重试。</span></div>}
      {pending && <div className="qa-pending"><strong>等待工具审批</strong><p>请在工具中心处理审批后继续，执行将从暂停处恢复。</p><Button disabled={loading} onClick={() => void runReAct(true)}>审批处理后继续</Button></div>}
      {response && <><div className="qa-result-heading"><h2>{submitted || '检索结果'}</h2><Tag>{elapsed ? `${elapsed.toFixed(1)} 秒` : '已返回'}</Tag></div><article className="markdown-body answer-body" onClick={event => {const link = (event.target as HTMLElement).closest('a'); if (link?.getAttribute('href')?.startsWith('#citation-')) {event.preventDefault(); const id = link.getAttribute('href')!.slice(1); setTab('sources'); requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({block: 'nearest'}));}}} dangerouslySetInnerHTML={{__html: answerHtml}} /><div className="answer-meta">{response.agent_name} · {response.tool_calls} 次工具调用 · {response.evidences.length} 条证据{response.runtime_mode === 'react_v2' && <span> · {response.citation_validation === 'passed' ? '引用已复核；结论正确性未评定' : '引用未通过，显示安全提示'}</span>}</div></>}
      {(response || events.length > 0) && <Tabs activeKey={tab} onChange={setTab}>
        <TabPane tab={`引用来源（${response?.evidences.length || 0}）`} itemKey="sources"><div className="source-list">{response?.evidences.map((item, index) => <article id={`citation-${index + 1}`} key={`${item.source_id}-${index}`}><div><span>{index + 1}</span><strong>{item.filename}</strong></div><small>{item.page != null ? `第 ${item.page} 页` : item.location || '位置未标注'} · {item.retrieval_type}</small><p>{item.content}</p><Button size="small" theme="borderless" icon={<IconExternalOpen />} onClick={() => window.open(item.pdf_url || `/api/files/${encodeURIComponent(item.source_id)}`, '_blank', 'noopener,noreferrer')}>打开原始文件</Button></article>)}{!response?.evidences.length && <p className="qa-help">尚无可展示的引用来源。</p>}</div></TabPane>
        <TabPane tab="执行轨迹" itemKey="trace"><pre className="trace-json">{JSON.stringify(events.length ? events : response?.trace || [], null, 2)}</pre></TabPane>
      </Tabs>}
    </section>
  </div>;
}
