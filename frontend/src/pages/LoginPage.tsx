import { useState } from 'react';
import { Button, Form, Typography } from '@douyinfe/semi-ui';
import { IconLock, IconUser } from '@douyinfe/semi-icons';
import { api } from '../api/client';
import type { Session } from '../types';

export function LoginPage({onLogin}: {onLogin: (session: Session) => void}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function submit(values: Record<string, unknown>) {
    setLoading(true);
    setError('');
    try {
      const session = await api<Session>('/api/auth/login', {
        method: 'POST', body: JSON.stringify(values),
      });
      onLogin(session);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-page login-redesign">
      <aside className="login-story"><div className="login-brand"><span>S</span><div><strong>SANSUO AI</strong><small>KNOWLEDGE PLATFORM</small></div></div><div className="login-story-copy"><h1>让企业知识<br />真正产生价值<span>。</span></h1><p>统一管理知识资产，连接智能体与业务工具。<br />让每一次搜索都有依据，每一次决策都可追溯。</p><div className="login-features"><span>企业级知识管理</span><span>可信 RAG 检索</span><span>智能体执行</span></div></div></aside>
      <section className="login-panel">
        <div className="login-welcome">WELCOME BACK</div>
        <Typography.Title heading={2}>登录到工作台</Typography.Title>
        <Typography.Text type="tertiary">使用你的工作账号继续访问</Typography.Text>
        <Form onSubmit={submit} className="login-form">
          <Form.Input field="username" label="账号" prefix={<IconUser />} rules={[{required: true, message: '请输入账号'}]} />
          <Form.Input field="password" label="密码" mode="password" prefix={<IconLock />} rules={[{required: true, message: '请输入密码'}]} />
          {error && <div className="form-error" role="alert">{error}</div>}
          <Button htmlType="submit" theme="solid" type="primary" loading={loading} block>登录工作台 →</Button>
        </Form>
        <p className="login-footnote">支持管理员、本地用户与组织域账号</p>
      </section>
    </main>
  );
}
