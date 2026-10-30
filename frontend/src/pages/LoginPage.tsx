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
    <main className="login-page">
      <section className="login-panel">
        <div className="product-mark" aria-hidden="true"><span>S</span></div>
        <Typography.Title heading={2}>SANSUO AI</Typography.Title>
        <Typography.Text type="tertiary">知识与工具平台</Typography.Text>
        <Form onSubmit={submit} className="login-form">
          <Form.Input field="username" label="账号" prefix={<IconUser />} rules={[{required: true, message: '请输入账号'}]} />
          <Form.Input field="password" label="密码" mode="password" prefix={<IconLock />} rules={[{required: true, message: '请输入密码'}]} />
          {error && <div className="form-error" role="alert">{error}</div>}
          <Button htmlType="submit" theme="solid" type="primary" loading={loading} block>登录平台</Button>
        </Form>
        <p className="login-footnote">支持管理员账号与组织域账号</p>
      </section>
    </main>
  );
}
