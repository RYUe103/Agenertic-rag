import { useEffect, useState } from 'react';
import { Button, Form, Modal, SideSheet, Table, Tag, Toast, Typography } from '@douyinfe/semi-ui';
import { IconDelete, IconEdit, IconPlus, IconRefresh } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import type { KnowledgeBase } from '../types';

interface AgentConfig {
  agent_id: string;
  name: string;
  description: string;
  knowledge_base_id: string;
  planner_prompt: string;
  planner_model_ids: string[];
  answer_model_ids: string[];
  glossary_documents: unknown[];
  allowed_tools: string[];
  max_tool_calls: number;
  max_retrieval_rounds: number;
  max_evidence_for_answer: number;
  enabled: boolean;
  is_default: boolean;
  planner_prompt_custom?: boolean;
}

interface ModelConnection {model_connection_id: string; name: string; enabled: boolean;}

const toolOptions = [
  'vector_search', 'grep_files', 'keyword_search', 'find_files', 'search_within_document',
  'metadata_search', 'dify_metadata_search', 'read_full_document', 'get_adjacent_chunks', 'get_document_outline',
];

export function AgentsPage() {
  const {reloadAgents} = usePlatform();
  const [items, setItems] = useState<AgentConfig[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [models, setModels] = useState<ModelConnection[]>([]);
  const [editing, setEditing] = useState<AgentConfig | null | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [agents, bases, connections] = await Promise.all([
        api<AgentConfig[]>('/api/admin/agents'), api<KnowledgeBase[]>('/api/admin/knowledge-bases'), api<ModelConnection[]>('/api/admin/model-connections'),
      ]);
      setItems(agents); setKnowledgeBases(bases); setModels(connections);
    } catch (error) { Toast.error(error instanceof Error ? error.message : 'Agent 加载失败'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function save(values: Record<string, unknown>) {
    setSaving(true);
    try {
      const id = String(values.agent_id);
      const body = {...(editing || {}), ...values, glossary_documents: editing?.glossary_documents || []};
      delete (body as Record<string, unknown>).planner_prompt_custom;
      await api(editing ? `/api/admin/agents/${encodeURIComponent(id)}` : '/api/admin/agents', {method: editing ? 'PUT' : 'POST', body: JSON.stringify(body)});
      Toast.success(editing ? 'Agent 配置已保存' : 'Agent 已创建'); setEditing(undefined); await Promise.all([load(), reloadAgents()]);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '保存失败'); }
    finally { setSaving(false); }
  }

  function remove(item: AgentConfig) {
    Modal.confirm({title: '删除 Agent', content: `确定删除“${item.name}”？`, okText: '确认删除', cancelText: '取消', okButtonProps: {type: 'danger'}, onOk: async () => {await api(`/api/admin/agents/${encodeURIComponent(item.agent_id)}`, {method: 'DELETE'}); await Promise.all([load(), reloadAgents()]);}});
  }

  const initialValues = editing || {agent_id: '', name: '', description: '', knowledge_base_id: knowledgeBases[0]?.knowledge_base_id, planner_prompt: '', planner_model_ids: [], answer_model_ids: [], allowed_tools: toolOptions, max_tool_calls: 6, max_retrieval_rounds: 3, max_evidence_for_answer: 12, enabled: true, is_default: false};

  return <div className="page-stack">
    <section className="toolbar-row"><Typography.Text type="tertiary">共 {items.length} 个 Agent</Typography.Text><div><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button> <Button icon={<IconPlus />} theme="solid" type="primary" onClick={() => setEditing(null)}>新建 Agent</Button></div></section>
    <section className="content-section flush-section"><Table<AgentConfig> dataSource={items} loading={loading} rowKey="agent_id" pagination={false}>
      <Table.Column title="Agent" dataIndex="name" render={(_, record) => { const item = record as AgentConfig; return <div className="primary-cell"><strong>{item.name}{item.is_default && <Tag color="blue">默认</Tag>}</strong><small>{item.description || item.agent_id}</small></div>; }} />
      <Table.Column title="知识库" dataIndex="knowledge_base_id" render={value => knowledgeBases.find(item => item.knowledge_base_id === value)?.name || String(value)} />
      <Table.Column title="工具" dataIndex="allowed_tools" width={90} render={value => `${(value as string[]).length} 个`} />
      <Table.Column title="模型链" width={150} render={(_, record) => { const item = record as AgentConfig; return `Planner ${item.planner_model_ids.length} / Answer ${item.answer_model_ids.length}`; }} />
      <Table.Column title="状态" dataIndex="enabled" width={90} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已启用' : '已禁用'}</Tag>} />
      <Table.Column title="" width={110} render={(_, record) => { const item = record as AgentConfig; return <><Button icon={<IconEdit />} theme="borderless" onClick={() => setEditing(item)} /><Button icon={<IconDelete />} type="danger" theme="borderless" onClick={() => remove(item)} /></>; }} />
    </Table></section>
    <SideSheet visible={editing !== undefined} onCancel={() => setEditing(undefined)} title={editing ? `编辑·${editing.name}` : '新建 Agent'} width={680}>
      <Form key={editing?.agent_id || 'new'} initValues={initialValues} onSubmit={values => void save(values as Record<string, unknown>)} labelPosition="top">
        <div className="form-grid two-columns">
          <Form.Input field="agent_id" label="Agent ID" disabled={Boolean(editing)} rules={[{required: true}]} />
          <Form.Input field="name" label="名称" rules={[{required: true}]} />
          <Form.TextArea field="description" label="描述" className="full-span" />
          <Form.Select field="knowledge_base_id" label="绑定知识库" className="full-span" rules={[{required: true}]}>{knowledgeBases.map(item => <Form.Select.Option key={item.knowledge_base_id} value={item.knowledge_base_id}>{item.name}</Form.Select.Option>)}</Form.Select>
          <Form.Select field="planner_model_ids" label="Planner 模型链" multiple>{models.map(item => <Form.Select.Option key={item.model_connection_id} value={item.model_connection_id}>{item.name}</Form.Select.Option>)}</Form.Select>
          <Form.Select field="answer_model_ids" label="Answer 模型链" multiple>{models.map(item => <Form.Select.Option key={item.model_connection_id} value={item.model_connection_id}>{item.name}</Form.Select.Option>)}</Form.Select>
          <Form.Select field="allowed_tools" label="授权工具" multiple className="full-span">{toolOptions.map(tool => <Form.Select.Option key={tool} value={tool}>{tool}</Form.Select.Option>)}</Form.Select>
          <Form.TextArea field="planner_prompt" label="Planner 提示词" className="full-span" autosize rows={8} />
          <Form.InputNumber field="max_tool_calls" label="最大工具调用" min={1} max={30} /><Form.InputNumber field="max_retrieval_rounds" label="最大检索轮数" min={1} max={10} /><Form.InputNumber field="max_evidence_for_answer" label="回答证据上限" min={1} max={50} /><Form.Switch field="enabled" label="启用 Agent" /><Form.Switch field="is_default" label="设为默认 Agent" />
        </div>
        <div className="drawer-footer"><Button onClick={() => setEditing(undefined)}>取消</Button><Button htmlType="submit" theme="solid" type="primary" loading={saving}>保存配置</Button></div>
      </Form>
    </SideSheet>
  </div>;
}
