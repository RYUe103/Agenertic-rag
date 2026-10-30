# 后续：LDAP / OIDC / 跨系统接入备忘

## 目标

当前 Agentic RAG 先作为独立服务运行。后续接入三所小智和其他内网系统时，需要实现统一登录、按用户授权访问 Agent/知识库，以及系统到系统的服务账号调用。

## 推荐架构

```text
LDAP（用户目录）
  -> Keycloak / Authentik / FreeIPA（身份中心）
  -> OIDC/OAuth2
     -> 三所小智
     -> Agentic RAG 管理前端/API
     -> 其他内网系统
```

LDAP 负责账号和用户组；OIDC 负责浏览器单点登录和 Token。不要让每个业务系统直接实现 LDAP 登录，也不要在系统之间传递用户密码。

## 权限模型

```text
用户/用户组 -> 角色 -> Agent -> 知识库 -> 工具/操作
```

建议至少有：

- `kb-user`：提问和查看引用
- `kb-uploader`：上传文件
- `kb-admin`：配置 Agent、Workflow、元数据和知识库
- `service-xiaozhi`：三所小智的系统调用账号
- `service-common-kb`：公共知识库后台服务账号

每个业务系统使用独立服务账号，不能共享一个 API Key，便于审计和吊销。

## 三所小智单点登录

三所小智和 Agentic RAG 都注册为同一 OIDC Provider 的客户端。用户在三所小智登录后，三所小智调用 RAG API 时携带：

```http
Authorization: Bearer <OIDC access_token>
```

RAG 服务验证 issuer、audience、签名、过期时间和用户组，再决定可访问的 Agent。前端嵌入可以后续提供 JS SDK；后端接入优先提供 OpenAPI。

## 后续实施顺序

1. 为 RAG 增加 OIDC JWT 验证中间件。
2. 增加用户、组、角色与 Agent 的授权表。
3. 增加 `POST /api/v1/agents/{agent_id}/ask` 等稳定版本 API。
4. 三所小智使用 OIDC Token 调用 RAG。
5. 提供 OpenAPI 文档和可嵌入的问答组件。

## 安全约束

- LDAP 密码只在身份中心验证，RAG 不保存密码。
- Dify Dataset Key、Workflow App Key 和服务账号 Token 分开保存。
- 服务账号按系统隔离，支持吊销和审计。
- 所有 API 按 Agent 和知识库做授权，不能只相信前端传来的 `agent_id`。
- 内网部署也要配置反向代理、HTTPS（或可信内网 TLS）、上传大小限制和审计日志。
