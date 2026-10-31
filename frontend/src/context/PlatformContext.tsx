import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Toast } from '@douyinfe/semi-ui';
import { api } from '../api/client';
import type { AgentSummary, Session } from '../types';

interface PlatformState {
  session: Session;
  agents: AgentSummary[];
  activeAgentId: string;
  activeAgent?: AgentSummary;
  canWriteDocuments: boolean;
  loadingAgents: boolean;
  setActiveAgentId: (value: string) => void;
  reloadAgents: () => Promise<void>;
}

interface KnowledgeSelection {knowledge_base_id: string; name: string; runtime_id: string; can_write_documents: boolean;}

const PlatformContext = createContext<PlatformState | null>(null);

export function PlatformProvider({session, children}: {session: Session; children: React.ReactNode}) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [writableAgents, setWritableAgents] = useState<string[]>([]);
  const [activeAgentId, setActiveAgentIdState] = useState(localStorage.getItem('active-agent') || '');
  const [loadingAgents, setLoadingAgents] = useState(true);

  const reloadAgents = useCallback(async () => {
    setLoadingAgents(true);
    try {
      const bases = await api<KnowledgeSelection[]>('/api/knowledge-bases');
      const values: AgentSummary[] = bases.map((item, index) => ({agent_id: item.runtime_id, name: item.name, description: '', is_default: index === 0, allowed_tools: []}));
      setWritableAgents(bases.filter(item => item.can_write_documents).map(item => item.runtime_id));
      setAgents(values);
      setActiveAgentIdState(current => {
        if (values.some(item => item.agent_id === current)) return current;
        return values.find(item => item.is_default)?.agent_id || values[0]?.agent_id || '';
      });
    } catch (error) {
      Toast.error(error instanceof Error ? error.message : '无法加载 Agent');
    } finally {
      setLoadingAgents(false);
    }
  }, []);

  useEffect(() => { void reloadAgents(); }, [reloadAgents]);
  useEffect(() => {
    if (activeAgentId) localStorage.setItem('active-agent', activeAgentId);
  }, [activeAgentId]);

  const setActiveAgentId = useCallback((value: string) => setActiveAgentIdState(value), []);
  const value = useMemo(() => ({
    session, agents, activeAgentId,
    activeAgent: agents.find(item => item.agent_id === activeAgentId),
    canWriteDocuments: writableAgents.includes(activeAgentId),
    loadingAgents, setActiveAgentId, reloadAgents,
  }), [session, agents, activeAgentId, writableAgents, loadingAgents, setActiveAgentId, reloadAgents]);

  return <PlatformContext.Provider value={value}>{children}</PlatformContext.Provider>;
}

export function usePlatform() {
  const value = useContext(PlatformContext);
  if (!value) throw new Error('PlatformContext is unavailable');
  return value;
}
