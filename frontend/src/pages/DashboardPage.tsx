import { useEffect, useState } from 'react';
import { Button, Progress, Skeleton, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconArrowRight, IconRefresh } from '@douyinfe/semi-icons';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import type { Health, SourceStats, TaskSummary } from '../types';
import { formatDate, taskProgress } from '../utils/format';

interface RecentDocument {source_id: string; filename: string; knowledge_base: string; parse_status: string; dify_status: string; created_at: string;}
interface RecentRun {run_id: string; agent_name: string; status: string; operation: string; created_at: string; duration_ms: number | null;}
interface RecentCall {id: number; knowledge_base_id: string; tool_name: string; channel: string; status: string; duration_ms: number; created_at: string;}
interface Dashboard {knowledge_bases: number; documents: SourceStats; tools: number; active_integrations: number; agent_run_total: number; recent_documents: RecentDocument[]; recent_runs: RecentRun[]; recent_tasks: TaskSummary[]; recent_tool_calls: RecentCall[];}

export function DashboardPage() {
  const navigate = useNavigate();
  const [health, setHealth] = useState<Health | null>(null);
  const [data, setData] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const [overview, status] = await Promise.allSettled([api<Dashboard>('/api/dashboard'), api<Health>('/api/health')]);
    if (overview.status === 'fulfilled') setData(overview.value);
    else {setData(null); Toast.error(overview.reason instanceof Error ? overview.reason.message : '无法加载总览');}
    setHealth(status.status === 'fulfilled' ? status.value : null);
    setLoading(false);
  }
  useEffect(() => { void load(); }, []);
  if (loading && !data) return <Skeleton placeholder={<Skeleton.Paragraph rows={8} />} active />;
  if (!data) return <div className="empty-inline">总览加载失败 <Button icon={<IconRefresh />} onClick={() => void load()}>重试</Button></div>;

  const metrics = [
    {label: '知识库', value: data?.knowledge_bases, note: '可访问且已启用'},
    {label: '文档', value: data?.documents.total, note: `${data?.documents.indexed ?? '—'} 份已建立索引`},
    {label: '处理中', value: data?.documents.processing, note: '解析或索引未完成'},
    {label: '异常文档', value: data?.documents.failed, note: '解析、索引或文件平台失败', danger: Boolean(data?.documents.failed)},
  ];

  return <div className="page-stack dashboard-page">
    <section className="metric-strip" aria-label="核心指标">{metrics.map(item => <div key={item.label} className={`metric-item${item.danger ? ' metric-danger' : ''}`}><span>{item.label}</span><strong>{item.value ?? '—'}</strong><small>{item.note}</small></div>)}</section>
    <div className="dashboard-insights"><section className="content-section"><div className="section-heading"><Typography.Title heading={5}>文档处理概况</Typography.Title><Typography.Text type="tertiary">当前可访问范围</Typography.Text></div><div className="document-bars">{[{label:'全部文档', value:data.documents.total},{label:'已解析',value:data.documents.parsed},{label:'已索引',value:data.documents.indexed},{label:'处理中',value:data.documents.processing},{label:'失败',value:data.documents.failed}].map(item => <div className="document-bar" key={item.label}><strong>{item.value}</strong><div className="document-bar-track"><span style={{height:`${data.documents.total ? Math.max(0, item.value / data.documents.total * 100) : 0}%`}} /></div><small>{item.label}</small></div>)}</div></section>
    <section className="content-section service-overview">
      <div className="section-heading"><Typography.Title heading={5}>服务运行状态</Typography.Title><Button icon={<IconRefresh />} theme="borderless" loading={loading} onClick={() => void load()}>刷新</Button></div>
      <div className="service-line"><div><span className="live-dot" /><strong>API 服务</strong><Tag color={health ? 'green' : 'grey'} size="small">{health ? '正常' : '不可用'}</Tag></div><div><span>LLM</span><StatusTag status={health ? (health.llm_configured ? 'success' : 'disabled') : 'unknown'} /></div><div><span>外部 API</span><StatusTag status={health ? (health.external_api_configured ? 'success' : 'disabled') : 'unknown'} /></div><div><span>问答并发</span><strong>{health?.ask_active ?? '—'} / {health?.ask_concurrency_limit ?? '—'}</strong></div><div><span>版本</span><code>{health?.build_version || '—'}</code></div></div>
    </section></div>
    <section className="content-section"><div className="section-heading"><Typography.Title heading={5}>最近任务</Typography.Title><Button theme="borderless" icon={<IconArrowRight />} iconPosition="right" onClick={() => navigate('/tasks')}>查看全部</Button></div>
      <Table<TaskSummary> dataSource={data?.recent_tasks || []} pagination={false} rowKey="task_id" size="small" empty="暂无任务记录"><Table.Column title="任务" dataIndex="title" /><Table.Column title="状态" dataIndex="status" width={110} render={value => <StatusTag status={String(value)} />} /><Table.Column title="进度" width={180} render={(_, item) => <div className="progress-cell"><Progress percent={taskProgress(item.completed, item.total)} size="small" showInfo={false} /><span>{item.completed}/{item.total}</span></div>} /><Table.Column title="更新时间" dataIndex="updated_at" width={170} render={value => formatDate(String(value))} /></Table>
    </section>
    <section className="content-section"><div className="section-heading"><Typography.Title heading={5}>最近上传</Typography.Title><Button theme="borderless" icon={<IconArrowRight />} iconPosition="right" onClick={() => navigate('/documents')}>文档管理</Button></div>
      <Table<RecentDocument> dataSource={data?.recent_documents || []} pagination={false} rowKey="source_id" size="small" empty="暂无文档"><Table.Column title="文件" dataIndex="filename" /><Table.Column title="知识库" dataIndex="knowledge_base" /><Table.Column title="解析" dataIndex="parse_status" render={value => <StatusTag status={String(value)} />} /><Table.Column title="索引" dataIndex="dify_status" render={value => <StatusTag status={String(value)} />} /><Table.Column title="上传时间" dataIndex="created_at" render={value => formatDate(String(value))} /></Table>
    </section>
    <section className="content-section"><div className="section-heading"><Typography.Title heading={5}>最近 Agent 执行</Typography.Title><Button theme="borderless" icon={<IconArrowRight />} iconPosition="right" onClick={() => navigate('/agent-runs')}>执行记录</Button></div>
      <Table<RecentRun> dataSource={data?.recent_runs || []} pagination={false} rowKey="run_id" size="small" empty="暂无执行记录"><Table.Column title="Agent" dataIndex="agent_name" /><Table.Column title="类型" dataIndex="operation" /><Table.Column title="状态" dataIndex="status" render={value => <StatusTag status={String(value)} />} /><Table.Column title="耗时" dataIndex="duration_ms" render={value => value == null ? '—' : `${value} ms`} /><Table.Column title="时间" dataIndex="created_at" render={value => formatDate(String(value))} /></Table>
    </section>
    <section className="content-section"><div className="section-heading"><Typography.Title heading={5}>最近工具调用</Typography.Title><Button theme="borderless" icon={<IconArrowRight />} iconPosition="right" onClick={() => navigate('/tools')}>工具中心</Button></div>
      <Table<RecentCall> dataSource={data?.recent_tool_calls || []} pagination={false} rowKey="id" size="small" empty="暂无工具调用"><Table.Column title="工具" dataIndex="tool_name" /><Table.Column title="知识库 ID" dataIndex="knowledge_base_id" /><Table.Column title="来源" dataIndex="channel" /><Table.Column title="状态" dataIndex="status" render={value => <StatusTag status={String(value)} />} /><Table.Column title="耗时" dataIndex="duration_ms" render={value => `${value} ms`} /><Table.Column title="时间" dataIndex="created_at" render={value => formatDate(String(value))} /></Table>
    </section>
  </div>;
}
