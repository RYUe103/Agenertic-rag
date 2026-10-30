import { useCallback, useEffect, useState } from 'react';
import { Button, Progress, Select, SideSheet, Table, Toast, Typography } from '@douyinfe/semi-ui';
import { IconPause, IconRefresh, IconRestart, IconSpin } from '@douyinfe/semi-icons';
import { api, queryString } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { usePlatform } from '../context/PlatformContext';
import type { DeleteJob, IngestJob, JobItem, TaskPage, TaskSummary } from '../types';
import { formatDate, taskProgress } from '../utils/format';

type TaskDetail = IngestJob | DeleteJob;

export function TasksPage() {
  const {activeAgentId} = usePlatform();
  const [data, setData] = useState<TaskPage>({items: [], page: 1, page_size: 30, total: 0, total_pages: 1});
  const [status, setStatus] = useState('');
  const [taskType, setTaskType] = useState('');
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<TaskSummary | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async (page = data.page) => {
    if (!activeAgentId) return;
    setLoading(true);
    try {
      setData(await api<TaskPage>(`/api/tasks${queryString({page, page_size: data.page_size, status, task_type: taskType, agent_id: activeAgentId})}`));
    } catch (error) { Toast.error(error instanceof Error ? error.message : '任务加载失败'); }
    finally { setLoading(false); }
  }, [activeAgentId, data.page, data.page_size, status, taskType]);

  async function loadDetail(task: TaskSummary) {
    setSelected(task); setDetailLoading(true);
    try {
      const path = task.task_type === 'document_ingest' ? `/api/jobs/${task.task_id}` : `/api/delete-jobs/${task.task_id}`;
      setDetail(await api<TaskDetail>(path));
    } catch (error) { Toast.error(error instanceof Error ? error.message : '任务详情加载失败'); }
    finally { setDetailLoading(false); }
  }

  async function taskAction(action: 'pause' | 'resume' | 'retry-failed') {
    if (!selected) return;
    try {
      const next = await api<IngestJob>(`/api/jobs/${selected.task_id}/${action}`, {method: 'POST'});
      setDetail(next); Toast.success('任务状态已更新'); await load();
    } catch (error) { Toast.error(error instanceof Error ? error.message : '操作失败'); }
  }

  useEffect(() => { void load(1); }, [activeAgentId, status, taskType]);
  useEffect(() => {
    if (!data.items.some(item => item.status === 'RUNNING')) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [data.items, load]);

  return <div className="page-stack">
    <section className="toolbar-row">
      <div className="filter-cluster">
        <Select value={taskType} onChange={value => setTaskType(String(value))} style={{width: 160}}>
          <Select.Option value="">全部任务</Select.Option><Select.Option value="document_ingest">文档入库</Select.Option><Select.Option value="document_delete">文档删除</Select.Option>
        </Select>
        <Select value={status} onChange={value => setStatus(String(value))} style={{width: 140}}>
          <Select.Option value="">全部状态</Select.Option><Select.Option value="PENDING">待处理</Select.Option><Select.Option value="RUNNING">运行中</Select.Option><Select.Option value="SUCCEEDED">已完成</Select.Option><Select.Option value="FAILED">失败</Select.Option>
        </Select>
      </div>
      <Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button>
    </section>
    <section className="content-section flush-section">
      <Table<TaskSummary> dataSource={data.items} loading={loading} rowKey="task_id" onRow={record => ({onClick: () => record && void loadDetail(record as TaskSummary), className: 'clickable-row'})}
        pagination={{currentPage: data.page, pageSize: data.page_size, total: data.total, showSizeChanger: true, pageSizeOpts: [15, 30, 50, 100], onPageChange: page => void load(page), onPageSizeChange: size => setData(current => ({...current, page_size: size, page: 1}))}}>
        <Table.Column title="任务" dataIndex="title" render={(_, item) => <div className="primary-cell"><strong>{item.title}</strong><small>{item.task_id}</small></div>} />
        <Table.Column title="类型" dataIndex="task_type" width={120} render={value => value === 'document_ingest' ? '文档入库' : '文档删除'} />
        <Table.Column title="状态" dataIndex="status" width={110} render={value => <StatusTag status={String(value)} />} />
        <Table.Column title="进度" width={200} render={(_, item) => <div className="progress-cell"><Progress percent={taskProgress(item.completed, item.total)} size="small" showInfo={false} /><span>{item.completed}/{item.total}</span></div>} />
        <Table.Column title="成功 / 失败" width={120} render={(_, item) => <span className={item.failed ? 'text-danger' : ''}>{item.succeeded} / {item.failed}</span>} />
        <Table.Column title="更新时间" dataIndex="updated_at" width={180} render={value => formatDate(String(value))} />
      </Table>
    </section>
    <SideSheet title={selected?.title || '任务详情'} visible={Boolean(selected)} onCancel={() => {setSelected(null); setDetail(null);}} width={720}>
      {detailLoading || !selected || !detail ? <div className="drawer-loading"><IconSpin spin /> 加载任务详情…</div> : <div className="task-detail">
        <div className="detail-summary">
          <div><span>状态</span><StatusTag status={detail.status} /></div><div><span>进度</span><strong>{detail.completed} / {detail.total}</strong></div><div><span>成功</span><strong>{detail.succeeded}</strong></div><div><span>失败</span><strong className={detail.failed ? 'text-danger' : ''}>{detail.failed}</strong></div>
        </div>
        {selected.task_type === 'document_ingest' && <div className="drawer-actions">
          {selected.can_pause && <Button icon={<IconPause />} onClick={() => void taskAction('pause')}>暂停任务</Button>}
          {selected.can_resume && <Button icon={<IconRestart />} onClick={() => void taskAction('resume')}>恢复任务</Button>}
          {selected.can_retry && <Button icon={<IconRefresh />} onClick={() => void taskAction('retry-failed')}>重试失败项</Button>}
        </div>}
        <Typography.Title heading={6}>文件处理记录</Typography.Title>
        <Table<JobItem> dataSource={detail.items} pagination={detail.items.length > 20 ? {pageSize: 20} : false} rowKey="item_id" size="small">
          <Table.Column title="文件" dataIndex="filename" render={value => <span className="filename-cell">{String(value)}</span>} />
          <Table.Column title="状态" dataIndex="status" width={100} render={value => <StatusTag status={String(value)} />} />
          <Table.Column title="结果" dataIndex="message" render={value => <span className="task-message">{String(value || '—')}</span>} />
        </Table>
      </div>}
    </SideSheet>
  </div>;
}
