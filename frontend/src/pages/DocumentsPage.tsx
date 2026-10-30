import { useCallback, useEffect, useMemo, useState } from 'react';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import {
  Button, DatePicker, Dropdown, Input, Modal, Select, SideSheet, Space, Table, Tabs,
  Toast, Typography,
} from '@douyinfe/semi-ui';
import { IconDelete, IconEdit, IconEyeOpened, IconMore, IconRefresh, IconSearch } from '@douyinfe/semi-icons';
import { api, queryString } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { usePlatform } from '../context/PlatformContext';
import type { MarkdownPreview, Source, SourcePage } from '../types';
import { formatDate } from '../utils/format';

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
  const {activeAgentId} = usePlatform();
  const [data, setData] = useState<SourcePage>({items: [], page: 1, page_size: 30, total: 0, total_pages: 1});
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [applied, setApplied] = useState<Filters>(emptyFilters);
  const [loading, setLoading] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Array<string | number>>([]);
  const [activeSource, setActiveSource] = useState<Source | null>(null);
  const [markdown, setMarkdown] = useState<MarkdownPreview | null>(null);
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

  async function openSource(source: Source, tab = 'overview') {
    setActiveSource(source); setDrawerTab(tab); setMarkdown(null);
    if (tab === 'markdown') await loadMarkdown(source.source_id);
  }

  async function loadMarkdown(sourceId: string, version = 'document') {
    if (!activeAgentId) return;
    setMarkdownLoading(true);
    try { setMarkdown(await api<MarkdownPreview>(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(sourceId)}/markdown${queryString({version})}`)); }
    catch (error) { Toast.error(error instanceof Error ? error.message : 'Markdown 加载失败'); }
    finally { setMarkdownLoading(false); }
  }

  async function reparse(source: Source) {
    if (!activeAgentId) return;
    Modal.confirm({
      title: '重新解析文档', content: `将从原始文件重建“${source.filename}”的 Markdown 并刷新 Dify 文档。`,
      okText: '开始重新解析', onOk: async () => {
        await api(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(source.source_id)}/reparse`, {method: 'POST'});
        Toast.success('重新解析已完成'); await load();
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
    let succeeded = 0;
    for (const source of selectedSources) {
      try { await api(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(source.source_id)}/reparse`, {method: 'POST'}); succeeded += 1; }
      catch { /* The aggregate result reports incomplete operations. */ }
    }
    Toast.info(`重新解析完成：${succeeded}/${selectedSources.length}`); await load();
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
        let succeeded = 0;
        for (const source of selectedSources) {
          try { await api(`/api/agents/${encodeURIComponent(activeAgentId)}/sources/${encodeURIComponent(source.source_id)}`, {method: 'DELETE'}); succeeded += 1; }
          catch { /* Continue and report the aggregate result. */ }
        }
        Toast.info(`批量删除完成：${succeeded}/${selectedSources.length}`); await load();
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
      <Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button>
    </section>

    {selectedIds.length > 0 && <section className="selection-bar">
      <strong>已选 {selectedIds.length} 项</strong><span />
      <Button onClick={() => void batchExtractMetadata()}>抽取元数据</Button><Button onClick={() => void batchReparse()}>重新解析</Button><Button type="danger" onClick={() => void batchDelete()}>删除</Button>
    </section>}

    <section className="content-section flush-section">
      <Table<Source> dataSource={data.items} loading={loading} rowKey="source_id" rowSelection={{selectedRowKeys: selectedIds, onChange: keys => setSelectedIds(keys || [])}}
        pagination={{currentPage: data.page, pageSize: data.page_size, total: data.total, showSizeChanger: true, pageSizeOpts: [15, 30, 50, 100], onPageChange: page => void load(page), onPageSizeChange: size => void load(1, size)}}>
        <Table.Column title="文件" dataIndex="filename" render={(_, record) => { const source = record as Source; return <button className="filename-button" onClick={() => void openSource(source)}><span className={`file-type file-${source.document_type}`}>{source.document_type.toUpperCase()}</span><span><strong>{source.filename}</strong><small>{source.standard_title || source.source_id}</small></span></button>; }} />
        <Table.Column title="解析" dataIndex="parse_status" width={105} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="索引" dataIndex="dify_status" width={105} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="元数据" dataIndex="metadata_status" width={105} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="上传时间" dataIndex="created_at" width={180} render={value => formatDate(String(value))} />
        <Table.Column title="" width={54} render={(_, record) => { const source = record as Source; return <Dropdown trigger="click" position="bottomRight" render={<Dropdown.Menu><Dropdown.Item icon={<IconEyeOpened />} onClick={() => void openSource(source, 'markdown')}>Markdown 预览</Dropdown.Item><Dropdown.Item icon={<IconEdit />} onClick={() => void reparse(source)}>重新解析</Dropdown.Item><Dropdown.Divider/><Dropdown.Item type="danger" icon={<IconDelete />} onClick={() => void remove(source)}>删除</Dropdown.Item></Dropdown.Menu>}><Button icon={<IconMore />} theme="borderless" aria-label="文档操作" /></Dropdown>; }} />
      </Table>
    </section>

    <SideSheet visible={Boolean(activeSource)} onCancel={() => setActiveSource(null)} title={activeSource?.filename || '文档详情'} width={860}>
      {activeSource && <Tabs activeKey={drawerTab} onChange={key => {setDrawerTab(key); if (key === 'markdown' && !markdown) void loadMarkdown(activeSource.source_id);}}>
        <Tabs.TabPane tab="基本信息" itemKey="overview"><div className="detail-grid">
          <div><span>文件类型</span><strong>{activeSource.document_type.toUpperCase()}</strong></div><div><span>上传时间</span><strong>{formatDate(activeSource.created_at)}</strong></div><div><span>解析状态</span><StatusTag status={activeSource.parse_status} /></div><div><span>索引状态</span><StatusTag status={activeSource.dify_status} /></div><div><span>文件平台</span><StatusTag status={activeSource.pdf_platform_status} /></div><div><span>元数据</span><StatusTag status={activeSource.metadata_status} /></div><div className="detail-wide"><span>Source ID</span><code>{activeSource.source_id}</code></div><div className="detail-wide"><span>文件 Hash</span><code>{activeSource.file_hash}</code></div>
          {(activeSource.error_message || activeSource.metadata_error) && <div className="detail-wide error-box">{activeSource.error_message || activeSource.metadata_error}</div>}
          <div className="detail-wide"><Space><Button theme="solid" type="primary" onClick={() => window.open(`/api/files/${encodeURIComponent(activeSource.source_id)}`, '_blank')}>打开原始文件</Button><Button onClick={() => void reparse(activeSource)}>重新解析</Button></Space></div>
        </div></Tabs.TabPane>
        <Tabs.TabPane tab="Markdown" itemKey="markdown"><div className="markdown-tools"><Select value={markdown?.version || 'document'} onChange={value => void loadMarkdown(activeSource.source_id, String(value))} loading={markdownLoading}>{(markdown?.available_versions || ['document']).map(version => <Select.Option key={version} value={version}>{version}</Select.Option>)}</Select>{markdown && <Typography.Text type="tertiary">{String(markdown.stats.characters || 0)} 字符 · {String(markdown.stats.lines || 0)} 行</Typography.Text>}</div><article className="markdown-body" aria-busy={markdownLoading} dangerouslySetInnerHTML={{__html: parsedMarkdown}} /></Tabs.TabPane>
        <Tabs.TabPane tab="自定义元数据" itemKey="metadata">{Object.keys(activeSource.custom_metadata).length ? <div className="metadata-list">{Object.entries(activeSource.custom_metadata).map(([key, item]) => <div key={key}><span>{key}</span><strong>{String(item.value ?? '—')}</strong><small>{item.evidence || item.location || item.extraction_method}</small></div>)}</div> : <div className="empty-inline">尚未提取自定义元数据</div>}</Tabs.TabPane>
        <Tabs.TabPane tab="解析与索引" itemKey="records"><div className="capability-gap compact"><strong>当前可用记录</strong><p>现有接口提供解析、Dify 索引和文件平台的最终状态。逐阶段耗时、Chunk 列表、索引版本和结构化日志尚无后端 API。</p></div></Tabs.TabPane>
      </Tabs>}
    </SideSheet>
  </div>;
}
