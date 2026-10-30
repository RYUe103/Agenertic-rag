import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Toast } from '@douyinfe/semi-ui';
import { api } from '../api/client';
import type { AgentSummary, Session } from '../types';

interface PlatformState {
  session: Session;
  agents: AgentSummary[];
  activeAgentId: string;
  activeAgent?: AgentSummary;
  loadingAgents: boolean;
  setActiveAgentId: (value: string) => void;
  reloadAgents: () => Promise<void>;
}

const PlatformContext = createContext<PlatformState | null>(null);

export function PlatformProvider({session, children}: {session: Session; children: React.ReactNode}) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [activeAgentId, setActiveAgentIdState] = useState(localStorage.getItem('active-agent') || '');
  const [loadingAgents, setLoadingAgents] = useState(true);

  const reloadAgents = useCallback(async () => {
    setLoadingAgents(true);
    try {
      const values = await api<AgentSummary[]>('/api/agents');
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
    loadingAgents, setActiveAgentId, reloadAgents,
  }), [session, agents, activeAgentId, loadingAgents, setActiveAgentId, reloadAgents]);

  return <PlatformContext.Provider value={value}>{children}</PlatformContext.Provider>;
}

export function usePlatform() {
  const value = useContext(PlatformContext);
  if (!value) throw new Error('PlatformContext is unavailable');
  return value;
}
