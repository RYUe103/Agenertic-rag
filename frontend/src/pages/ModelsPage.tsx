import { useEffect, useState } from 'react';
import { Button, Form, Modal, SideSheet, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconDelete, IconEdit, IconPlus, IconRefresh } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';

interface ModelConnection {
  model_connection_id: string;
  name: string;
  base_url: string;
  model: string;
  api_style: string;
  timeout_seconds: number;
  priority: number;
  enabled: boolean;
  api_key_configured: boolean;
}

export function ModelsPage() {
  const {session} = usePlatform();
  const [items, setItems] = useState<ModelConnection[]>([]);
  const [editing, setEditing] = useState<ModelConnection | null | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  async function load() {setLoading(true); try {setItems(await api<ModelConnection[]>('/api/admin/model-connections'));} catch (error) {Toast.error(error instanceof Error ? error.message : '模型连接加载失败');} finally {setLoading(false);}}
  useEffect(() => {void load();}, []);

  async function save(values: Record<string, unknown>) {
    setSaving(true);
    try {
      const id = String(values.model_connection_id);
      await api(editing ? `/api/admin/model-connections/${encodeURIComponent(id)}` : '/api/admin/model-connections', {method: editing ? 'PUT' : 'POST', body: JSON.stringify({...values, api_key: values.api_key || null})});
      Toast.success('模型连接已保存'); setEditing(undefined); await load();
    } catch (error) {Toast.error(error instanceof Error ? error.message : '保存失败');} finally {setSaving(false);}
  }
  async function test(values: Record<string, unknown>) {
    try {const result = await api<{ok: boolean; message: string; latency_ms?: number}>('/api/admin/model-connections/test', {method: 'POST', body: JSON.stringify({...values, api_key: values.api_key || null})}); result.ok ? Toast.success(`${result.message}${result.latency_ms ? ` · ${result.latency_ms}ms` : ''}`) : Toast.error(result.message);} catch (error) {Toast.error(error instanceof Error ? error.message : '连接测试失败');}
  }
  function remove(item: ModelConnection) {Modal.confirm({title: '删除模型连接', content: `确定删除“${item.name}”？请先检查 Agent 的模型链配置。`, okText: '确认删除', cancelText: '取消', okButtonProps: {type: 'danger'}, onOk: async () => {await api(`/api/admin/model-connections/${encodeURIComponent(item.model_connection_id)}`, {method: 'DELETE'}); await load();}});}
  if (!session.is_admin) return <div className="permission-state"><strong>需要管理员权限</strong><span>模型连接由 sansuokb 统一配置，域账号用户不可修改。</span></div>;
  const initialValues: Record<string, unknown> = editing ? {...editing} : {model_connection_id: '', name: '', base_url: '', model: '', api_style: 'chat_completions', timeout_seconds: 30, priority: 100, enabled: true};
  return <div className="page-stack"><section className="toolbar-row"><Typography.Text type="tertiary">数字越小，在模型故障转移链中优先级越高</Typography.Text><div><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button> <Button icon={<IconPlus />} theme="solid" type="primary" onClick={() => {setDraft({}); setEditing(null);}}>新建连接</Button></div></section><section className="content-section flush-section"><Table<ModelConnection> dataSource={items} loading={loading} rowKey="model_connection_id" pagination={false}>
    <Table.Column title="连接" dataIndex="name" render={(_, record) => { const item = record as ModelConnection; return <div className="primary-cell"><strong>{item.name}</strong><small>{item.base_url}</small></div>; }} /><Table.Column title="模型" dataIndex="model" /><Table.Column title="接口" dataIndex="api_style" width={160} /><Table.Column title="优先级" dataIndex="priority" width={100} /><Table.Column title="API Key" dataIndex="api_key_configured" width={100} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已配置' : '未配置'}</Tag>} /><Table.Column title="状态" dataIndex="enabled" width={90} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已启用' : '已禁用'}</Tag>} /><Table.Column title="" width={110} render={(_, record) => { const item = record as ModelConnection; return <><Button icon={<IconEdit />} theme="borderless" onClick={() => {setDraft({...item}); setEditing(item);}} /><Button icon={<IconDelete />} theme="borderless" type="danger" onClick={() => remove(item)} /></>; }} />
  </Table></section><SideSheet visible={editing !== undefined} onCancel={() => setEditing(undefined)} title={editing ? `编辑·${editing.name}` : '新建模型连接'} width={620}><Form key={editing?.model_connection_id || 'new'} initValues={initialValues} onValueChange={values => setDraft(values as Record<string, unknown>)} onSubmit={values => void save(values as Record<string, unknown>)} labelPosition="top">
    <div className="form-grid two-columns"><Form.Input field="model_connection_id" label="连接 ID" disabled={Boolean(editing)} rules={[{required: true}]} /><Form.Input field="name" label="名称" rules={[{required: true}]} /><Form.Input field="base_url" label="OpenAI 兼容 Base URL" className="full-span" rules={[{required: true}]} /><Form.Input field="model" label="模型名称" rules={[{required: true}]} /><Form.Select field="api_style" label="接口类型"><Form.Select.Option value="chat_completions">Chat Completions</Form.Select.Option><Form.Select.Option value="responses">Responses</Form.Select.Option></Form.Select><Form.Input field="api_key" label="API Key" mode="password" placeholder={editing?.api_key_configured ? '留空保持当前密钥' : '输入 API Key'} /><Form.InputNumber field="timeout_seconds" label="超时（秒）" min={1} max={300} /><Form.InputNumber field="priority" label="优先级" min={1} max={10000} /><Form.Switch field="enabled" label="启用连接" /></div><div className="drawer-footer"><Button onClick={() => void test({...initialValues, ...draft})}>测试连接</Button><span /><Button onClick={() => setEditing(undefined)}>取消</Button><Button htmlType="submit" theme="solid" type="primary" loading={saving}>保存配置</Button></div>
  </Form></SideSheet></div>;
}
