import { OverviewStats } from '../components/OverviewStats';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Modal, Progress, Select, SideSheet, Table, Toast, Typography } from '@douyinfe/semi-ui';
import { IconPause, IconRefresh, IconRestart, IconSpin } from '@douyinfe/semi-icons';
import { api, queryString } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { usePlatform } from '../context/PlatformContext';
import { useNavigate } from 'react-router-dom';
import type { DeleteJob, IngestJob, JobItem, TaskPage, TaskSummary } from '../types';
import { formatDate, taskProgress } from '../utils/format';

type TaskDetail = IngestJob | DeleteJob;
interface TaskEvent {event_id: number; filename: string | null; phase: string; status: string; message: string; created_at: string; duration_ms: number | null;}

export function TasksPage() {
  const navigate = useNavigate();
  const {activeAgentId, canWriteDocuments} = usePlatform();
  const [data, setData] = useState<TaskPage>({items: [], page: 1, page_size: 30, total: 0, total_pages: 1});
  const [status, setStatus] = useState('');
  const [taskType, setTaskType] = useState('');
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<TaskSummary | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [beforeId, setBeforeId] = useState<number | null>(null);
  const [eventsLoading, setEventsLoading] = useState(false);
  const selectedTaskId = useRef<string | null>(null);

  async function loadEvents(task: TaskSummary, before?: number) {
    if (task.task_type === 'document_delete') { setEvents([]); setBeforeId(null); return; }
    setEventsLoading(true);
    try {
      const result = await api<{items: TaskEvent[]; next_before_id: number | null}>(`/api/jobs/${encodeURIComponent(task.task_id)}/events${queryString({limit: 50, before_id: before})}`);
      if (selectedTaskId.current !== task.task_id) return;
      setEvents(current => before ? [...current, ...result.items] : result.items);
      setBeforeId(result.next_before_id);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '任务记录加载失败'); }
    finally { if (selectedTaskId.current === task.task_id) setEventsLoading(false); }
  }

  const load = useCallback(async (page = data.page) => {
    if (!activeAgentId) return;
    setLoading(true);
    try {
      const result = await api<TaskPage>(`/api/tasks${queryString({page, page_size: data.page_size, status, task_type: taskType, agent_id: activeAgentId})}`);
      setData(result);
      setSelected(current => current ? result.items.find(item => item.task_id === current.task_id) || current : null);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '任务加载失败'); }
    finally { setLoading(false); }
  }, [activeAgentId, data.page, data.page_size, status, taskType]);

  async function loadDetail(task: TaskSummary) {
    selectedTaskId.current = task.task_id;
    setSelected(task); setDetail(null); setEvents([]); setBeforeId(null); setDetailLoading(true);
    void loadEvents(task);
    try {
      const path = task.task_type !== 'document_delete' ? `/api/jobs/${task.task_id}` : `/api/delete-jobs/${task.task_id}`;
      const result = await api<TaskDetail>(path);
      if (selectedTaskId.current === task.task_id) setDetail(result);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '任务详情加载失败'); }
    finally { if (selectedTaskId.current === task.task_id) setDetailLoading(false); }
  }

  async function taskAction(action: 'pause' | 'resume' | 'retry-failed' | 'cancel') {
    if (!selected) return;
    try {
      const path = selected.task_type === 'document_delete' ? `/api/delete-jobs/${selected.task_id}/${action}` : `/api/jobs/${selected.task_id}/${action}`;
      const next = await api<TaskDetail>(path, {method: 'POST'});
      setDetail(next); Toast.success('任务状态已更新'); await load();
    } catch (error) { Toast.error(error instanceof Error ? error.message : '操作失败'); }
  }

  function cancelTask() {
    const content = selected?.task_type === 'document_delete' ? '尚未开始的文件将停止删除；正在执行的删除会继续，已删除的文件无法恢复。' : '尚未开始的文件将停止处理；正在执行的文件会继续运行，已提交给 Dify 的索引可能继续进行。';
    Modal.confirm({title: '取消任务', content, okText: '确认取消', cancelText: '返回', onOk: () => taskAction('cancel')});
  }

  useEffect(() => { void load(1); }, [activeAgentId, status, taskType]);
  useEffect(() => { selectedTaskId.current = null; setSelected(null); setDetail(null); }, [activeAgentId]);
  useEffect(() => {
    if (!data.items.some(item => item.status === 'RUNNING')) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [data.items, load]);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    const path = selected.task_type !== 'document_delete' ? `/api/jobs/${selected.task_id}` : `/api/delete-jobs/${selected.task_id}`;
    api<TaskDetail>(path).then(value => {if (active) setDetail(value);}).catch(() => {});
    return () => {active = false;};
  }, [selected?.task_id, selected?.raw_status, selected?.updated_at]);

  return <div className="page-stack"><OverviewStats items={[{label:'匹配任务', value:data.total, note:'当前筛选范围'},{label:'本页运行中', value:data.items.filter(x => x.status === 'RUNNING').length, note:'当前列表'},{label:'本页已完成', value:data.items.filter(x => x.status === 'SUCCEEDED').length, note:'当前列表'},{label:'本页失败', value:data.items.filter(x => x.status === 'FAILED').length, note:'当前列表'}]} />
    <section className="toolbar-row">
      <div className="filter-cluster">
        <Select value={taskType} onChange={value => setTaskType(String(value))} style={{width: 160}}>
          <Select.Option value="">全部任务</Select.Option><Select.Option value="document_ingest">文档入库</Select.Option><Select.Option value="document_reparse">重新解析</Select.Option><Select.Option value="document_reindex">重建索引</Select.Option><Select.Option value="document_delete">文档删除</Select.Option>
        </Select>
        <Select value={status} onChange={value => setStatus(String(value))} style={{width: 140}}>
          <Select.Option value="">全部状态</Select.Option><Select.Option value="PENDING">待处理</Select.Option><Select.Option value="RUNNING">运行中</Select.Option><Select.Option value="SUCCEEDED">已完成</Select.Option><Select.Option value="FAILED">失败</Select.Option><Select.Option value="CANCELLED">已取消</Select.Option>
        </Select>
      </div>
      <Button icon={<IconRefresh />} onClick={() => void load()} loading={loading}>刷新</Button>
    </section>
    <section className="content-section flush-section">
      <Table<TaskSummary> dataSource={data.items} loading={loading} rowKey="task_id" onRow={record => ({onClick: () => record && void loadDetail(record as TaskSummary), className: 'clickable-row'})}
        pagination={{currentPage: data.page, pageSize: data.page_size, total: data.total, showSizeChanger: true, pageSizeOpts: [15, 30, 50, 100], onPageChange: page => void load(page), onPageSizeChange: size => setData(current => ({...current, page_size: size, page: 1}))}}>
        <Table.Column title="任务" dataIndex="title" render={(_, item) => <div className="primary-cell"><strong>{item.title}</strong><small>{item.task_id}</small></div>} />
        <Table.Column title="类型" dataIndex="task_type" width={120} render={value => value === 'document_ingest' ? '文档入库' : value === 'document_reparse' ? '重新解析' : value === 'document_reindex' ? '重建索引' : '文档删除'} />
        <Table.Column title="状态" dataIndex="status" width={110} render={(value, item) => <StatusTag status={item.raw_status === 'cancelling' ? 'cancelling' : String(value)} />} />
        <Table.Column title="进度" width={200} render={(_, item) => <div className="progress-cell"><Progress percent={taskProgress(item.completed, item.total)} size="small" showInfo={false} /><span>{item.completed}/{item.total}</span></div>} />
        <Table.Column title="成功 / 失败" width={120} render={(_, item) => <span className={item.failed ? 'text-danger' : ''}>{item.succeeded} / {item.failed}</span>} />
        <Table.Column title="更新时间" dataIndex="updated_at" width={180} render={value => formatDate(String(value))} />
      </Table>
    </section>
    <SideSheet title={selected?.title || '任务详情'} visible={Boolean(selected)} onCancel={() => {selectedTaskId.current = null; setSelected(null); setDetail(null);}} width={720}>
      {detailLoading || !selected || !detail ? <div className="drawer-loading"><IconSpin spin /> 加载任务详情…</div> : <div className="task-detail">
        <div className="detail-summary">
          <div><span>状态</span><StatusTag status={detail.status} /></div><div><span>进度</span><strong>{detail.completed} / {detail.total}</strong></div><div><span>成功</span><strong>{detail.succeeded}</strong></div><div><span>失败</span><strong className={detail.failed ? 'text-danger' : ''}>{detail.failed}</strong></div>
        </div>
        {canWriteDocuments && selected.agent_id === activeAgentId && <div className="drawer-actions">
          {selected.task_type === 'document_ingest' && selected.raw_status === 'uploading' && <Button onClick={() => navigate(`/upload?job=${encodeURIComponent(selected.task_id)}`)}>继续上传</Button>}
          {selected.can_pause && <Button icon={<IconPause />} onClick={() => void taskAction('pause')}>暂停任务</Button>}
          {selected.can_resume && <Button icon={<IconRestart />} onClick={() => void taskAction('resume')}>恢复任务</Button>}
          {selected.can_retry && <Button icon={<IconRefresh />} onClick={() => void taskAction('retry-failed')}>重试失败项</Button>}
          {selected.can_cancel && <Button type="danger" onClick={cancelTask}>取消任务</Button>}
        </div>}
        <Typography.Title heading={6}>文件处理记录</Typography.Title>
        <Table<JobItem> dataSource={detail.items} pagination={detail.items.length > 20 ? {pageSize: 20} : false} rowKey="item_id" size="small">
          <Table.Column title="文件" dataIndex="filename" render={value => <span className="filename-cell">{String(value)}</span>} />
          <Table.Column title="状态" dataIndex="status" width={100} render={value => <StatusTag status={String(value)} />} />
          <Table.Column title="结果" dataIndex="message" render={value => <span className="task-message">{String(value || '—')}</span>} />
        </Table>
        {selected.task_type !== 'document_delete' && <><div className="section-heading"><Typography.Title heading={6}>处理事件</Typography.Title><Button icon={<IconRefresh />} theme="borderless" loading={eventsLoading} onClick={() => void loadEvents(selected)}>刷新</Button></div><Table<TaskEvent> dataSource={events} rowKey="event_id" loading={eventsLoading} pagination={false} size="small" empty="该任务没有已采集的事件"><Table.Column title="时间" dataIndex="created_at" render={value => formatDate(String(value))} /><Table.Column title="文件" dataIndex="filename" render={value => value || '任务'} /><Table.Column title="状态" dataIndex="status" render={value => <StatusTag status={String(value)} />} /><Table.Column title="记录" dataIndex="message" /><Table.Column title="耗时" dataIndex="duration_ms" render={value => value == null ? '—' : `${value} ms`} /></Table>{beforeId && <Button loading={eventsLoading} onClick={() => void loadEvents(selected, beforeId)}>加载更早记录</Button>}</>}
      </div>}
    </SideSheet>
  </div>;
}
