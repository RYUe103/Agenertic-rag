import { OverviewStats } from '../components/OverviewStats';
import { useEffect, useState } from 'react';
import { Button, Form, Modal, SideSheet, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconEdit, IconEyeOpened, IconPlus, IconRefresh } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import { formatDate } from '../utils/format';
import type { Health } from '../types';

interface Integration {
  id: string; name: string; client_type: string; owner: string; created_at: string;
  last_used_at: string | null; revoked_at: string | null; knowledge_base_ids: string[]; tool_names: string[];
}
interface Base {knowledge_base_id: string; name: string;}
interface Tool {name: string; description: string;}
interface Call {knowledge_base_id: string; tool_name: string; status: string; duration_ms: number; created_at: string;}

export function IntegrationsPage() {
  const {activeAgentId} = usePlatform();
  const [items, setItems] = useState<Integration[]>([]);
  const [bases, setBases] = useState<Base[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Integration | null>(null);
  const [saving, setSaving] = useState(false);
  const [token, setToken] = useState('');
  const [selected, setSelected] = useState<Integration | null>(null);
  const [calls, setCalls] = useState<Call[]>([]);
  const [mcpAvailable, setMcpAvailable] = useState(false);
  const kbId = activeAgentId.startsWith('@kb:') ? activeAgentId.slice(4) : '';

  async function load() {
    try {
      const [integrations, knowledgeBases, health] = await Promise.all([api<Integration[]>('/api/integrations'), api<Base[]>('/api/knowledge-bases'), api<Health>('/api/health')]);
      setItems(integrations); setBases(knowledgeBases); setMcpAvailable(health.mcp_available);
    } catch (error) {Toast.error(error instanceof Error ? error.message : '接入加载失败');}
  }
  useEffect(() => {void load();}, []);
  useEffect(() => {
    setTools([]);
    if (kbId) void api<Tool[]>(`/api/knowledge-bases/${encodeURIComponent(kbId)}/tools`).then(setTools).catch(() => Toast.error('无法加载授权工具'));
  }, [kbId]);

  async function create(values: Record<string, unknown>) {
    setSaving(true);
    try {
      const result = await api<{token: string}>('/api/integrations', {method: 'POST', body: JSON.stringify(values)});
      setCreating(false); setToken(result.token); await load();
    } catch (error) {Toast.error(error instanceof Error ? error.message : '创建失败');}
    finally {setSaving(false);}
  }
  async function showCalls(item: Integration) {
    try {setCalls(await api<Call[]>(`/api/integrations/${encodeURIComponent(item.id)}/calls`)); setSelected(item);}
    catch (error) {Toast.error(error instanceof Error ? error.message : '调用记录加载失败');}
  }
  async function savePermissions(values: Record<string, unknown>) {
    if (!editing) return;
    setSaving(true);
    try {
      await api(`/api/integrations/${encodeURIComponent(editing.id)}/permissions`, {method: 'PUT', body: JSON.stringify(values)});
      setEditing(null); Toast.success('授权范围已更新'); await load();
    } catch (error) {Toast.error(error instanceof Error ? error.message : '授权更新失败');}
    finally {setSaving(false);}
  }
  function change(item: Integration, action: 'rotate' | 'revoke') {
    Modal.confirm({title: action === 'rotate' ? '轮换密钥' : '撤销接入',
      content: action === 'rotate' ? `轮换后“${item.name}”的旧密钥立即失效。` : `撤销后“${item.name}”将无法继续调用。`,
      okText: '确认', cancelText: '取消', onOk: async () => {
        try {
          const result = await api<{token: string} | null>(`/api/integrations/${encodeURIComponent(item.id)}/${action}`, {method: 'POST'});
          if (result?.token) setToken(result.token);
          await load();
        } catch (error) {Toast.error(error instanceof Error ? error.message : '操作失败');}
      }});
  }

  return <div className="page-stack"><OverviewStats items={[{label:'接入配置', value:items.length, note:'当前可见接入'},{label:'有效接入', value:items.filter(x => !x.revoked_at).length, note:'尚未撤销'},{label:'可授权知识库', value:bases.length, note:'当前用户范围'},{label:'MCP', value:mcpAvailable ? '已启用' : '未启用', note:'服务能力'}]} />
    <section className="toolbar-row"><Typography.Text type="tertiary">{items.length} 个接入 · REST API · MCP {mcpAvailable ? '已启用' : '未启用'}</Typography.Text><div><Button icon={<IconRefresh />} onClick={() => void load()}>刷新</Button> <Button icon={<IconPlus />} theme="solid" type="primary" onClick={() => setCreating(true)}>新建接入</Button></div></section>
    <section className="content-section flush-section"><Table<Integration> dataSource={items} rowKey="id" pagination={false} empty="暂无接入">
      <Table.Column title="名称" dataIndex="name" render={(_, item) => <div className="primary-cell"><strong>{(item as Integration).name}</strong><small>{(item as Integration).id}</small></div>} />
      <Table.Column title="类型" dataIndex="client_type" width={130} /><Table.Column title="所有者" dataIndex="owner" width={120} />
      <Table.Column title="授权范围" render={(_, item) => {const value = item as Integration; return `${value.knowledge_base_ids.length} 个知识库 / ${value.tool_names.length} 个工具`;}} />
      <Table.Column title="最近调用" dataIndex="last_used_at" width={170} render={value => value ? formatDate(String(value)) : '尚无调用'} />
      <Table.Column title="状态" width={90} render={(_, item) => <Tag color={(item as Integration).revoked_at ? 'grey' : 'green'}>{(item as Integration).revoked_at ? '已撤销' : '启用'}</Tag>} />
      <Table.Column title="" width={290} render={(_, item) => {const value = item as Integration; return <><Button theme="borderless" icon={<IconEdit />} disabled={Boolean(value.revoked_at)} onClick={() => setEditing(value)}>授权</Button><Button theme="borderless" icon={<IconEyeOpened />} onClick={() => void showCalls(value)}>记录</Button><Button theme="borderless" disabled={Boolean(value.revoked_at)} onClick={() => change(value, 'rotate')}>轮换</Button><Button theme="borderless" type="danger" disabled={Boolean(value.revoked_at)} onClick={() => change(value, 'revoke')}>撤销</Button></>;}} />
    </Table></section>
    <SideSheet visible={creating} onCancel={() => setCreating(false)} title="新建接入" width={620}>
      <Form initValues={{client_type: 'custom', knowledge_base_ids: kbId ? [kbId] : [], tool_names: []}} onSubmit={values => void create(values as Record<string, unknown>)} labelPosition="top">
        <Form.Input field="name" label="接入名称" rules={[{required: true}]} />
        <Form.Select field="client_type" label="客户端类型"><Form.Select.Option value="opencode">OpenCode</Form.Select.Option><Form.Select.Option value="sansuo">三所小智</Form.Select.Option><Form.Select.Option value="mcp_client">MCP Client</Form.Select.Option><Form.Select.Option value="custom">其他 AI</Form.Select.Option></Form.Select>
        <Form.Select field="knowledge_base_ids" label="授权知识库" multiple rules={[{required: true}]}>{bases.map(base => <Form.Select.Option key={base.knowledge_base_id} value={base.knowledge_base_id}>{base.name}</Form.Select.Option>)}</Form.Select>
        <Form.Select field="tool_names" label="授权工具" multiple rules={[{required: true}]}>{tools.map(tool => <Form.Select.Option key={tool.name} value={tool.name}>{tool.name} · {tool.description}</Form.Select.Option>)}</Form.Select>
        <div className="drawer-footer"><Button onClick={() => setCreating(false)}>取消</Button><Button htmlType="submit" theme="solid" type="primary" loading={saving}>创建</Button></div>
      </Form>
    </SideSheet>
    <SideSheet visible={Boolean(editing)} onCancel={() => setEditing(null)} title={`修改授权 · ${editing?.name || ''}`} width={620}>
      {editing && <Form key={editing.id} initValues={{knowledge_base_ids: editing.knowledge_base_ids, tool_names: editing.tool_names}} onSubmit={values => void savePermissions(values as Record<string, unknown>)} labelPosition="top">
        <Form.Select field="knowledge_base_ids" label="授权知识库" multiple rules={[{required: true}]}>{bases.map(base => <Form.Select.Option key={base.knowledge_base_id} value={base.knowledge_base_id}>{base.name}</Form.Select.Option>)}</Form.Select>
        <Form.Select field="tool_names" label="授权工具" multiple rules={[{required: true}]}>{tools.map(tool => <Form.Select.Option key={tool.name} value={tool.name}>{tool.name} · {tool.description}</Form.Select.Option>)}</Form.Select>
        <div className="drawer-footer"><Button onClick={() => setEditing(null)}>取消</Button><Button htmlType="submit" theme="solid" type="primary" loading={saving}>保存授权</Button></div>
      </Form>}
    </SideSheet>
    <Modal visible={Boolean(token)} title="接入密钥" onCancel={() => setToken('')} onOk={() => setToken('')} okText="完成" cancelButtonProps={{style: {display: 'none'}}}>
      <Typography.Paragraph>密钥仅在创建或轮换时显示，请保存在客户端的安全配置中。</Typography.Paragraph><pre className="trace-json">{token}</pre>
      <Typography.Paragraph>REST 地址：<code>{window.location.origin}/api/external/v1/tools</code></Typography.Paragraph><Typography.Paragraph>OpenAI-compatible 地址：<code>{window.location.origin}/v1</code>。使用此密钥请求 <code>/models</code> 获取已授权的知识库模型 ID，聊天请求使用返回的 ID，仅支持非流式响应。</Typography.Paragraph>
      {mcpAvailable && <><Typography.Paragraph>MCP 配置：</Typography.Paragraph><pre className="trace-json">{JSON.stringify({mcpServers: {knowledge: {url: new URL('api/mcp/', window.location.href.split('#')[0]).href, headers: {Authorization: `Bearer ${token}`}}}}, null, 2)}</pre></>}
    </Modal>
    <SideSheet visible={Boolean(selected)} onCancel={() => setSelected(null)} title={`${selected?.name || ''} · 调用记录`} width={680}>
      <Table<Call> dataSource={calls} pagination={false} empty="暂无调用记录"><Table.Column title="时间" dataIndex="created_at" render={value => formatDate(String(value))} /><Table.Column title="知识库" dataIndex="knowledge_base_id" /><Table.Column title="工具" dataIndex="tool_name" /><Table.Column title="结果" dataIndex="status" /><Table.Column title="耗时" dataIndex="duration_ms" render={value => `${value} ms`} /></Table>
    </SideSheet>
  </div>;
}
