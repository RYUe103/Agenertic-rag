import { Tag } from '@douyinfe/semi-ui';

const labels: Record<string, string> = {
  pending: '待处理', uploading: '上传中', uploaded: '已上传', queued: '排队中',
  processing: '处理中', running: '运行中', retrying: '重试中', paused: '已暂停',
  success: '已完成', succeeded: '已完成', completed: '已完成', duplicate: '重复文件',
  failed: '失败', cancelling: '取消中', cancelled: '已取消', preview_ready: '待确认', disabled: '已禁用',
  not_requested: '未启用', confirmed: '已确认', unknown: '结果未知', timeout: '超时',
};

export function StatusTag({status}: {status?: string | null}) {
  const value = (status || 'pending').toLowerCase();
  const color = value.includes('fail') ? 'red'
    : value.includes('success') || value === 'completed' || value === 'confirmed' ? 'green'
      : value.includes('running') || value.includes('processing') || value.includes('retry') ? 'blue'
        : value.includes('cancel') || value === 'disabled' ? 'grey' : 'amber';
  return <Tag type="light" color={color} size="small">{labels[value] || status}</Tag>;
}
