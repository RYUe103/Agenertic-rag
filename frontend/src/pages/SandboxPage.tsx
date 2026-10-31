import { useEffect, useState } from 'react';
import { Button, Form, Select, SideSheet, Table, Toast, Typography } from '@douyinfe/semi-ui';
import { IconPlay, IconRefresh } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { usePlatform } from '../context/PlatformContext';
import { formatDate } from '../utils/format';
import type { Source, SourcePage } from '../types';

interface Policy {enabled: boolean; allow_users: boolean; allow_python: boolean; allow_shell: boolean; allow_documents: boolean; timeout_seconds: number; memory_mb: number; cpu: number; workspace_mb: number;}
interface Run {id: string; owner: string; name: string; language: string; status: string; code?: string; stdout?: string; stderr?: string; duration_ms?: number; created_at: string; artifacts?: {id: string; name: string; size: number}[]; resource_usage?: {status: string; cpu_percent_peak?: number; memory_bytes_peak?: number; pids_peak?: number};}

export function SandboxPage() {
  const {session, activeAgentId} = usePlatform();
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<Run | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  const [sourceQuery, setSourceQuery] = useState('');
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try { const [nextPolicy, nextRuns] = await Promise.all([session.is_admin ? api<Policy>('/api/admin/sandbox/policy') : Promise.resolve(null), api<Run[]>('/api/sandbox/runs')]); setPolicy(nextPolicy); setRuns(nextRuns); if (nextPolicy) form.setValues(nextPolicy); }
    catch (error) { Toast.error(error instanceof Error ? error.message : 'Sandbox 加载失败'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    setSourceIds([]); setSources([]); setSourceQuery('');
  }, [activeAgentId]);
  useEffect(() => {
    if (!activeAgentId) return;
    let active = true;
    const timer = window.setTimeout(() => {
      api<SourcePage>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/page?page_size=100&query=${encodeURIComponent(sourceQuery)}`).then(result => {if (active) setSources(current => [...current.filter(item => sourceIds.includes(item.source_id) && !result.items.some(next => next.source_id === item.source_id)), ...result.items]);}).catch(() => {});
    }, 250);
    return () => {active = false; window.clearTimeout(timer);};
  }, [activeAgentId, sourceQuery]);

  async function save(value: Record<string, unknown>) {
    try { const next = await api<Policy>('/api/admin/sandbox/policy', {method: 'PUT', body: JSON.stringify(value)}); setPolicy(next); Toast.success('策略已保存'); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '保存失败'); }
  }
  async function execute(value: Record<string, unknown>) {
    setRunning(true);
    try { const result = await api<Run>('/api/sandbox/runs', {method: 'POST', body: JSON.stringify({...value, agent_id: activeAgentId, source_ids: sourceIds})}); setSelected(result); await load(); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '执行失败'); }
    finally { setRunning(false); }
  }

  return <div className="page-stack">
    {session.is_admin && <section className="content-section"><div className="section-heading"><div><Typography.Title heading={5}>Sandbox 策略</Typography.Title><Typography.Text type="tertiary">执行在独立 Docker 容器中进行，默认无网络且不能访问知识库文件。</Typography.Text></div><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button></div>
      <Form form={form} layout="horizontal" labelPosition="left" onSubmit={value => void save(value as Record<string, unknown>)} initValues={policy ? {...policy} : undefined}>
        <Form.Switch field="enabled" label="允许代码执行" />
        <Form.Switch field="allow_users" label="允许普通用户执行" />
        <Form.Switch field="allow_python" label="允许 Python" />
        <Form.Switch field="allow_shell" label="允许 Shell" />
        <Form.Switch field="allow_documents" label="允许读取所选文档" />
        <Form.InputNumber field="timeout_seconds" label="最大执行时间（秒）" min={1} max={120} />
        <Form.InputNumber field="memory_mb" label="内存限制（MB）" min={64} max={1024} />
        <Form.InputNumber field="cpu" label="CPU 限制" min={0.1} max={2} step={0.1} />
        <Form.InputNumber field="workspace_mb" label="临时空间（MB）" min={1} max={128} />
        <Button theme="solid" htmlType="submit">保存策略</Button>
      </Form>
    </section>}
    <section className="content-section"><div className="section-heading"><div><Typography.Title heading={5}>执行测试</Typography.Title><Typography.Text type="tertiary">将输出文件写入 /tmp/output；单次最多保存 20 个文件、共 2 MB。</Typography.Text></div></div>
      <Form layout="vertical" onSubmit={value => void execute(value as Record<string, unknown>)} initValues={{name: '手动分析', language: 'python', code: 'print(1 + 1)'}}>
        <Form.Input field="name" label="任务名称" /><Form.Select field="language" label="语言"><Form.Select.Option value="python">Python</Form.Select.Option><Form.Select.Option value="shell">Shell</Form.Select.Option></Form.Select>
        <div><Typography.Text>授权文档</Typography.Text><br /><Select multiple filter showClear maxTagCount={3} value={sourceIds} onSearch={setSourceQuery} onChange={value => setSourceIds((value as string[]).slice(0, 10))} placeholder="搜索当前知识库的文档（最多 10 个）" style={{width: '100%', maxWidth: 600}}>{sources.map(source => <Select.Option key={source.source_id} value={source.source_id}>{source.filename}</Select.Option>)}</Select></div>
        {sourceIds.length > 0 && <Typography.Text type="tertiary">Python 使用 knowledge_sdk.list_documents() 和 knowledge_sdk.read_document(source_id) 读取解析文本副本。</Typography.Text>}
        <Form.TextArea field="code" label="代码" rows={8} maxLength={32768} showClear /><Button htmlType="submit" theme="solid" icon={<IconPlay />} loading={running}>执行</Button>
      </Form>
    </section>
    <section className="content-section flush-section"><div className="section-heading"><Typography.Title heading={5}>执行记录</Typography.Title><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button></div><Table dataSource={runs} rowKey="id" loading={loading} onRow={record => ({onClick: () => { if (record) api<Run>(`/api/sandbox/runs/${record.id}`).then(setSelected).catch(error => Toast.error(error.message)); }})} empty="暂无执行记录"><Table.Column title="任务" dataIndex="name" /><Table.Column title="用户" dataIndex="owner" /><Table.Column title="语言" dataIndex="language" width={100} /><Table.Column title="状态" dataIndex="status" width={110} render={value => <StatusTag status={String(value)} />} /><Table.Column title="耗时" dataIndex="duration_ms" width={110} render={value => value ? `${value} ms` : '—'} /><Table.Column title="时间" dataIndex="created_at" width={180} render={value => formatDate(String(value))} /></Table></section>
    <SideSheet title={selected?.name || '执行详情'} visible={Boolean(selected)} onCancel={() => setSelected(null)} width={760}>{selected && <div className="task-detail"><Typography.Title heading={6}>代码</Typography.Title><pre className="code-block">{selected.code}</pre><Typography.Title heading={6}>标准输出</Typography.Title><pre className="code-block">{selected.stdout || '—'}</pre><Typography.Title heading={6}>错误输出</Typography.Title><pre className="code-block">{selected.stderr || '—'}</pre><Typography.Title heading={6}>资源使用</Typography.Title><Typography.Text type="tertiary">{selected.resource_usage?.status === 'collected' || selected.resource_usage?.status === 'partial' ? `CPU 峰值 ${selected.resource_usage.cpu_percent_peak?.toFixed(1) || 0}% · 内存峰值 ${Math.round((selected.resource_usage.memory_bytes_peak || 0) / 1024 / 1024)} MB · 进程峰值 ${selected.resource_usage.pids_peak || 0}` : '未采集'}</Typography.Text><Typography.Title heading={6}>输出文件</Typography.Title>{selected.artifacts?.length ? selected.artifacts.map(file => <div key={file.id}><a href={`/api/sandbox/runs/${selected.id}/artifacts/${file.id}`}>{file.name}</a> <Typography.Text type="tertiary">{file.size} B</Typography.Text></div>) : <Typography.Text type="tertiary">无输出文件</Typography.Text>}</div>}</SideSheet>
  </div>;
}
