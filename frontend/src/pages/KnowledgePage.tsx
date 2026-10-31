import { useEffect, useState } from 'react';
import { Button, Form, Input, Modal, Select, SideSheet, Switch, Table, Tag, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconDelete, IconEdit, IconPlus, IconRefresh, IconSearch, IconSetting } from '@douyinfe/semi-icons';
import { RetrievalTest } from '../components/RetrievalTest';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';
import { useLocation, useNavigate } from 'react-router-dom';
import type { KnowledgeBase } from '../types';

type FormValues = Record<string, unknown>;
type MetadataField = KnowledgeBase['metadata_fields'][number];
interface KnowledgeStats {knowledge_base_id: string; documents: {total: number; indexed: number; failed: number; processing: number} | null; chunk_count: number | null;}

function payload(values: FormValues, existing: KnowledgeBase | null) {
  const result: Record<string, unknown> = {...(existing || {}), ...values};
  [
    'owner_username', 'owner_user_id', 'api_key_configured', 'api_key_hint', 'answer_prompt_custom',
    'qwenvl_api_key_configured', 'source_platform_api_key_configured', 'workflow_api_key_configured',
  ].forEach(key => delete result[key]);
  result.api_key = values.api_key || null;
  result.qwenvl_api_key = null;
  result.source_platform_api_key = values.source_platform_api_key || null;
  result.workflow_api_key = null;
  result.metadata_fields ||= [];
  return result;
}

export function KnowledgePage() {
  const {reloadAgents, session} = usePlatform();
  const location = useLocation();
  const navigate = useNavigate();
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [stats, setStats] = useState<Record<string, KnowledgeStats>>({});
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<KnowledgeBase | null | undefined>(undefined);
  const [testing, setTesting] = useState<KnowledgeBase | null>(null);
  const [saving, setSaving] = useState(false);
  const [difyBaseUrl, setDifyBaseUrl] = useState('');
  const [metadataBase, setMetadataBase] = useState<KnowledgeBase | null>(null);
  const [metadataFields, setMetadataFields] = useState<MetadataField[]>([]);
  const [metadataEnabled, setMetadataEnabled] = useState(true);
  const [metadataPrompt, setMetadataPrompt] = useState('');

  async function load() {
    setLoading(true);
    try {
      const [bases, summaries] = await Promise.all([api<KnowledgeBase[]>('/api/admin/knowledge-bases'), api<KnowledgeStats[]>('/api/knowledge-bases/statistics')]);
      setItems(bases); setStats(Object.fromEntries(summaries.map(item => [item.knowledge_base_id, item])));
    }
    catch (error) { Toast.error(error instanceof Error ? error.message : '知识库加载失败'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); void api<{dify_base_url: string}>('/api/platform/config').then(value => setDifyBaseUrl(value.dify_base_url)).catch(() => Toast.error('无法读取 Dify 服务地址')); }, []);
  useEffect(() => { if (new URLSearchParams(location.search).get('create') === '1') { setEditing(null); navigate('/knowledge', {replace: true}); } }, [location.search, navigate]);

  async function save(values: FormValues) {
    setSaving(true);
    try {
      const id = editing?.knowledge_base_id || `kb-${crypto.randomUUID()}`;
      values = {...values, knowledge_base_id: id, base_url: editing?.base_url || difyBaseUrl};
      await api<KnowledgeBase>(editing ? `/api/admin/knowledge-bases/${encodeURIComponent(id)}` : '/api/admin/knowledge-bases', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload(values, editing || null)),
      });
      Toast.success(editing ? '知识库配置已保存' : '知识库已创建'); setEditing(undefined); await Promise.all([load(), reloadAgents()]);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '保存失败'); }
    finally { setSaving(false); }
  }

  function remove(item: KnowledgeBase) {
    Modal.confirm({title: '删除知识库连接', content: `确定删除“${item.name}”的平台配置？请先确认关联 Agent 和文档的处理方式。`, okText: '确认删除', cancelText: '取消', okButtonProps: {type: 'danger'}, onOk: async () => {await api(`/api/admin/knowledge-bases/${encodeURIComponent(item.knowledge_base_id)}`, {method: 'DELETE'}); Toast.success('知识库配置已删除'); await Promise.all([load(), reloadAgents()]);}});
  }

  function openMetadata(item: KnowledgeBase) {
    setMetadataBase(item);
    setMetadataFields(item.metadata_fields.map(field => ({...field})));
    setMetadataEnabled(Boolean(item.metadata_ai_enabled));
    setMetadataPrompt(String(item.metadata_extraction_prompt || ''));
  }

  async function saveMetadata() {
    if (!metadataBase) return;
    const keys = metadataFields.map(field => field.key);
    if (keys.some(key => !/^[a-z][a-z0-9_]{0,63}$/.test(key)) || metadataFields.some(field => !field.label.trim())) {
      Toast.error('请填写字段名称，标识须以小写字母开头且仅包含小写字母、数字和下划线'); return;
    }
    if (new Set(keys).size !== keys.length) { Toast.error('字段标识不能重复'); return; }
    setSaving(true);
    try {
      const latest = (await api<KnowledgeBase[]>('/api/admin/knowledge-bases')).find(item => item.knowledge_base_id === metadataBase.knowledge_base_id);
      if (!latest) throw new Error('知识库不存在或无权访问');
      await api(`/api/admin/knowledge-bases/${encodeURIComponent(latest.knowledge_base_id)}`, {
        method: 'PUT', body: JSON.stringify(payload({metadata_fields: metadataFields, metadata_ai_enabled: metadataEnabled, metadata_extraction_prompt: metadataPrompt}, latest)),
      });
      Toast.success('元数据配置已保存'); setMetadataBase(null); await load();
    } catch (error) { Toast.error(error instanceof Error ? error.message : '元数据配置保存失败'); }
    finally { setSaving(false); }
  }

  function updateMetadataField(index: number, change: Partial<MetadataField>) {
    setMetadataFields(fields => fields.map((field, i) => i === index ? {...field, ...change} : field));
  }

  const initialValues = editing || {
    name: '', dataset_id: '',
    search_method: 'semantic_search', chunking_mode: 'automatic', chunk_separator: '\n__SPLIT__\n',
    chunk_max_tokens: 800, pdf_parser: 'default', enabled: true,
  };

  return <div className="page-stack">
    <section className="toolbar-row"><Typography.Text type="tertiary">共 {items.length} 个知识库连接</Typography.Text><div><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button> <Button icon={<IconPlus />} theme="solid" type="primary" onClick={() => setEditing(null)}>新建知识库</Button></div></section>
    <section className="content-section flush-section"><Table<KnowledgeBase> dataSource={items} loading={loading} rowKey="knowledge_base_id" pagination={false}>
      <Table.Column title="知识库" dataIndex="name" render={(_, record) => { const item = record as KnowledgeBase; return <div className="primary-cell"><strong>{item.name}</strong><small>{item.description || item.knowledge_base_id}</small></div>; }} />
      <Table.Column title="所有者" dataIndex="owner_username" width={130} />
      <Table.Column title="文档数" width={90} render={(_, record) => stats[record.knowledge_base_id]?.documents?.total ?? '未采集'} />
      <Table.Column title="分段数" width={90} render={(_, record) => stats[record.knowledge_base_id]?.chunk_count ?? '未采集'} />
      <Table.Column title="文档索引" width={150} render={(_, record) => {const value = stats[record.knowledge_base_id]?.documents; return value ? `${value.indexed} / ${value.total} 已索引` : '未采集';}} />
      <Table.Column title="解析器" dataIndex="pdf_parser" width={120} render={value => <Tag>{String(value)}</Tag>} />
      <Table.Column title="分块" dataIndex="chunking_mode" width={120} render={(_, record) => { const item = record as KnowledgeBase; return item.chunking_mode === 'automatic' ? '自动分块' : `自定义 · ${item.chunk_max_tokens}`; }} />
      <Table.Column title="Dify" dataIndex="api_key_configured" width={100} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已配置' : '未配置'}</Tag>} />
      <Table.Column title="状态" dataIndex="enabled" width={90} render={value => <Tag color={value ? 'green' : 'grey'}>{value ? '已启用' : '已禁用'}</Tag>} />
      <Table.Column title="" width={180} render={(_, record) => { const item = record as KnowledgeBase; return <><Button icon={<IconSearch />} theme="borderless" onClick={() => setTesting(item)} aria-label="检索测试" title="检索测试" /><Button icon={<IconSetting />} theme="borderless" onClick={() => openMetadata(item)} aria-label="元数据配置" title="元数据配置" /><Button icon={<IconEdit />} theme="borderless" onClick={() => setEditing(item)} aria-label="编辑" title="编辑" /><Button icon={<IconDelete />} type="danger" theme="borderless" onClick={() => remove(item)} aria-label="删除" title="删除" /></>; }} />
    </Table></section>
    <SideSheet visible={Boolean(testing)} onCancel={() => setTesting(null)} title={`检索测试 · ${testing?.name || ''}`} width={680}>{testing && <RetrievalTest key={testing.knowledge_base_id} knowledgeBaseId={testing.knowledge_base_id} />}</SideSheet>
    <SideSheet visible={editing !== undefined} onCancel={() => setEditing(undefined)} title={editing ? `编辑·${editing.name}` : '新建知识库'} width={640}>
      <Form key={editing?.knowledge_base_id || 'new'} initValues={initialValues} onSubmit={values => void save(values as FormValues)} labelPosition="top">
        <div className="form-grid two-columns">
          <Form.Input field="name" label="名称" rules={[{required: true}]} />
          {session.is_admin && <Form.Input field="tenant_key" label="组织租户（可选）" placeholder="绑定后仅组织成员可访问" />}
          <Form.Input field="dataset_id" label="Dataset ID" rules={[{required: true}]} />
          <Form.Input field="embedding_model" label="Embedding 模型标识（可选）" placeholder="例如 text-embedding-3-large；未配置则显示未采集" />
          <Form.Input field="api_key" label="API Key" mode="password" className="full-span" rules={editing?.api_key_configured ? [] : [{required: true}]} placeholder={editing?.api_key_configured ? '留空保持当前密钥' : '输入 Dify API Key'} />
          {session.is_admin && <Form.Select field="pdf_parser" label="统一文档解析器" className="full-span" extraText="选择 MinerU 后，PDF、DOC、DOCX 和 TXT 都先提交 MinerU；服务失败行为由 MINERU_FALLBACK_ON_ERROR 控制。"><Form.Select.Option value="default">按平台默认配置</Form.Select.Option><Form.Select.Option value="mineru">MinerU（所有支持格式）</Form.Select.Option><Form.Select.Option value="local">本地解析（显式回退）</Form.Select.Option><Form.Select.Option value="qwenvl">Qwen-VL（仅 PDF）</Form.Select.Option></Form.Select>}
          {editing && session.is_admin && <Form.Select field="source_platform_mode" label="源文件展示平台" className="full-span"><Form.Select.Option value="inherit">使用管理员统一配置</Form.Select.Option><Form.Select.Option value="local">本地文件</Form.Select.Option><Form.Select.Option value="http">HTTP 文件平台</Form.Select.Option><Form.Select.Option value="command">命令行平台</Form.Select.Option></Form.Select>}
          {editing && session.is_admin && <Form.Input field="source_platform_base_url" label="HTTP 平台地址" className="full-span" />}
          {editing && session.is_admin && <Form.Input field="source_platform_api_key" label="HTTP 平台密钥" mode="password" className="full-span" placeholder="留空保持当前密钥" />}
        </div>
        <div className="drawer-footer"><Button onClick={() => setEditing(undefined)}>取消</Button><Button htmlType="submit" theme="solid" type="primary" loading={saving}>保存配置</Button></div>
      </Form>
    </SideSheet>
    <SideSheet visible={Boolean(metadataBase)} onCancel={() => setMetadataBase(null)} title={`元数据配置 · ${metadataBase?.name || ''}`} width={760}>
      <div className="toolbar-row"><Typography.Text>AI 元数据提取</Typography.Text><Switch checked={metadataEnabled} onChange={setMetadataEnabled} aria-label="AI 元数据提取" /></div>
      <Typography.Title heading={6}>提取说明</Typography.Title><TextArea value={metadataPrompt} onChange={setMetadataPrompt} maxLength={12000} rows={3} />
      <div className="toolbar-row"><Typography.Title heading={6}>字段定义</Typography.Title><Button icon={<IconPlus />} disabled={metadataFields.length >= 50} onClick={() => setMetadataFields(fields => [...fields, {key: '', label: '', value_type: 'string', description: ''}])}>添加字段</Button></div>
      {metadataFields.length === 0 && <Typography.Text type="tertiary">暂无自定义字段</Typography.Text>}
      {metadataFields.map((field, index) => <div className="metadata-field-editor" key={index}>
        <Input value={field.key} onChange={value => updateMetadataField(index, {key: value})} placeholder="字段标识，如 project_name" aria-label="字段标识" maxLength={64} />
        <Input value={field.label} onChange={value => updateMetadataField(index, {label: value})} placeholder="显示名称" aria-label="显示名称" maxLength={100} />
        <Select value={field.value_type} onChange={value => updateMetadataField(index, {value_type: String(value)})} aria-label="字段类型" optionList={[{label: '文本', value: 'string'}, {label: '数字', value: 'number'}, {label: '日期', value: 'date'}, {label: '布尔', value: 'boolean'}, {label: '列表', value: 'list'}]} />
        <Input value={field.description} onChange={value => updateMetadataField(index, {description: value})} placeholder="提取说明（可选）" aria-label="字段提取说明" maxLength={500} />
        <Button icon={<IconDelete />} theme="borderless" type="danger" aria-label="移除字段" title="移除字段" onClick={() => setMetadataFields(fields => fields.filter((_, i) => i !== index))} />
      </div>)}
      <div className="drawer-footer"><Button onClick={() => setMetadataBase(null)}>取消</Button><Button theme="solid" type="primary" loading={saving} onClick={() => void saveMetadata()}>保存配置</Button></div>
    </SideSheet>
  </div>;
}
