import { useEffect, useState } from 'react';
import { Button, Form, Modal, SideSheet, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconDelete, IconEdit, IconPlus, IconRefresh } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import type { KnowledgeBase } from '../types';

type FormValues = Record<string, unknown>;

function payload(values: FormValues, existing: KnowledgeBase | null) {
  const result: Record<string, unknown> = {...(existing || {}), ...values};
  [
    'owner_username', 'owner_user_id', 'api_key_configured', 'api_key_hint', 'answer_prompt_custom',
    'qwenvl_api_key_configured', 'source_platform_api_key_configured', 'workflow_api_key_configured',
  ].forEach(key => delete result[key]);
  result.api_key = values.api_key || null;
  result.qwenvl_api_key = null;
  result.source_platform_api_key = null;
  result.workflow_api_key = null;
  result.metadata_fields ||= [];
  return result;
}

export function KnowledgePage() {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<KnowledgeBase | null | undefined>(undefined);
  const [saving, setSaving] = useState(false);

  async function load() {
    setLoading(true);
    try { setItems(await api<KnowledgeBase[]>('/api/admin/knowledge-bases')); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '知识库加载失败'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function save(values: FormValues) {
    setSaving(true);
    try {
      const id = String(values.knowledge_base_id);
      await api<KnowledgeBase>(editing ? `/api/admin/knowledge-bases/${encodeURIComponent(id)}` : '/api/admin/knowledge-bases', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload(values, editing || null)),
      });
      Toast.success(editing ? '知识库配置已保存' : '知识库已创建'); setEditing(undefined); await load();
    } catch (error) { Toast.error(error instanceof Error ? error.message : '保存失败'); }
    finally { setSaving(false); }
  }

  function remove(item: KnowledgeBase) {
    Modal.confirm({title: '删除知识库连接', content: `确定删除“${item.name}”的平台配置？请先确认关联 Agent 和文档的处理方式。`, okText: '确认删除', cancelText: '取消', okButtonProps: {type: 'danger'}, onOk: async () => {await api(`/api/admin/knowledge-bases/${encodeURIComponent(item.knowledge_base_id)}`, {method: 'DELETE'}); Toast.success('知识库配置已删除'); await load();}});
  }

  const initialValues = editing || {
    knowledge_base_id: '', name: '', description: '', base_url: 'https://api.dify.ai', dataset_id: '',
    search_method: 'semantic_search', chunking_mode: 'automatic', chunk_separator: '\n__SPLIT__\n',
    chunk_max_tokens: 800, pdf_parser: 'default', enabled: true,
  };

  return <div className="page-stack">
    <section className="toolbar-row"><Typography.Text type="tertiary">共 {items.length} 个知识库连接</Typography.Text><div><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button> <Button icon={<IconPlus />} theme="solid" type="primary" onClick={() => setEditing(null)}>新建知识库</Button></div></section>
    <section className="content-section flush-section"><Table<KnowledgeBase> dataSource={items} loading={loading} rowKey="knowledge_base_id" pagination={false}>
      <Table.Column title="知识库" dataIndex="name" render={(_, record) => { const item = record as KnowledgeBase; return <div className="primary-cell"><strong>{item.name}</strong><small>{item.description || item.knowledge_base_id}</small></div>; }} />
      <Table.Column title="所有者" dataIndex="owner_username" width={130} />
      <Table.Column title="解析器" dataIndex="pdf_parser" width={120} render={value => <Tag>{String(value)}</Tag>} />
      <Table.Column title="分块" dataIndex="chunking_mode" width={120} render={(_, record) => { const item = record as KnowledgeBase; return item.chunking_mode === 'automatic' ? '自动分块' : `自定义 · ${item.chunk_max_tokens}`; }} />
      <Table.Column title="Dify" dataIndex="api_key_configured" width={100} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已配置' : '未配置'}</Tag>} />
      <Table.Column title="状态" dataIndex="enabled" width={90} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已启用' : '已禁用'}</Tag>} />
      <Table.Column title="" width={110} render={(_, record) => { const item = record as KnowledgeBase; return <><Button icon={<IconEdit />} theme="borderless" onClick={() => setEditing(item)} aria-label="编辑" /><Button icon={<IconDelete />} type="danger" theme="borderless" onClick={() => remove(item)} aria-label="删除" /></>; }} />
    </Table></section>
    <SideSheet visible={editing !== undefined} onCancel={() => setEditing(undefined)} title={editing ? `编辑·${editing.name}` : '新建知识库'} width={640}>
      <Form key={editing?.knowledge_base_id || 'new'} initValues={initialValues} onSubmit={values => void save(values as FormValues)} labelPosition="top">
        <div className="form-grid two-columns">
          <Form.Input field="knowledge_base_id" label="知识库 ID" disabled={Boolean(editing)} rules={[{required: true}]} />
          <Form.Input field="name" label="名称" rules={[{required: true}]} />
          <Form.TextArea field="description" label="描述" className="full-span" />
          <Form.Input field="base_url" label="Dify Base URL" className="full-span" rules={[{required: true}]} />
          <Form.Input field="dataset_id" label="Dataset ID" rules={[{required: true}]} />
          <Form.Input field="api_key" label="API Key" mode="password" placeholder={editing?.api_key_configured ? '留空保持当前密钥' : '输入 Dify API Key'} />
          <Form.Select field="pdf_parser" label="PDF 解析器"><Form.Select.Option value="default">系统默认</Form.Select.Option><Form.Select.Option value="mineru">MinerU</Form.Select.Option><Form.Select.Option value="qwenvl">Qwen-VL</Form.Select.Option><Form.Select.Option value="local">本地解析</Form.Select.Option></Form.Select>
          <Form.Select field="chunking_mode" label="分块策略"><Form.Select.Option value="automatic">Dify 自动分块</Form.Select.Option><Form.Select.Option value="custom">自定义分块</Form.Select.Option></Form.Select>
          <Form.InputNumber field="chunk_max_tokens" label="最大 Token" min={1} max={10000} />
          <Form.Input field="chunk_separator" label="自定义分隔符" />
          <Form.Switch field="enabled" label="启用知识库" />
        </div>
        <div className="drawer-footer"><Button onClick={() => setEditing(undefined)}>取消</Button><Button htmlType="submit" theme="solid" type="primary" loading={saving}>保存配置</Button></div>
      </Form>
    </SideSheet>
  </div>;
}
