import { useEffect, useRef, useState } from 'react';
import { Button, Checkbox, Progress, Table, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconFile, IconFolder, IconPause, IconRefresh, IconRestart, IconUpload } from '@douyinfe/semi-icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { usePlatform } from '../context/PlatformContext';
import type { IngestJob, JobItem } from '../types';
import { formatBytes, taskProgress } from '../utils/format';

function buildBatches(files: Array<{file: File; position: number}>, maxFiles = 25, maxBytes = 120 * 1024 * 1024) {
  const batches: Array<Array<{file: File; position: number}>> = [];
  let batch: Array<{file: File; position: number}> = [];
  let bytes = 0;
  files.forEach(({file, position}) => {
    if (batch.length && (batch.length >= maxFiles || bytes + file.size > maxBytes)) {
      batches.push(batch); batch = []; bytes = 0;
    }
    batch.push({file, position}); bytes += file.size;
  });
  if (batch.length) batches.push(batch);
  return batches;
}

export function UploadPage() {
  const navigate = useNavigate();
  const {activeAgentId, canWriteDocuments, agents, setActiveAgentId} = usePlatform();
  const [searchParams] = useSearchParams();
  const recoveryId = searchParams.get('job') || '';
  const inputRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [rewriteEnabled, setRewriteEnabled] = useState(false);
  const [rewriteReview, setRewriteReview] = useState(false);
  const [workflowEnabled, setWorkflowEnabled] = useState(false);
  const [rewritePrompt, setRewritePrompt] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [uploadCount, setUploadCount] = useState(0);
  const [job, setJob] = useState<IngestJob | null>(null);
  const [jobAgentId, setJobAgentId] = useState('');
  const [recovering, setRecovering] = useState(false);

  useEffect(() => {
    if (!recoveryId) return;
    let active = true;
    api<IngestJob>(`/api/jobs/${encodeURIComponent(recoveryId)}`).then(value => {
      if (!active) return;
      if (value.status !== 'uploading') {Toast.warning('该任务不再等待上传'); return;}
      setJob(value); setJobAgentId(value.agent_id); setUploadCount(value.uploaded); setRecovering(true);
      if (agents.some(item => item.agent_id === value.agent_id)) setActiveAgentId(value.agent_id);
    }).catch(error => {if (active) Toast.error(error instanceof Error ? error.message : '任务加载失败');});
    return () => {active = false;};
  }, [recoveryId, agents, setActiveAgentId]);

  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);

  function acceptFiles(values: FileList | File[]) {
    if (submitting || (job?.status === 'uploading' && !recovering)) return;
    const next = Array.from(values).filter(file => /\.(pdf|doc|docx|txt)$/i.test(file.name));
    if (next.length > 2000) { Toast.warning('单个任务最多 2000 个文件'); return; }
    setFiles(next);
  }

  async function uploadBatch(jobId: string, batch: Array<{file: File; position: number}>) {
    let lastError: unknown;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      const body = new FormData();
      batch.forEach(item => body.append('files', item.file));
      body.append('positions', JSON.stringify(batch.map(item => item.position)));
      try { return await api<IngestJob>(`/api/jobs/${encodeURIComponent(jobId)}/upload-batch`, {method: 'POST', body}); }
      catch (error) { lastError = error; }
    }
    throw lastError;
  }

  async function finishUpload(jobId: string, recoveryMode: boolean) {
    const current = await api<IngestJob>(`/api/jobs/${encodeURIComponent(jobId)}`);
    setJob(current); setUploadCount(current.uploaded);
    if (current.status !== 'uploading') return;
    const waiting = current.items.filter(item => item.status === 'pending');
    const pending = waiting.map(item => {
      if (!recoveryMode) return {file: files[item.position], position: item.position};
      if (item.expected_size == null) throw new Error('旧任务未记录文件大小，无法安全恢复，请取消后重新上传');
      const matches = files.filter(file => file.name === item.filename && file.size === item.expected_size);
      const duplicates = waiting.filter(other => other.filename === item.filename && other.expected_size === item.expected_size);
      if (matches.length !== 1 || duplicates.length !== 1) throw new Error(`无法唯一匹配文件：${item.filename}，请选择大小一致且无同名冲突的文件`);
      return {file: matches[0], position: item.position};
    });
    if ((!recoveryMode && current.total !== files.length) || pending.some(item => !item.file || item.file.name !== current.items[item.position]?.filename || (current.items[item.position]?.expected_size != null && item.file.size !== current.items[item.position]?.expected_size))) throw new Error('所选文件与原任务不一致，无法继续上传');
    let uploaded = current.uploaded;
    for (const batch of buildBatches(pending)) {
      const next = await uploadBatch(jobId, batch);
      uploaded += batch.length; setUploadCount(uploaded); setJob(next);
    }
    setJob(await api<IngestJob>(`/api/jobs/${encodeURIComponent(jobId)}/start`, {method: 'POST'}));
    Toast.success('文件已提交后台处理，可以离开当前页面');
  }

  async function startUpload() {
    if (!activeAgentId) { Toast.warning('请先选择知识库'); return; }
    if (!canWriteDocuments) { Toast.warning('当前知识库为只读'); return; }
    if (!files.length) { Toast.warning('请先选择文件'); return; }
    setSubmitting(true); setUploadCount(0);
    try {
      const created = await api<IngestJob>(`/api/agents/${encodeURIComponent(activeAgentId)}/ingest-jobs`, {
        method: 'POST', body: JSON.stringify({filenames: files.map(file => file.name), file_sizes: files.map(file => file.size), rewrite_enabled: rewriteEnabled,
          rewrite_prompt: rewritePrompt, rewrite_review_required: rewriteReview, workflow_enabled: workflowEnabled}),
      });
      setJob(created); setJobAgentId(activeAgentId); setRecovering(false);
      await finishUpload(created.job_id, false);
    } catch (error) { Toast.error(error instanceof Error ? error.message : '上传失败'); }
    finally { setSubmitting(false); }
  }

  async function resumeUpload() {
    if (!job || jobAgentId !== activeAgentId || !canWriteDocuments) return;
    setSubmitting(true);
    try { await finishUpload(job.job_id, recovering); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '继续上传失败'); }
    finally { setSubmitting(false); }
  }

  async function action(name: 'pause' | 'resume' | 'retry-failed' | 'cancel') {
    if (!job) return;
    try { setJob(await api<IngestJob>(`/api/jobs/${encodeURIComponent(job.job_id)}/${name}`, {method: 'POST'})); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '任务操作失败'); }
  }

  useEffect(() => {
    if (!job || ['completed', 'paused', 'cancelled'].includes(job.status)) {
      return;
    }
    const timer = window.setTimeout(async () => {
      try { setJob(await api<IngestJob>(`/api/jobs/${encodeURIComponent(job.job_id)}`)); }
      catch { /* Keep the current observable state; the next manual refresh can retry. */ }
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [job]);

  return <div className="upload-layout">
    <section className="upload-workspace">
      <div className={`drop-zone${dragging ? ' dragging' : ''}${files.length ? ' has-files' : ''}`} onDragOver={event => {event.preventDefault(); if (!submitting && (job?.status !== 'uploading' || recovering)) setDragging(true);}} onDragLeave={() => setDragging(false)} onDrop={event => {event.preventDefault(); setDragging(false); acceptFiles(event.dataTransfer.files);}}>
        <input ref={inputRef} type="file" multiple accept=".pdf,.doc,.docx,.txt" hidden onChange={event => event.target.files && acceptFiles(event.target.files)} />
        <input ref={folderRef} type="file" multiple hidden onChange={event => event.target.files && acceptFiles(event.target.files)} {...({webkitdirectory: ''} as React.InputHTMLAttributes<HTMLInputElement>)} />
        <div className="drop-icon"><IconUpload size="extra-large" /></div>
        <Typography.Title heading={5}>{files.length ? `已选择 ${files.length} 个文件` : '将文件拖到此处'}</Typography.Title>
        <Typography.Text type="tertiary">{recovering ? '请选择所有尚未上传的原始文件；文件名和大小必须一致' : files.length ? `总计 ${formatBytes(totalBytes)}，提交后在后台完成处理` : '支持 PDF、DOC、DOCX 和 TXT，单次最多 2000 个文件'}</Typography.Text>
        <div className="drop-actions"><Button disabled={submitting || (job?.status === 'uploading' && !recovering)} icon={<IconFile />} theme="solid" type="primary" onClick={() => inputRef.current?.click()}>选择文件</Button><Button disabled={submitting || (job?.status === 'uploading' && !recovering)} icon={<IconFolder />} onClick={() => folderRef.current?.click()}>选择目录</Button>{files.length > 0 && <Button disabled={submitting || (job?.status === 'uploading' && !recovering)} theme="borderless" onClick={() => setFiles([])}>清空</Button>}</div>
      </div>
      {files.length > 0 && <section className="content-section"><Typography.Title heading={6}>待上传文件</Typography.Title><Table dataSource={files.map((file, position) => ({position, name: file.name, size: file.size, duplicate: files.findIndex(item => item.name === file.name) !== position}))} rowKey="position" size="small" pagination={files.length > 10 ? {pageSize: 10} : false}><Table.Column title="文件" dataIndex="name" /><Table.Column title="大小" dataIndex="size" render={value => formatBytes(Number(value))} /><Table.Column title="提示" dataIndex="duplicate" render={value => value ? '文件名重复' : '—'} /></Table></section>}

      <div className="upload-submit">{job?.status === 'uploading' ? <Button theme="solid" type="primary" size="large" icon={<IconUpload />} loading={submitting} disabled={jobAgentId !== activeAgentId || !canWriteDocuments} onClick={() => void resumeUpload()}>继续上传</Button> : <Button theme="solid" type="primary" size="large" icon={<IconUpload />} loading={submitting} disabled={!files.length || submitting || !canWriteDocuments} onClick={() => void startUpload()}>开始入库</Button>}<span>{!canWriteDocuments && activeAgentId ? '当前知识库为只读，无法上传文档' : job?.status === 'uploading' && jobAgentId !== activeAgentId ? '请切回创建任务的知识库' : '上传完成后可安全离开，任务不会中断'}</span></div>
    </section>

    <aside className="upload-guide content-section"><h3>处理设置</h3><p className="resource-subtle">目标知识库</p><strong>{agents.find(item => item.agent_id === activeAgentId)?.name || '请先选择知识库'}</strong>      <section className="upload-options-panel">
        <div className="section-heading"><div><Typography.Title heading={6}>处理选项</Typography.Title><Typography.Text type="tertiary">所有文件使用当前知识库的解析与分块配置</Typography.Text></div></div>
        <div className="option-row"><Checkbox checked={rewriteEnabled} onChange={event => {setRewriteEnabled(Boolean(event.target.checked)); if (!event.target.checked) setRewriteReview(false);}}>使用 AI 改写 Markdown</Checkbox><Checkbox checked={rewriteReview} disabled={!rewriteEnabled} onChange={event => setRewriteReview(Boolean(event.target.checked))}>改写后需人工预览确认</Checkbox><Checkbox checked={workflowEnabled} onChange={event => setWorkflowEnabled(Boolean(event.target.checked))}>调用已配置的 Workflow</Checkbox></div>
        {rewriteEnabled && <TextArea value={rewritePrompt} onChange={setRewritePrompt} placeholder="输入改写规则；留空时使用系统默认规则" autosize rows={4} />}
      </section>
<h3>文件处理流程</h3><ol className="process-guide"><li>上传原始文件</li><li>按知识库配置解析与索引</li><li>通过检索查看内容与引用</li></ol></aside>
    {job && <aside className="active-job-panel">
      <div className="section-heading"><div><Typography.Title heading={6}>当前入库任务</Typography.Title><code>{job.job_id}</code></div><StatusTag status={job.status} /></div>
      <Progress percent={job.status === 'uploading' ? taskProgress(uploadCount, job.total) : taskProgress(job.completed, job.total)} showInfo />
      <div className="job-counts"><div><span>总计</span><strong>{job.total}</strong></div><div><span>成功</span><strong>{job.succeeded}</strong></div><div><span>待确认</span><strong>{job.preview_ready}</strong></div><div><span>失败</span><strong className={job.failed ? 'text-danger' : ''}>{job.failed}</strong></div></div>
      {canWriteDocuments && jobAgentId === activeAgentId && <div className="drawer-actions">{job.status === 'uploading' && <Button type="danger" disabled={submitting} onClick={() => void action('cancel')}>取消上传</Button>}{job.status === 'running' && <Button icon={<IconPause />} onClick={() => void action('pause')}>暂停</Button>}{job.status === 'paused' && <Button icon={<IconRestart />} onClick={() => void action('resume')}>恢复</Button>}{job.failed > 0 && <Button icon={<IconRefresh />} onClick={() => void action('retry-failed')}>重试失败项</Button>}</div>}
      <Table<JobItem> dataSource={job.items.slice(0, 12)} pagination={false} rowKey="item_id" size="small" className="active-job-table">
        <Table.Column title="文件" dataIndex="filename" render={value => <span className="filename-cell">{String(value)}</span>} /><Table.Column title="状态" dataIndex="status" width={90} render={value => <StatusTag status={String(value)} />} />
      </Table>
      {job.items.length > 12 && <Button block theme="borderless" onClick={() => navigate('/tasks')}>在任务中心查看全部 {job.items.length} 项</Button>}
    </aside>}
  </div>;
}
