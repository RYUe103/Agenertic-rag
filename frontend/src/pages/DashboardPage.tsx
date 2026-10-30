import { useEffect, useState } from 'react';
import { Button, Progress, Skeleton, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconArrowRight, IconRefresh } from '@douyinfe/semi-icons';
import { useNavigate } from 'react-router-dom';
import { api, queryString } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import { StatusTag } from '../components/StatusTag';
import type { Health, KnowledgeBase, SourceStats, TaskPage, TaskSummary } from '../types';
import { formatDate, taskProgress } from '../utils/format';

export function DashboardPage() {
  const navigate = useNavigate();
  const {activeAgentId} = usePlatform();
  const [health, setHealth] = useState<Health | null>(null);
  const [stats, setStats] = useState<SourceStats | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const [healthValue, bases, taskPage, sourceStats] = await Promise.all([
        api<Health>('/api/health'),
        api<KnowledgeBase[]>('/api/admin/knowledge-bases'),
        api<TaskPage>(`/api/tasks${queryString({page_size: 6, agent_id: activeAgentId || undefined})}`),
        activeAgentId ? api<SourceStats>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/stats`) : Promise.resolve(null),
      ]);
      setHealth(healthValue); setKnowledgeBases(bases); setTasks(taskPage.items); setStats(sourceStats);
    } catch (error) {
      Toast.error(error instanceof Error ? error.message : '无法加载总览');
    } finally { setLoading(false); }
  }

  useEffect(() => { void load(); }, [activeAgentId]);

  if (loading && !health) return <Skeleton placeholder={<Skeleton.Paragraph rows={8} />} active />;

  const metrics = [
    {label: '知识库', value: knowledgeBases.length, note: `${knowledgeBases.filter(item => item.enabled).length} 个已启用`},
    {label: '文档总数', value: stats?.total ?? '—', note: `${stats?.indexed ?? 0} 份已建立索引`},
    {label: '处理中', value: stats?.processing ?? '—', note: '解析或索引未完成'},
    {label: '异常文档', value: stats?.failed ?? '—', note: '需要检查或重试', danger: Boolean(stats?.failed)},
  ];

  return <div className="page-stack dashboard-page">
    <section className="metric-strip" aria-label="核心指标">
      {metrics.map(item => <div key={item.label} className={`metric-item${item.danger ? ' metric-danger' : ''}`}>
        <span>{item.label}</span><strong>{item.value}</strong><small>{item.note}</small>
      </div>)}
    </section>

    <section className="content-section service-overview">
      <div className="section-heading"><div><Typography.Title heading={5}>服务运行状态</Typography.Title><Typography.Text type="tertiary">来自后端实时健康检查</Typography.Text></div><Button icon={<IconRefresh />} theme="borderless" onClick={() => void load()}>刷新</Button></div>
      <div className="service-line">
        <div><span className="live-dot" /><strong>API 服务</strong><Tag color="green" size="small">正常</Tag></div>
        <div><span>LLM</span><StatusTag status={health?.llm_configured ? 'success' : 'disabled'} /></div>
        <div><span>外部 API</span><StatusTag status={health?.external_api_configured ? 'success' : 'disabled'} /></div>
        <div><span>问答并发</span><strong>{health?.ask_active ?? 0} / {health?.ask_concurrency_limit ?? '—'}</strong></div>
        <div><span>版本</span><code>{health?.build_version || '—'}</code></div>
      </div>
    </section>

    <section className="content-section">
      <div className="section-heading"><div><Typography.Title heading={5}>最近任务</Typography.Title><Typography.Text type="tertiary">入库与删除队列的最新进展</Typography.Text></div><Button theme="borderless" icon={<IconArrowRight />} iconPosition="right" onClick={() => navigate('/tasks')}>查看全部</Button></div>
      <Table<TaskSummary> dataSource={tasks} pagination={false} rowKey="task_id" size="small" empty="暂无任务记录">
        <Table.Column title="任务" dataIndex="title" render={(_, item) => <div className="primary-cell"><strong>{item.title}</strong><small>{item.task_id}</small></div>} />
        <Table.Column title="状态" dataIndex="status" width={110} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="进度" width={180} render={(_, item) => <div className="progress-cell"><Progress percent={taskProgress(item.completed, item.total)} size="small" showInfo={false} /><span>{item.completed}/{item.total}</span></div>} />
        <Table.Column title="更新时间" dataIndex="updated_at" width={170} render={value => formatDate(String(value))} />
      </Table>
    </section>
  </div>;
}
