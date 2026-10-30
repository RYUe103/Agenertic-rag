import { Table, Tag, Typography } from '@douyinfe/semi-ui';

const capabilities = [
  {area: '知识与文档', capability: '文档入库、MinerU/Qwen-VL 解析、Dify 索引、Markdown 预览', status: '已可用', note: '已连接真实后端接口'},
  {area: '任务系统', capability: '批量入库、暂停恢复、失败重试、批量删除', status: '已可用', note: '基于持久化 SQLite 队列'},
  {area: '问答与 Agent', capability: 'LangGraph Agent、本地 grep、Dify 检索、元数据筛选、引用来源', status: '已可用', note: '已连接真实问答接口'},
  {area: 'MCP 与外部 AI', capability: 'MCP Server、授权工具、Token 轮换与调用日志', status: '缺少后端', note: '尚无注册表、鉴权和审计 API'},
  {area: 'Sandbox', capability: 'Python/Shell 执行、资源限制、只读 Knowledge SDK、输出文件', status: '缺少后端', note: '尚无隔离运行时和任务数据模型'},
  {area: '治理与审计', capability: '租户、角色、权限、审计日志、历史监控指标', status: '部分可用', note: '已有管理员/LDAP 登录与知识库所有者隔离'},
];

export function CapabilitiesPage() {
  return <div className="page-stack"><section className="capability-intro"><Typography.Title heading={5}>后端能力是唯一事实来源</Typography.Title><Typography.Paragraph>本页只列出仓库中已实现的服务和明确的接口缺口。缺失的 MCP、Sandbox 和审计能力不会用前端模拟数据冒充。</Typography.Paragraph></section><section className="content-section flush-section"><Table dataSource={capabilities} pagination={false} rowKey="area"><Table.Column title="领域" dataIndex="area" width={150} render={value => <strong>{String(value)}</strong>} /><Table.Column title="能力" dataIndex="capability" /><Table.Column title="实现状态" dataIndex="status" width={130} render={value => <Tag color={value === '已可用' ? 'green' : value === '部分可用' ? 'amber' : 'grey'}>{String(value)}</Tag>} /><Table.Column title="当前边界" dataIndex="note" /></Table></section></div>;
}
