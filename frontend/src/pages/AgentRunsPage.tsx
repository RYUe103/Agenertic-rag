import { OverviewStats } from '../components/OverviewStats';
import { useEffect, useState } from 'react';
import { Button, Select, SideSheet, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconEyeOpened, IconRefresh } from '@douyinfe/semi-icons';
import { api, queryString } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import { formatDate } from '../utils/format';

interface RunEvent {sequence: number; kind: string; title: string; status: string; round?: number | null; tool_name?: string | null; evidence_count?: number | null;}
interface AgentRun {
  run_id: string; owner_id: string; knowledge_base_id: string; agent_id: string; agent_name: string;
  operation: string; status: string; created_at: string; completed_at: string | null;
  duration_ms: number | null; tool_calls: number | null; evidence_count: number | null;
  evidence_insufficient: boolean | null; trace: RunEvent[];
}

const statusLabels: Record<string, string> = {running: '进行中', waiting_approval: '等待审批', resumed: '已恢复', succeeded: '已完成', failed: '失败', rejected: '排队超时', request_cancelled: '请求已中断'};

export function AgentRunsPage() {
  const {agents} = usePlatform();
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState('');
  const [operation, setOperation] = useState('');
  const [knowledgeBaseId, setKnowledgeBaseId] = useState('');
  const [selected, setSelected] = useState<AgentRun | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const result = await api<{items: AgentRun[]; total: number}>(`/api/agent-runs/page${queryString({page, page_size: 30, status, operation, knowledge_base_id: knowledgeBaseId})}`);
      setRuns(result.items); setTotal(result.total);
    }
    catch (error) {Toast.error(error instanceof Error ? error.message : '执行记录加载失败');}
    finally {setLoading(false);}
  }
  useEffect(() => {void load();}, [page, status, operation, knowledgeBaseId]);

  return <div className="page-stack"><OverviewStats items={[{label:'匹配执行', value:total, note:'当前筛选范围'},{label:'本页已完成', value:runs.filter(x => x.status === 'succeeded').length, note:'当前列表'},{label:'本页失败', value:runs.filter(x => x.status === 'failed').length, note:'当前列表'},{label:'本页待审批', value:runs.filter(x => x.status === 'waiting_approval').length, note:'当前列表'}]} />
    <section className="toolbar-row"><div className="filter-cluster">
      <Select value={knowledgeBaseId} style={{width: 180}} onChange={value => {setKnowledgeBaseId(String(value)); setPage(1);}}><Select.Option value="">全部知识库</Select.Option>{agents.map(item => <Select.Option key={item.agent_id} value={item.agent_id.replace(/^@kb:/, '')}>{item.name}</Select.Option>)}</Select>
      <Select value={operation} style={{width: 120}} onChange={value => {setOperation(String(value)); setPage(1);}}><Select.Option value="">全部类型</Select.Option><Select.Option value="ask">问答</Select.Option><Select.Option value="retrieve">检索</Select.Option></Select>
      <Select value={status} style={{width: 140}} onChange={value => {setStatus(String(value)); setPage(1);}}><Select.Option value="">全部状态</Select.Option>{Object.entries(statusLabels).map(([key, label]) => <Select.Option key={key} value={key}>{label}</Select.Option>)}</Select>
      <Typography.Text type="tertiary">共 {total} 条</Typography.Text></div><Button icon={<IconRefresh />} loading={loading} onClick={() => void load()}>刷新</Button></section>
    <section className="content-section flush-section"><Table<AgentRun> dataSource={runs} loading={loading} rowKey="run_id" pagination={{currentPage: page, pageSize: 30, total, onPageChange: setPage}} empty="暂无执行记录">
      <Table.Column title="Agent" dataIndex="agent_name" render={(_, row) => {const item = row as AgentRun; return <div className="primary-cell"><strong>{item.agent_name}</strong><small>{item.run_id}</small></div>;}} />
      <Table.Column title="知识库 ID" dataIndex="knowledge_base_id" /><Table.Column title="类型" dataIndex="operation" width={100} render={value => value === 'ask' ? '问答' : '检索'} />
      <Table.Column title="状态" dataIndex="status" width={110} render={value => <Tag color={value === 'succeeded' ? 'green' : value === 'failed' || value === 'rejected' ? 'red' : 'grey'}>{statusLabels[String(value)] || String(value)}</Tag>} />
      <Table.Column title="工具调用" dataIndex="tool_calls" width={110} render={value => value == null ? '—' : String(value)} />
      <Table.Column title="耗时" dataIndex="duration_ms" width={110} render={value => value == null ? '—' : `${value} ms`} />
      <Table.Column title="开始时间" dataIndex="created_at" width={180} render={value => formatDate(String(value))} />
      <Table.Column title="" width={90} render={(_, row) => <Button icon={<IconEyeOpened />} theme="borderless" onClick={() => setSelected(row as AgentRun)}>详情</Button>} />
    </Table></section>
    <SideSheet visible={Boolean(selected)} title="执行详情" width={680} onCancel={() => setSelected(null)}>
      {selected && <><Typography.Paragraph><strong>{selected.agent_name}</strong> · {statusLabels[selected.status] || selected.status} · {selected.knowledge_base_id}</Typography.Paragraph>
        <Typography.Paragraph>工具调用 {selected.tool_calls ?? '—'} 次 · 证据 {selected.evidence_count ?? '—'} 条 · {selected.duration_ms == null ? '耗时未知' : `${selected.duration_ms} ms`}</Typography.Paragraph>
        {selected.status === 'waiting_approval' && <Button theme="solid" onClick={() => void api(`/api/agent-runs/${selected.run_id}/resume`, {method: 'POST'}).then(() => {Toast.success('Agent 已恢复执行'); setSelected(null); void load();}).catch(error => Toast.error(error instanceof Error ? error.message : '恢复失败'))}>审批后恢复</Button>}
        <Typography.Title heading={6}>执行轨迹</Typography.Title>
        <Table<RunEvent> dataSource={selected.trace} rowKey="sequence" pagination={false} empty="暂无轨迹"><Table.Column title="阶段" dataIndex="title" /><Table.Column title="工具" dataIndex="tool_name" render={value => value ? String(value) : '—'} /><Table.Column title="状态" dataIndex="status" /><Table.Column title="证据" dataIndex="evidence_count" render={value => value == null ? '—' : String(value)} /></Table>
      </>}
    </SideSheet>
  </div>;
}
