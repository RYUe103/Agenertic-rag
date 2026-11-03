import { OverviewStats } from '../components/OverviewStats';
import { useEffect, useState } from 'react';
import { Button, DatePicker, Input, Select, Table, Tag, Toast } from '@douyinfe/semi-ui';
import { IconRefresh } from '@douyinfe/semi-icons';
import { api, queryString } from '../api/client';
import { formatDate } from '../utils/format';

interface AuditEvent {id: number; created_at: string; actor: string; action: string; resource_type: string; resource_id: string; status_code: number;}
interface AuditPageData {items: AuditEvent[]; page: number; page_size: number; total: number;}
const actions: Record<string, string> = {login: '登录', logout: '退出', create: '创建', update: '更新', delete: '删除', read: '读取', invoke: '调用', permissions: '授权变更', rotate: '轮换', revoke: '撤销'};
const resources: Record<string, string> = {session: '会话', knowledge_base: '知识库', document: '文档', document_task: '文档任务', tool: '工具', integration: 'AI 接入', configuration: '配置'};

export function AuditPage() {
  const [data, setData] = useState<AuditPageData>({items: [], page: 1, page_size: 30, total: 0});
  const [page, setPage] = useState(1);
  const [actor, setActor] = useState('');
  const [action, setAction] = useState('');
  const [resourceType, setResourceType] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api<AuditPageData>(`/api/admin/audit${queryString({page, page_size: 30, actor, action, resource_type: resourceType, start, end})}`)
      .then(result => {if (active) setData(result);})
      .catch(error => {if (active) Toast.error(error instanceof Error ? error.message : '审计日志加载失败');})
      .finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [page, actor, action, resourceType, start, end, refresh]);

  return <div className="page-stack"><OverviewStats items={[{label:'匹配记录', value:data.total, note:'当前筛选范围'},{label:'本页记录', value:data.items.length, note:'当前列表'},{label:'本页异常', value:data.items.filter(x => x.status_code >= 400).length, note:'HTTP 状态 ≥ 400'},{label:'本页操作者', value:new Set(data.items.map(x => x.actor)).size, note:'按操作者统计'}]} />
    <section className="toolbar-row"><div className="filter-cluster filter-wrap">
      <Input placeholder="完整用户 ID" value={actor} onChange={value => {setActor(value); setPage(1);}} showClear style={{width: 180}} />
      <Select value={resourceType} onChange={value => {setResourceType(String(value)); setPage(1);}} style={{width: 140}}><Select.Option value="">全部资源</Select.Option>{Object.entries(resources).map(([key, label]) => <Select.Option key={key} value={key}>{label}</Select.Option>)}</Select>
      <Select value={action} onChange={value => {setAction(String(value)); setPage(1);}} style={{width: 140}}><Select.Option value="">全部操作</Select.Option>{Object.entries(actions).map(([key, label]) => <Select.Option key={key} value={key}>{label}</Select.Option>)}</Select>
      <DatePicker type="dateRange" placeholder={['开始日期', '结束日期']} onChange={(value: unknown) => {const range = Array.isArray(value) ? value : []; const first = range[0] ? new Date(range[0]) : null; const last = range[1] ? new Date(range[1]) : null; if (first) first.setHours(0, 0, 0, 0); if (last) last.setHours(23, 59, 59, 999); setStart(first?.toISOString() || ''); setEnd(last?.toISOString() || ''); setPage(1);}} />
    </div><Button icon={<IconRefresh />} loading={loading} onClick={() => setRefresh(value => value + 1)}>刷新</Button><Button onClick={() => void api<{deleted: number}>('/api/admin/audit/prune?retention_days=180', {method: 'POST'}).then(result => {Toast.success(`已清理 ${result.deleted} 条旧审计记录`); setRefresh(value => value + 1);}).catch(error => Toast.error(error instanceof Error ? error.message : '清理失败'))}>清理 180 天前记录</Button></section>
    <section className="content-section flush-section"><Table<AuditEvent> dataSource={data.items} loading={loading} rowKey="id" empty="暂无审计记录" pagination={{currentPage: page, pageSize: 30, total: data.total, onPageChange: setPage}}>
      <Table.Column title="时间" dataIndex="created_at" width={180} render={value => formatDate(String(value))} />
      <Table.Column title="操作者" dataIndex="actor" />
      <Table.Column title="操作" dataIndex="action" width={110} render={value => actions[String(value)] || String(value)} />
      <Table.Column title="资源类型" dataIndex="resource_type" width={120} render={value => resources[String(value)] || String(value)} />
      <Table.Column title="资源 ID" dataIndex="resource_id" render={value => value ? String(value) : '—'} />
      <Table.Column title="结果" dataIndex="status_code" width={110} render={value => <Tag color={Number(value) < 400 ? 'green' : 'red'}>HTTP {String(value)}</Tag>} />
    </Table></section>
  </div>;
}
