import { OverviewStats } from '../components/OverviewStats';
import { Tag } from '@douyinfe/semi-ui';
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { Health } from '../types';

const capabilities = [
  {area: '知识与文档', capability: '文档入库、MinerU/Qwen-VL 解析、Dify 索引、Markdown 预览', status: '已可用', note: '已连接真实后端接口'},
  {area: '任务系统', capability: '批量入库、暂停恢复、失败重试、批量删除', status: '已可用', note: '基于持久化 SQLite 队列'},
  {area: '问答与 Agent', capability: 'LangGraph Agent、本地 grep、Dify 检索、元数据筛选、引用来源', status: '已可用', note: '已连接真实问答接口'},
  {area: 'Sandbox', capability: 'Python/Shell 执行、资源限制、只读 Knowledge SDK、输出文件', status: '部分可用', note: 'Docker 隔离、资源采集和按用户执行记录已接入；异常恢复与依赖安装仍受限'},
  {area: '治理与审计', capability: '租户、角色、权限、LDAP 配置、审计日志、历史监控指标', status: '部分可用', note: '组织成员已参与知识库访问授权；仍需更细粒度文档写权限与完整联合身份管理'},
];

export function CapabilitiesPage() {
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {void api<Health>('/api/health').then(setHealth).catch(() => setHealth(null));}, []);
  const rows = [...capabilities, {area: 'MCP 与外部 AI', capability: 'REST、MCP、工具授权、密钥轮换与调用记录', status: '部分可用', note: health ? (health.mcp_available ? 'MCP 服务已启用；完整审计仍待实现' : 'REST 可用；MCP 组件未启用') : '服务状态未获取'}];
  return <div className="page-stack"><OverviewStats items={[{label:'能力领域',value:rows.length,note:'当前实现说明'},{label:'已可用',value:rows.filter(x => x.status === '已可用').length,note:'功能范围'},{label:'部分可用',value:rows.filter(x => x.status === '部分可用').length,note:'请留意边界说明'},{label:'MCP 服务',value:health ? (health.mcp_available ? '已启用' : '未启用') : '—',note:'实际服务配置'}]} /><section className="resource-grid">{rows.map(item => <article className="resource-card" key={item.area}><div className="resource-top"><span className="resource-symbol">◇</span><Tag color={item.status === '已可用' ? 'green' : 'amber'}>{item.status}</Tag></div><h3>{item.area}</h3><p>{item.capability}</p><div className="capability-boundary">{item.note}</div></article>)}</section></div>;
}
