import { Button, Dropdown, Select, Tooltip, Typography } from '@douyinfe/semi-ui';
import {
  IconArticle, IconBriefcase, IconChevronDown, IconCloud, IconExit,
  IconFile, IconHome, IconList, IconMoon, IconSearch, IconServer, IconSetting, IconSun, IconUpload,
} from '@douyinfe/semi-icons';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { usePlatform } from '../context/PlatformContext';

const navItems = [
  {to: '/', label: '运行总览', icon: <IconHome />},
  {to: '/documents', label: '文档管理', icon: <IconFile />},
  {to: '/upload', label: '上传文档', icon: <IconUpload />},
  {to: '/tasks', label: '任务中心', icon: <IconList />},
  {to: '/knowledge', label: '知识库', icon: <IconArticle />},
  {to: '/qa', label: '问答检索', icon: <IconSearch />},
  {to: '/tools', label: '工具中心', icon: <IconSetting />},
  {to: '/integrations', label: 'AI 接入', icon: <IconCloud />},
  {to: '/agent-runs', label: 'Agent 执行', icon: <IconList />},
  {to: '/sandbox', label: 'Sandbox', icon: <IconServer />},
];

const adminItems = [
  {to: '/agents', label: 'Agent 管理', icon: <IconBriefcase />},
  {to: '/models', label: '模型连接', icon: <IconCloud />},
  {to: '/capabilities', label: '平台能力', icon: <IconServer />},
  {to: '/audit', label: '审计日志', icon: <IconList />, adminOnly: true},
  {to: '/monitoring', label: '运行监控', icon: <IconServer />, adminOnly: true},
  {to: '/access', label: '用户与权限', icon: <IconBriefcase />, adminOnly: true},
  {to: '/organizations', label: '组织与租户', icon: <IconBriefcase />, adminOnly: true},
];

const pageTitles: Record<string, [string, string]> = {
  '/': ['运行总览', '知识服务、文档处理与任务队列状态'],
  '/documents': ['文档管理', '查看、筛选和维护已入库文档'],
  '/upload': ['上传文档', '批量提交文件，后台完成解析与索引'],
  '/tasks': ['任务中心', '跟踪入库、重试和删除任务'],
  '/knowledge': ['知识库', '管理知识连接、解析与分块策略'],
  '/qa': ['问答检索', '基于知识库提问，查看回答、来源与执行轨迹'],
  '/tools': ['工具中心', '查看和测试当前知识库授权的检索工具'],
  '/integrations': ['AI 接入', '管理外部客户端及授权范围'],
  '/agent-runs': ['Agent 执行', '查看问答与检索的执行记录'],
  '/agents': ['Agent 管理', '查看 Agent 的知识库与工具授权'],
  '/models': ['模型连接', '管理 Planner 与 Answer 模型链'],
  '/capabilities': ['平台能力', '已实现服务与后端接口缺口'],
  '/audit': ['审计日志', '查看平台操作记录'],
  '/sandbox': ['Sandbox', '配置受控代码执行与查看执行记录'],
  '/monitoring': ['运行监控', '查看真实采集的 API、任务和 Agent 指标'],
  '/access': ['用户与权限', '管理角色、权限和用户绑定'],
  '/organizations': ['组织与租户', '管理组织、租户和成员关系'],
};

export function AppShell({onLogout}: {onLogout: () => void}) {
  const location = useLocation();
  const {session, agents, activeAgentId, setActiveAgentId, loadingAgents} = usePlatform();
  const title = pageTitles[location.pathname] || pageTitles['/'];
  const [dark, setDark] = useState(localStorage.getItem('theme-mode') === 'dark');

  useEffect(() => {
    document.body.setAttribute('theme-mode', dark ? 'dark' : 'light');
    localStorage.setItem('theme-mode', dark ? 'dark' : 'light');
  }, [dark]);

  async function logout() {
    await api<void>('/api/auth/logout', {method: 'POST'});
    onLogout();
  }

  return (
    <div className="app-frame">
      <aside className="app-sidebar">
        <div className="brand-lockup"><div className="brand-symbol">S</div><div><strong>SANSUO AI</strong><span>Knowledge Platform</span></div></div>
        <nav className="side-nav" aria-label="主导航">
          <div className="nav-group-label">工作台</div>
          {navItems.map(item => <NavLink key={item.to} to={item.to} end={item.to === '/'} aria-label={item.label} className={({isActive}) => isActive ? 'nav-link active' : 'nav-link'}>{item.icon}<span className="nav-label">{item.label}</span></NavLink>)}
          <div className="nav-group-label">配置与能力</div>
          {adminItems.filter(item => !item.adminOnly || session.is_admin).map(item => <NavLink key={item.to} to={item.to} aria-label={item.label} className={({isActive}) => isActive ? 'nav-link active' : 'nav-link'}>{item.icon}<span className="nav-label">{item.label}</span></NavLink>)}
        </nav>
        <div className="sidebar-footer">
          <Dropdown trigger="click" position="topLeft" render={
            <Dropdown.Menu>
              <Dropdown.Item icon={<IconSetting />} disabled>{session.is_admin ? '管理员' : '普通用户'}</Dropdown.Item>
              <Dropdown.Divider />
              <Dropdown.Item icon={<IconExit />} onClick={() => void logout()}>退出登录</Dropdown.Item>
            </Dropdown.Menu>
          }>
            <button className="user-menu"><span className="avatar">{session.username.slice(0, 1).toUpperCase()}</span><span className="user-copy"><strong>{session.username}</strong><small>{session.is_admin ? '平台管理员' : '知识库用户'}</small></span><IconChevronDown /></button>
          </Dropdown>
        </div>
      </aside>
      <section className="app-main">
        <header className="topbar">
          <div className="page-heading"><Typography.Title heading={4}>{title[0]}</Typography.Title><Typography.Text type="tertiary">{title[1]}</Typography.Text></div>
          <div className="topbar-actions">
            <span className="agent-label">当前知识库</span>
            <Select placeholder={agents.length ? '请选择知识库' : '暂无可用知识库'} value={activeAgentId || undefined} loading={loadingAgents} onChange={value => setActiveAgentId(String(value))} className="agent-select" emptyContent="暂无知识库">
              {agents.map(agent => <Select.Option key={agent.agent_id} value={agent.agent_id}>{agent.name}</Select.Option>)}
            </Select>
            <Tooltip content={dark ? '切换为浅色主题' : '切换为深色主题'}><Button icon={dark ? <IconSun /> : <IconMoon />} theme="borderless" onClick={() => setDark(value => !value)} aria-label="切换主题" /></Tooltip>
          </div>
        </header>
        <div className="page-content"><Outlet /></div>
      </section>
    </div>
  );
}
