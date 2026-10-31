import { useCallback, useEffect, useMemo, useState } from 'react';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import {
  Button, DatePicker, Dropdown, Input, Modal, Select, SideSheet, Space, Table, Tabs,
  Toast, Typography,
} from '@douyinfe/semi-ui';
import { IconDelete, IconEdit, IconEyeOpened, IconMore, IconPlus, IconRefresh, IconSearch } from '@douyinfe/semi-icons';
import { useNavigate } from 'react-router-dom';
import { api, queryString } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { DocumentOutline, DocumentSegments } from '../components/DocumentStructure';
import { usePlatform } from '../context/PlatformContext';
import type { MarkdownPreview, Source, SourcePage, SourceVersion } from '../types';
import { formatBytes, formatDate } from '../utils/format';

interface Filters {
  query: string;
  parseStatus: string;
  difyStatus: string;
  documentType: string;
  createdFrom: string;
  createdTo: string;
}

const emptyFilters: Filters = {query: '', parseStatus: '', difyStatus: '', documentType: '', createdFrom: '', createdTo: ''};

function dateParam(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'string') return new Date(value).toISOString();
  return '';
}

export function DocumentsPage() {
  const navigate = useNavigate();
  const {activeAgentId, canWriteDocuments} = usePlatform();
  const [data, setData] = useState<SourcePage>({items: [], page: 1, page_size: 30, total: 0, total_pages: 1});
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [applied, setApplied] = useState<Filters>(emptyFilters);
  const [loading, setLoading] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Array<string | number>>([]);
  const [activeSource, setActiveSource] = useState<Source | null>(null);
  const [markdown, setMarkdown] = useState<MarkdownPreview | null>(null);
  const [versions, setVersions] = useState<SourceVersion[]>([]);
  const [revision, setRevision] = useState<{version_no: number; markdown: string} | null>(null);
  const [lifecycle, setLifecycle] = useState<{name: string; label: string; status: string; error?: string | null}[]>([]);
  const [markdownLoading, setMarkdownLoading] = useState(false);
  const [drawerTab, setDrawerTab] = useState('overview');

  const load = useCallback(async (page = data.page, pageSize = data.page_size) => {
    if (!activeAgentId) return;
    setLoading(true);
    try {
      const result = await api<SourcePage>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/page${queryString({
        page, page_size: pageSize, query: applied.query, parse_status: applied.parseStatus,
        dify_status: applied.difyStatus, document_type: applied.documentType,
        created_from: applied.createdFrom, created_to: applied.createdTo,
      })}`);
      setData(result); setSelectedIds([]);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '文档加载失败'); }
    finally { setLoading(false); }
  }, [activeAgentId, applied, data.page, data.page_size]);

  useEffect(() => { void load(1); }, [activeAgentId, applied]);
  useEffect(() => { setActiveSource(null); setRevision(null); setSelectedIds([]); }, [activeAgentId]);

  async function openSource(source: Source, tab = 'overview') {
    setActiveSource(source); setDrawerTab(tab); setMarkdown(null); setVersions([]); setLifecycle([]);
    if (tab === 'markdown') await loadMarkdown(source.source_id);
  }

  async function loadMarkdown(sourceId: string, version = 'document') {
    if (!activeAgentId) return;
    setMarkdownLoading(true);
    try { setMarkdown(await api<MarkdownPreview>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(sourceId)}/markdown${queryString({version})}`)); }
    catch (error) { Toast.error(error instanceof Error ? error.message : 'Markdown 加载失败'); }
    finally { setMarkdownLoading(false); }
  }
  async function loadVersions(sourceId: string) {
    if (!activeAgentId) return;
    try { setVersions(await api<SourceVersion[]>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(sourceId)}/versions`)); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '版本记录加载失败'); }
  }
  async function openRevision(sourceId: string, versionNo: number) {
    try { setRevision(await api<{version_no: number; markdown: string}>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(sourceId)}/versions/${versionNo}`)); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '版本内容加载失败'); }
  }
  async function loadLifecycle(sourceId: string) {
    if (!activeAgentId) return;
    try { setLifecycle((await api<{stages: typeof lifecycle}>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(sourceId)}/lifecycle`)).stages); } catch (error) { Toast.error(error instanceof Error ? error.message : '生命周期加载失败'); }
  }

  async function reparse(source: Source) {
    if (!activeAgentId) return;
    Modal.confirm({
      title: '重新解析文档', content: `将从原始文件重建“${source.filename}”的 Markdown 并刷新 Dify 文档。`,
      okText: '开始重新解析', onOk: async () => {
        try {
          await api(`/api/agents/${encodeURIComponent(activeAgentId)}/reparse-jobs`, {method: 'POST', body: JSON.stringify({source_ids: [source.source_id]})});
          Toast.success('重新解析任务已创建'); await load();
        } catch (error) {Toast.error(error instanceof Error ? error.message : '创建任务失败');}
      },
    });
  }

  async function remove(source: Source) {
    if (!activeAgentId) return;
    Modal.confirm({
      title: '删除文档', content: `将删除“${source.filename}”的本地文件和 Dify 文档，此操作不可恢复。`,
      okText: '确认删除', cancelText: '取消', okButtonProps: {type: 'danger'},
      onOk: async () => {
        await api(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(source.source_id)}`, {method: 'DELETE'});
        Toast.success(`已删除 ${source.filename}`); setActiveSource(null); await load();
      },
    });
  }

  const selectedSources = useMemo(() => data.items.filter(item => selectedIds.includes(item.source_id)), [data.items, selectedIds]);

  async function batchReparse() {
    if (!activeAgentId || !selectedSources.length) return;
    Modal.confirm({title: `重新解析 ${selectedSources.length} 份文档`, content: '任务将在后台重新解析并刷新 Dify 索引。', okText: '创建任务', onOk: async () => {
      try {
        await api(`/api/agents/${encodeURIComponent(activeAgentId)}/reparse-jobs`, {method: 'POST', body: JSON.stringify({source_ids: selectedSources.map(source => source.source_id)})});
        Toast.success('重新解析任务已创建'); await load();
      } catch (error) {Toast.error(error instanceof Error ? error.message : '创建任务失败');}
    }});
  }

  function reindex(sources: Source[]) {
    if (!activeAgentId || !sources.length) return;
    Modal.confirm({title: `重建 ${sources.length} 份文档的索引`, content: '将使用已确认的 Markdown 重新提交 Dify，不重新解析原文件。', okText: '创建任务', onOk: async () => {
      try {
        await api(`/api/agents/${encodeURIComponent(activeAgentId)}/reindex-jobs`, {method: 'POST', body: JSON.stringify({source_ids: sources.map(source => source.source_id)})});
        Toast.success('索引重建任务已创建'); await load();
      } catch (error) {Toast.error(error instanceof Error ? error.message : '创建任务失败');}
    }});
  }

  async function batchExtractMetadata() {
    if (!activeAgentId || !selectedIds.length) return;
    try {
      const result = await api<{total: number; success: number; failed: number}>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/extract-metadata-batch`, {method: 'POST', body: JSON.stringify({source_ids: selectedIds})});
      Toast.info(`元数据抽取完成：${result.success} 成功，${result.failed} 失败`); await load();
    } catch (error) { Toast.error(error instanceof Error ? error.message : '元数据抽取失败'); }
  }

  async function batchDelete() {
    if (!activeAgentId || !selectedSources.length) return;
    Modal.confirm({
      title: `删除 ${selectedSources.length} 份文档`, content: '文档的本地文件与 Dify 记录都会被删除，且无法恢复。',
      okText: '确认批量删除', cancelText: '取消', okButtonProps: {type: 'danger'},
      onOk: async () => {
        try {
          await api(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/delete-batch`, {method: 'POST', body: JSON.stringify({source_ids: selectedSources.map(source => source.source_id)})});
          Toast.success('删除任务已创建，可在任务中心查看'); setSelectedIds([]);
        } catch (error) { Toast.error(error instanceof Error ? error.message : '创建删除任务失败'); }
      },
    });
  }

  const parsedMarkdown = useMemo(() => markdown ? DOMPurify.sanitize(marked.parse(markdown.markdown) as string) : '', [markdown]);

  return <div className="page-stack documents-page">
    <section className="toolbar-row document-toolbar">
      <div className="filter-cluster filter-wrap">
        <Input prefix={<IconSearch />} placeholder="搜索文件名或标题" value={filters.query} onChange={value => setFilters(current => ({...current, query: value}))} onEnterPress={() => setApplied(filters)} showClear style={{width: 260}} />
        <Select value={filters.parseStatus} onChange={value => setFilters(current => ({...current, parseStatus: String(value)}))} style={{width: 130}}><Select.Option value="">全部解析</Select.Option><Select.Option value="success">解析成功</Select.Option><Select.Option value="processing">解析中</Select.Option><Select.Option value="failed">解析失败</Select.Option></Select>
        <Select value={filters.difyStatus} onChange={value => setFilters(current => ({...current, difyStatus: String(value)}))} style={{width: 130}}><Select.Option value="">全部索引</Select.Option><Select.Option value="success">索引成功</Select.Option><Select.Option value="processing">索引中</Select.Option><Select.Option value="failed">索引失败</Select.Option></Select>
        <Select value={filters.documentType} onChange={value => setFilters(current => ({...current, documentType: String(value)}))} style={{width: 120}}><Select.Option value="">全部类型</Select.Option><Select.Option value="pdf">PDF</Select.Option><Select.Option value="docx">DOCX</Select.Option><Select.Option value="doc">DOC</Select.Option><Select.Option value="txt">TXT</Select.Option></Select>
        <DatePicker type="dateRange" placeholder={['开始日期', '结束日期']} onChange={(value: unknown) => {
          const range = Array.isArray(value) ? value : [];
          setFilters(current => ({...current, createdFrom: dateParam(range[0]), createdTo: dateParam(range[1])}));
        }} />
        <Button theme="solid" type="primary" onClick={() => setApplied(filters)}>查询</Button>
        <Button theme="borderless" onClick={() => {setFilters(emptyFilters); setApplied(emptyFilters);}}>重置</Button>
      </div>
      <Space><Button icon={<IconPlus />} onClick={() => navigate('/knowledge?create=1')}>新建知识库</Button><Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button></Space>
    </section>

    {!canWriteDocuments && activeAgentId && <Typography.Text type="tertiary">当前知识库为只读，无法修改文档。</Typography.Text>}
    {canWriteDocuments && selectedIds.length > 0 && <section className="selection-bar">
      <strong>已选 {selectedIds.length} 项</strong><span />
      <Button onClick={() => void batchExtractMetadata()}>抽取元数据</Button><Button onClick={() => void batchReparse()}>重新解析</Button><Button onClick={() => reindex(selectedSources)}>重建索引</Button><Button type="danger" onClick={() => void batchDelete()}>删除</Button>
    </section>}

    <section className="content-section flush-section">
      <Table<Source> dataSource={data.items} loading={loading} rowKey="source_id" rowSelection={canWriteDocuments ? {selectedRowKeys: selectedIds, onChange: keys => setSelectedIds(keys || [])} : undefined}
        pagination={{currentPage: data.page, pageSize: data.page_size, total: data.total, showSizeChanger: true, pageSizeOpts: [15, 30, 50, 100], onPageChange: page => void load(page), onPageSizeChange: size => void load(1, size)}}>
        <Table.Column title="文件" dataIndex="filename" render={(_, record) => { const source = record as Source; return <button className="filename-button" onClick={() => void openSource(source)}><span className={`file-type file-${source.document_type}`}>{source.document_type.toUpperCase()}</span><span><strong>{source.filename}</strong><small>{source.standard_title || source.source_id}</small></span></button>; }} />
        <Table.Column title="大小" dataIndex="file_size" width={100} render={value => value == null ? '未采集' : formatBytes(Number(value))} />
        <Table.Column title="页数" dataIndex="page_count" width={80} render={value => value == null ? '未采集' : String(value)} />
        <Table.Column title="上传人" dataIndex="uploaded_by" width={120} render={value => value || '未采集'} />
        <Table.Column title="解析" dataIndex="parse_status" width={105} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="索引" dataIndex="dify_status" width={105} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="元数据" dataIndex="metadata_status" width={105} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="上传时间" dataIndex="created_at" width={180} render={value => formatDate(String(value))} />
        <Table.Column title="" width={54} render={(_, record) => { const source = record as Source; return <Dropdown trigger="click" position="bottomRight" render={<Dropdown.Menu><Dropdown.Item icon={<IconEyeOpened />} onClick={() => void openSource(source, 'markdown')}>Markdown 预览</Dropdown.Item>{canWriteDocuments && <><Dropdown.Item icon={<IconEdit />} onClick={() => void reparse(source)}>重新解析</Dropdown.Item><Dropdown.Item icon={<IconRefresh />} onClick={() => reindex([source])}>重建索引</Dropdown.Item><Dropdown.Divider/><Dropdown.Item type="danger" icon={<IconDelete />} onClick={() => void remove(source)}>删除</Dropdown.Item></>}</Dropdown.Menu>}><Button icon={<IconMore />} theme="borderless" aria-label="文档操作" /></Dropdown>; }} />
      </Table>
    </section>

    <SideSheet visible={Boolean(activeSource)} onCancel={() => setActiveSource(null)} title={activeSource?.filename || '文档详情'} width={860}>
      {activeSource && <Tabs activeKey={drawerTab} onChange={key => {setDrawerTab(key); if (key === 'markdown' && !markdown) void loadMarkdown(activeSource.source_id); if (key === 'versions') void loadVersions(activeSource.source_id); if (key === 'records') void loadLifecycle(activeSource.source_id);}}>
        <Tabs.TabPane tab="基本信息" itemKey="overview"><div className="detail-grid">
          <div><span>文件类型</span><strong>{activeSource.document_type.toUpperCase()}</strong></div><div><span>文件大小</span><strong>{activeSource.file_size == null ? '未采集' : formatBytes(activeSource.file_size)}</strong></div><div><span>页数</span><strong>{activeSource.page_count ?? '未采集'}</strong></div><div><span>上传人</span><strong>{activeSource.uploaded_by || '未采集'}</strong></div><div><span>上传时间</span><strong>{formatDate(activeSource.created_at)}</strong></div><div><span>解析状态</span><StatusTag status={activeSource.parse_status} /></div><div><span>索引状态</span><StatusTag status={activeSource.dify_status} /></div><div><span>文件平台</span><StatusTag status={activeSource.pdf_platform_status} /></div><div><span>元数据</span><StatusTag status={activeSource.metadata_status} /></div><div className="detail-wide"><span>Source ID</span><code>{activeSource.source_id}</code></div><div className="detail-wide"><span>文件 Hash</span><code>{activeSource.file_hash}</code></div>
          {(activeSource.error_message || activeSource.metadata_error) && <div className="detail-wide error-box">{activeSource.error_message || activeSource.metadata_error}</div>}
          <div className="detail-wide"><Space><Button theme="solid" type="primary" onClick={() => window.open(`/api/files/${encodeURIComponent(activeSource.source_id)}`, '_blank')}>打开原始文件</Button>{canWriteDocuments && <Button onClick={() => void reparse(activeSource)}>重新解析</Button>}</Space></div>
        </div></Tabs.TabPane>
        <Tabs.TabPane tab="Markdown" itemKey="markdown"><div className="markdown-tools"><Select value={markdown?.version || 'document'} onChange={value => void loadMarkdown(activeSource.source_id, String(value))} loading={markdownLoading}>{(markdown?.available_versions || ['document']).map(version => <Select.Option key={version} value={version}>{version}</Select.Option>)}</Select>{markdown && <Typography.Text type="tertiary">{String(markdown.stats.characters || 0)} 字符 · {String(markdown.stats.lines || 0)} 行</Typography.Text>}</div><article className="markdown-body" aria-busy={markdownLoading} dangerouslySetInnerHTML={{__html: parsedMarkdown}} /></Tabs.TabPane>
        <Tabs.TabPane tab="文档目录" itemKey="outline">{drawerTab === 'outline' && <DocumentOutline agentId={activeAgentId} sourceId={activeSource.source_id} />}</Tabs.TabPane>
        <Tabs.TabPane tab="索引分段" itemKey="segments">{drawerTab === 'segments' && <DocumentSegments agentId={activeAgentId} sourceId={activeSource.source_id} />}</Tabs.TabPane>
        <Tabs.TabPane tab="自定义元数据" itemKey="metadata">{Object.keys(activeSource.custom_metadata).length ? <div className="metadata-list">{Object.entries(activeSource.custom_metadata).map(([key, item]) => <div key={key}><span>{key}</span><strong>{String(item.value ?? '—')}</strong><small>{item.evidence || item.location || item.extraction_method}</small></div>)}</div> : <div className="empty-inline">尚未提取自定义元数据</div>}</Tabs.TabPane>
        <Tabs.TabPane tab="解析与索引" itemKey="records"><div className="detail-grid"><div><span>最后更新</span><strong>{formatDate(activeSource.updated_at)}</strong></div>{lifecycle.map(stage => <div key={stage.name}><span>{stage.label}</span><StatusTag status={stage.status} />{stage.error && <small className="text-danger">{stage.error}</small>}</div>)}{activeSource.error_message && <div className="detail-wide error-box">{activeSource.error_message}</div>}</div></Tabs.TabPane>
        <Tabs.TabPane tab="版本历史" itemKey="versions"><Typography.Text type="tertiary">索引状态为创建快照时的状态；旧快照未采集的配置显示为未采集。</Typography.Text><Table<SourceVersion> dataSource={versions} rowKey="version_id" pagination={false} empty="暂无版本快照"><Table.Column title="版本" dataIndex="version_no" render={value => `v${value}`} /><Table.Column title="解析器" dataIndex="parser_engine" render={value => String(value || '未采集')} /><Table.Column title="分块" render={(_, item) => item.chunking_mode ? `${item.chunking_mode}${item.chunk_max_tokens ? ` · ${item.chunk_max_tokens}` : ''}` : '未采集'} /><Table.Column title="Embedding" dataIndex="embedding_model" render={value => String(value || '未采集')} /><Table.Column title="索引" dataIndex="dify_status" render={value => <StatusTag status={String(value)} />} /><Table.Column title="时间" dataIndex="created_at" render={value => formatDate(String(value))} /><Table.Column title="说明" dataIndex="message" /><Table.Column title="" render={(_, item) => <Button theme="borderless" icon={<IconEyeOpened />} onClick={() => void openRevision(activeSource.source_id, item.version_no)}>查看</Button>} /></Table></Tabs.TabPane>
      </Tabs>}
    </SideSheet>
    <Modal visible={Boolean(revision)} title={`版本 v${revision?.version_no || ''}`} onCancel={() => setRevision(null)} footer={null} width={800}><article className="markdown-body" dangerouslySetInnerHTML={{__html: revision ? DOMPurify.sanitize(marked.parse(revision.markdown) as string) : ''}} /></Modal>
  </div>;
}
