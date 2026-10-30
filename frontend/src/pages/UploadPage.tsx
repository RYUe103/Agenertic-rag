import { useEffect, useRef, useState } from 'react';
import { Button, Checkbox, Progress, Table, TextArea, Toast, Typography } from '@douyinfe/semi-ui';
import { IconFile, IconFolder, IconPause, IconRefresh, IconRestart, IconUpload } from '@douyinfe/semi-icons';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { StatusTag } from '../components/StatusTag';
import { usePlatform } from '../context/PlatformContext';
import type { IngestJob, JobItem } from '../types';
import { formatBytes, taskProgress } from '../utils/format';

function buildBatches(files: File[], maxFiles = 25, maxBytes = 120 * 1024 * 1024) {
  const batches: Array<Array<{file: File; position: number}>> = [];
  let batch: Array<{file: File; position: number}> = [];
  let bytes = 0;
  files.forEach((file, position) => {
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
  const {activeAgentId} = usePlatform();
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

  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);

  function acceptFiles(values: FileList | File[]) {
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

  async function startUpload() {
    if (!activeAgentId) { Toast.warning('请先选择 Agent'); return; }
    if (!files.length) { Toast.warning('请先选择文件'); return; }
    setSubmitting(true); setUploadCount(0);
    try {
      const created = await api<IngestJob>(`/api/agents/${encodeURIComponent(activeAgentId)}/ingest-jobs`, {
        method: 'POST', body: JSON.stringify({
          filenames: files.map(file => file.name), rewrite_enabled: rewriteEnabled,
          rewrite_prompt: rewritePrompt, rewrite_review_required: rewriteReview,
          workflow_enabled: workflowEnabled,
        }),
      });
      setJob(created);
      let uploaded = 0;
      for (const batch of buildBatches(files)) {
        const current = await uploadBatch(created.job_id, batch);
        uploaded += batch.length; setUploadCount(uploaded); setJob(current);
      }
      setJob(await api<IngestJob>(`/api/jobs/${encodeURIComponent(created.job_id)}/start`, {method: 'POST'}));
      Toast.success('文件已提交后台处理，可以离开当前页面');
    } catch (error) { Toast.error(error instanceof Error ? error.message : '上传失败'); setSubmitting(false); }
  }

  async function action(name: 'pause' | 'resume' | 'retry-failed') {
    if (!job) return;
    try { setJob(await api<IngestJob>(`/api/jobs/${encodeURIComponent(job.job_id)}/${name}`, {method: 'POST'})); }
    catch (error) { Toast.error(error instanceof Error ? error.message : '任务操作失败'); }
  }

  useEffect(() => {
    if (!job || ['completed', 'paused'].includes(job.status)) {
      if (job?.status === 'completed') setSubmitting(false);
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
      <div className={`drop-zone${dragging ? ' dragging' : ''}${files.length ? ' has-files' : ''}`} onDragOver={event => {event.preventDefault(); setDragging(true);}} onDragLeave={() => setDragging(false)} onDrop={event => {event.preventDefault(); setDragging(false); acceptFiles(event.dataTransfer.files);}}>
        <input ref={inputRef} type="file" multiple accept=".pdf,.doc,.docx,.txt" hidden onChange={event => event.target.files && acceptFiles(event.target.files)} />
        <input ref={folderRef} type="file" multiple hidden onChange={event => event.target.files && acceptFiles(event.target.files)} {...({webkitdirectory: ''} as React.InputHTMLAttributes<HTMLInputElement>)} />
        <div className="drop-icon"><IconUpload size="extra-large" /></div>
        <Typography.Title heading={5}>{files.length ? `已选择 ${files.length} 个文件` : '将文件拖到此处'}</Typography.Title>
        <Typography.Text type="tertiary">{files.length ? `总计 ${formatBytes(totalBytes)}，提交后在后台完成处理` : '支持 PDF、DOC、DOCX 和 TXT，单次最多 2000 个文件'}</Typography.Text>
        <div className="drop-actions"><Button icon={<IconFile />} theme="solid" type="primary" onClick={() => inputRef.current?.click()}>选择文件</Button><Button icon={<IconFolder />} onClick={() => folderRef.current?.click()}>选择目录</Button>{files.length > 0 && <Button theme="borderless" onClick={() => setFiles([])}>清空</Button>}</div>
      </div>

      <section className="upload-options-panel">
        <div className="section-heading"><div><Typography.Title heading={6}>处理选项</Typography.Title><Typography.Text type="tertiary">所有文件使用当前知识库的解析与分块配置</Typography.Text></div></div>
        <div className="option-row"><Checkbox checked={rewriteEnabled} onChange={event => {setRewriteEnabled(Boolean(event.target.checked)); if (!event.target.checked) setRewriteReview(false);}}>使用 AI 改写 Markdown</Checkbox><Checkbox checked={rewriteReview} disabled={!rewriteEnabled} onChange={event => setRewriteReview(Boolean(event.target.checked))}>改写后需人工预览确认</Checkbox><Checkbox checked={workflowEnabled} onChange={event => setWorkflowEnabled(Boolean(event.target.checked))}>调用已配置的 Workflow</Checkbox></div>
        {rewriteEnabled && <TextArea value={rewritePrompt} onChange={setRewritePrompt} placeholder="输入改写规则；留空时使用系统默认规则" autosize rows={4} />}
      </section>
      <div className="upload-submit"><Button theme="solid" type="primary" size="large" icon={<IconUpload />} loading={submitting && !job} disabled={!files.length || submitting} onClick={() => void startUpload()}>开始入库</Button><span>上传完成后可安全离开，任务不会中断</span></div>
    </section>

    {job && <aside className="active-job-panel">
      <div className="section-heading"><div><Typography.Title heading={6}>当前入库任务</Typography.Title><code>{job.job_id}</code></div><StatusTag status={job.status} /></div>
      <Progress percent={job.status === 'uploading' ? taskProgress(uploadCount, files.length) : taskProgress(job.completed, job.total)} showInfo />
      <div className="job-counts"><div><span>总计</span><strong>{job.total}</strong></div><div><span>成功</span><strong>{job.succeeded}</strong></div><div><span>待确认</span><strong>{job.preview_ready}</strong></div><div><span>失败</span><strong className={job.failed ? 'text-danger' : ''}>{job.failed}</strong></div></div>
      <div className="drawer-actions">{job.status === 'running' && <Button icon={<IconPause />} onClick={() => void action('pause')}>暂停</Button>}{job.status === 'paused' && <Button icon={<IconRestart />} onClick={() => void action('resume')}>恢复</Button>}{job.failed > 0 && <Button icon={<IconRefresh />} onClick={() => void action('retry-failed')}>重试失败项</Button>}</div>
      <Table<JobItem> dataSource={job.items.slice(0, 12)} pagination={false} rowKey="item_id" size="small" className="active-job-table">
        <Table.Column title="文件" dataIndex="filename" render={value => <span className="filename-cell">{String(value)}</span>} /><Table.Column title="状态" dataIndex="status" width={90} render={value => <StatusTag status={String(value)} />} />
      </Table>
      {job.items.length > 12 && <Button block theme="borderless" onClick={() => navigate('/tasks')}>在任务中心查看全部 {job.items.length} 项</Button>}
    </aside>}
  </div>;
}
