import { lazy, Suspense, useEffect, useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Spin } from '@douyinfe/semi-ui';
import { api } from './api/client';
import { AppShell } from './components/AppShell';
import { PlatformProvider } from './context/PlatformContext';
import { LoginPage } from './pages/LoginPage';
import type { Session } from './types';

const DashboardPage = lazy(() => import('./pages/DashboardPage').then(module => ({default: module.DashboardPage})));
const DocumentsPage = lazy(() => import('./pages/DocumentsPage').then(module => ({default: module.DocumentsPage})));
const TasksPage = lazy(() => import('./pages/TasksPage').then(module => ({default: module.TasksPage})));
const UploadPage = lazy(() => import('./pages/UploadPage').then(module => ({default: module.UploadPage})));
const KnowledgePage = lazy(() => import('./pages/KnowledgePage').then(module => ({default: module.KnowledgePage})));
const QaPage = lazy(() => import('./pages/QaPage').then(module => ({default: module.QaPage})));
const AgentsPage = lazy(() => import('./pages/AgentsPage').then(module => ({default: module.AgentsPage})));
const ModelsPage = lazy(() => import('./pages/ModelsPage').then(module => ({default: module.ModelsPage})));
const CapabilitiesPage = lazy(() => import('./pages/CapabilitiesPage').then(module => ({default: module.CapabilitiesPage})));
const ToolsPage = lazy(() => import('./pages/ToolsPage').then(module => ({default: module.ToolsPage})));
const IntegrationsPage = lazy(() => import('./pages/IntegrationsPage').then(module => ({default: module.IntegrationsPage})));
const AgentRunsPage = lazy(() => import('./pages/AgentRunsPage').then(module => ({default: module.AgentRunsPage})));
const AuditPage = lazy(() => import('./pages/AuditPage').then(module => ({default: module.AuditPage})));
const SandboxPage = lazy(() => import('./pages/SandboxPage').then(module => ({default: module.SandboxPage})));
const MonitoringPage = lazy(() => import('./pages/MonitoringPage').then(module => ({default: module.MonitoringPage})));
const AccessPage = lazy(() => import('./pages/AccessPage').then(module => ({default: module.AccessPage})));
const OrganizationsPage = lazy(() => import('./pages/OrganizationsPage').then(module => ({default: module.OrganizationsPage})));

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<Session>('/api/auth/session')
      .then(setSession)
      .catch(() => setSession({authenticated: false, username: '', is_admin: false, permissions: []}))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="app-loading"><Spin size="large" tip="正在加载平台" /></div>;
  if (!session?.authenticated) return <LoginPage onLogin={setSession} />;

  return (
    <PlatformProvider session={session}>
      <Suspense fallback={<div className="route-loading"><Spin tip="正在加载页面" /></div>}><Routes>
        <Route element={<AppShell onLogout={() => setSession({authenticated: false, username: '', is_admin: false})} />}>
          <Route index element={<DashboardPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="upload" element={<UploadPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="qa" element={<QaPage />} />
          <Route path="agents" element={<AgentsPage />} />
          <Route path="models" element={<ModelsPage />} />
          <Route path="capabilities" element={<CapabilitiesPage />} />
          <Route path="tools" element={<ToolsPage />} />
          <Route path="integrations" element={<IntegrationsPage />} />
          <Route path="agent-runs" element={<AgentRunsPage />} />
          <Route path="audit" element={session.is_admin ? <AuditPage /> : <Navigate to="/" replace />} />
          <Route path="sandbox" element={<SandboxPage />} />
          <Route path="monitoring" element={session.is_admin ? <MonitoringPage /> : <Navigate to="/" replace />} />
          <Route path="access" element={session.is_admin ? <AccessPage /> : <Navigate to="/" replace />} />
          <Route path="organizations" element={session.is_admin ? <OrganizationsPage /> : <Navigate to="/" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes></Suspense>
    </PlatformProvider>
  );
}
