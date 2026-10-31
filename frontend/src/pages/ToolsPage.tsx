import { useEffect, useState } from 'react';
import { Button, SideSheet, Switch, Table, Tag, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconEyeOpened, IconPlay } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';

interface Tool {name: string; tool_name?: string; description: string; category: string; read_only: boolean; input_schema: Record<string, unknown>; output_schema?: Record<string, unknown>; required_permission?: string; requires_approval?: boolean; enabled?: boolean; statistics?: {calls: number; succeeded: number; last_called_at?: string | null};}
interface Call {actor: string; channel: string; status: string; duration_ms: number; created_at: string;}
interface Approval {id: string; actor: string; knowledge_base_id: string; tool_name: string; arguments: Record<string, unknown>; status: string; expires_at: string;}

export function ToolsPage() {
  const {activeAgentId, session} = usePlatform();
  const [tools, setTools] = useState<Tool[]>([]);
  const [selected, setSelected] = useState<Tool | null>(null);
  const [argumentsText, setArgumentsText] = useState('{}');
  const [result, setResult] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [calls, setCalls] = useState<Call[]>([]);
  const [registryTools, setRegistryTools] = useState<Tool[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [ownApprovalIds, setOwnApprovalIds] = useState<string[]>([]);
  const [approvalId, setApprovalId] = useState('');
  const knowledgeBaseId = activeAgentId.startsWith('@kb:') ? activeAgentId.slice(4) : '';

  useEffect(() => {
    setSelected(null); setResult(null); setTools([]);
    if (!knowledgeBaseId) return;
    api<Tool[]>(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/tools`).then(setTools)
      .catch(error => Toast.error(error instanceof Error ? error.message : '工具加载失败'));
    if (session.is_admin) api<Tool[]>('/api/admin/tools').then(setRegistryTools).catch(error => Toast.error(error instanceof Error ? error.message : '工具注册表加载失败'));
    void refreshApprovals();
  }, [knowledgeBaseId, session.is_admin]);

  async function refreshApprovals() {
    try { const mine = await api<Approval[]>('/api/tool-approvals'); setOwnApprovalIds(mine.map(item => item.id)); setApprovals(session.is_admin ? await api<Approval[]>('/api/admin/tool-approvals') : mine); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '审批记录加载失败'); }
  }
  async function toggleRegistry(tool: Tool, change: {enabled?: boolean; requires_approval?: boolean}) {
    try { await api(`/api/admin/tools/${encodeURIComponent(tool.tool_name || tool.name)}`, {method: 'PUT', body: JSON.stringify({enabled: tool.enabled, ...change})}); setRegistryTools(items => items.map(item => item.name === tool.name ? {...item, ...change} : item)); if (knowledgeBaseId) setTools(await api<Tool[]>(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/tools`)); Toast.success('工具状态已更新'); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '工具状态更新失败'); }
  }

  async function run(requestApproval = false) {
    if (!selected) return;
    setLoading(true);
    try {
      const args: unknown = JSON.parse(argumentsText);
      if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('参数必须是 JSON 对象');
      const base = `/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/tools/${encodeURIComponent(selected.name)}`;
      if (requestApproval) {
        const approval = await api<Approval>(`${base}/approvals`, {method: 'POST', body: JSON.stringify(args)});
        setApprovalId(approval.id); await refreshApprovals(); Toast.success('审批申请已提交');
      } else {
        setResult(await api(`${base}/test`, {method: 'POST', headers: approvalId ? {'X-Approval-Id': approvalId} : {}, body: JSON.stringify(args)}));
        setApprovalId(''); await refreshApprovals();
      }
    } catch (error) { Toast.error(error instanceof Error ? error.message : '工具调用失败'); }
    finally { setLoading(false); }
  }

  function downloadExport() {
    const value = result as {content?: string; content_base64?: string; filename?: string; content_type?: string} | null;
    if (!value?.content && !value?.content_base64) return;
    let blob: Blob;
    if (value.content_base64) {
      const bytes = Uint8Array.from(atob(value.content_base64), character => character.charCodeAt(0));
      blob = new Blob([bytes], {type: value.content_type || 'application/octet-stream'});
    } else blob = new Blob([value.content || ''], {type: value.content_type || 'text/plain;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = value.filename || 'export.csv'; anchor.click();
    URL.revokeObjectURL(url);
  }

  return <div className="page-stack">
    <section className="toolbar-row"><Typography.Text type="tertiary">当前知识库可用的 {tools.length} 个工具</Typography.Text></section>
    {session.is_admin && <section className="content-section flush-section"><Typography.Title heading={5}>工具注册与治理</Typography.Title><Table<Tool> dataSource={registryTools} rowKey="name" pagination={false}><Table.Column title="工具" dataIndex="name" render={value => <code>{String(value)}</code>} /><Table.Column title="分类" dataIndex="category" /><Table.Column title="所需权限" dataIndex="required_permission" /><Table.Column title="人工审批" dataIndex="requires_approval" render={(value, item) => <Switch checked={Boolean(value)} disabled={!(item as Tool).read_only} onChange={checked => void toggleRegistry(item as Tool, {requires_approval: checked})} />} /><Table.Column title="启用" dataIndex="enabled" render={(value, item) => <Switch checked={Boolean(value)} onChange={checked => void toggleRegistry(item as Tool, {enabled: checked})} />} /></Table></section>}
    <section className="content-section flush-section"><div className="toolbar-row"><Typography.Title heading={5}>{session.is_admin ? '工具审批' : '我的审批'}</Typography.Title><div><Button onClick={() => void refreshApprovals()}>刷新</Button>{session.is_admin && <Button onClick={() => void api<{deleted: number}>('/api/admin/tool-approvals/cleanup', {method: 'POST'}).then(result => {Toast.success(`已清理 ${result.deleted} 条过期审批`); return refreshApprovals();}).catch(error => Toast.error(error.message))}>清理过期记录</Button>}</div></div><Typography.Text type="tertiary">审批在申请后 15 分钟过期，每次批准仅可执行一次。Agent 遇到需要审批的工具时会暂停，批准后可在执行记录中恢复。</Typography.Text><Table<Approval> dataSource={approvals} rowKey="id" pagination={{pageSize: 10}} empty="暂无审批申请"><Table.Column title="工具 / 知识库" render={(_, item) => <span>{item.tool_name}<br />{item.knowledge_base_id}</span>} /><Table.Column title="申请人" dataIndex="actor" /><Table.Column title="参数" render={(_, item) => <pre className="trace-json">{JSON.stringify(item.arguments, null, 2)}</pre>} /><Table.Column title="状态" dataIndex="status" /><Table.Column title="到期时间" dataIndex="expires_at" /><Table.Column title="操作" render={(_, item) => <>{session.is_admin && item.status === 'pending' && <><Button size="small" onClick={() => void api(`/api/admin/tool-approvals/${item.id}/approve`, {method: 'POST'}).then(refreshApprovals).catch(error => Toast.error(error.message))}>批准</Button><Button size="small" onClick={() => void api(`/api/admin/tool-approvals/${item.id}/reject`, {method: 'POST'}).then(refreshApprovals).catch(error => Toast.error(error.message))}>拒绝</Button></>}{item.status === 'approved' && ownApprovalIds.includes(item.id) && item.knowledge_base_id === knowledgeBaseId && <Button size="small" onClick={() => {const tool = tools.find(value => value.name === item.tool_name); if (tool) {setSelected(tool); if (item.tool_name !== 'upload_document') setArgumentsText(JSON.stringify(item.arguments, null, 2)); else Toast.info('请使用原始上传参数，审批仅保存文件摘要'); setApprovalId(item.id); setResult(null);}}}>用于测试</Button>}</>} /></Table></section>
    <section className="content-section flush-section"><Table<Tool> dataSource={tools} rowKey="name" pagination={false} empty="当前知识库没有可用工具">
      <Table.Column title="工具" dataIndex="name" render={value => <code>{String(value)}</code>} />
      <Table.Column title="用途" dataIndex="description" />
      <Table.Column title="类型" dataIndex="category" width={120} />
      <Table.Column title="权限" width={100} render={(_, item) => <Tag color={item.read_only ? 'green' : 'orange'}>{item.read_only ? '只读' : '写入'}</Tag>} />
      <Table.Column title="调用统计" width={130} render={(_, item) => `${item.statistics?.calls || 0} 次 / ${item.statistics?.succeeded || 0} 成功`} />
      <Table.Column title="" width={90} render={(_, item) => <Button icon={<IconEyeOpened />} theme="borderless" onClick={() => {setSelected(item as Tool); setArgumentsText('{}'); setApprovalId(''); setResult(null);}}>详情</Button>} />
    </Table></section>
    <SideSheet visible={Boolean(selected)} title={selected?.name} width={680} onCancel={() => setSelected(null)}>
      <Typography.Paragraph>{selected?.description}</Typography.Paragraph>
      <Typography.Title heading={6}>参数 Schema</Typography.Title><pre className="trace-json">{JSON.stringify(selected?.input_schema, null, 2)}</pre>
      <Typography.Title heading={6}>返回 Schema</Typography.Title><pre className="trace-json">{JSON.stringify(selected?.output_schema, null, 2)}</pre>
      <Typography.Title heading={6}>测试参数</Typography.Title><TextArea value={argumentsText} onChange={setArgumentsText} autosize rows={5} />
      {selected && (!selected.read_only || selected.requires_approval) && <Button loading={loading} onClick={() => void run(true)}>申请审批</Button>}
      {approvalId && <Typography.Paragraph>当前审批编号：{approvalId}</Typography.Paragraph>}
      <Button icon={<IconPlay />} theme="solid" type="primary" loading={loading} onClick={() => void run()}>运行测试</Button>
      <Button icon={<IconEyeOpened />} theme="borderless" onClick={() => {if (selected) api<Call[]>(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/tools/${encodeURIComponent(selected.name)}/calls`).then(setCalls).catch(error => Toast.error(error.message));}}>最近调用</Button>
      {calls.length > 0 && <Table<Call> dataSource={calls} rowKey="created_at" pagination={false} size="small"><Table.Column title="时间" dataIndex="created_at" /><Table.Column title="来源" dataIndex="channel" /><Table.Column title="操作者" dataIndex="actor" /><Table.Column title="状态" dataIndex="status" /><Table.Column title="耗时" dataIndex="duration_ms" render={value => `${value} ms`} /></Table>}
      {result !== null && <><Typography.Title heading={6}>结果</Typography.Title>{(selected?.name === 'export_csv' || selected?.name === 'export_excel') && <Button onClick={downloadExport}>下载导出文件</Button>}<pre className="trace-json">{JSON.stringify(result, null, 2)}</pre></>}
    </SideSheet>
  </div>;
}
