# 外部 REST 接入

在平台的“AI 接入”中创建接入，选择允许访问的知识库和只读检索工具。创建或轮换时只显示一次密钥；服务端只保存哈希。接入所有者或管理员可以查看调用记录、轮换密钥和撤销接入。旧密钥在轮换后立即失效。

使用创建时显示的密钥，通过 HTTPS 或可信的本地网络发送 Bearer 请求。实际地址按部署域名和反向代理前缀调整：

```http
GET /api/external/v1/tools
Authorization: Bearer <接入密钥>
```

响应列出接入授权的工具和输入 JSON Schema。调用工具时，在路径中指定工具名称，并在请求体中指定已授权的知识库 ID：

```http
POST /api/external/v1/tools/find_files
Authorization: Bearer <接入密钥>
Content-Type: application/json

{
  "knowledge_base_id": "your-knowledge-base-id",
  "arguments": {"query": "管道标准", "limit": 20}
}
```

返回 `items` 和 `count`。没有授权的知识库或工具返回 403，无效或已撤销的密钥返回 401。用户失去知识库所有权后，相应的非管理员接入也不能继续调用该知识库。调用记录保存工具、知识库、状态、时间和耗时，不保存密钥、参数或检索结果。

此接口按接入范围暴露已授权工具；写入工具还需要所有者当前的文档写权限和人工审批。代码执行不通过该接口开放。已有的全局 `EXTERNAL_API_KEY` 问答接口独立保留，其权限不受这些接入记录约束。

## OpenAI 兼容接口

相同接入密钥可用于 `GET /v1/models` 和非流式 `POST /v1/chat/completions`。模型列表仅包含接入所有者当前可访问且已授权的知识库，模型 ID 为 `@kb:<知识库 ID>`。请求的 `model` 必须使用该 ID；Agent 的检索工具受接入授权和管理员工具开关限制，代码执行不开放。当前仅支持文本形式的 `user` 和 `assistant` 消息，最后一条必须为 `user`；未采集 token 用量时不返回 `usage`。

## MCP Streamable HTTP

安装 MCP 可选依赖并重启服务：

```bash
pip install -e '.[mcp]'
```

使用相同的接入密钥，通过 Streamable HTTP 连接 `/api/mcp/`，请求头填写 `Authorization: Bearer <接入密钥>`。列出的工具和调用权限与上述 REST 接入一致；每次调用都需要填写 `knowledge_base_id` 和 `arguments`。页面在创建或轮换密钥时提供 MCP 配置示例。服务使用无状态传输，支持重连和服务重启。

为防止 DNS 重绑定，默认仅允许本机 Host。内网部署时必须在 `.env` 的 `MCP_ALLOWED_HOSTS` 中列出实际请求 Host，例如 `["127.0.0.1:*","kb.example.com"]`；浏览器跨域客户端还需在 `MCP_ALLOWED_ORIGINS` 中列出允许的来源。生产环境应使用 HTTPS。原有离线安装包没有 MCP 及其依赖的 wheels，不能直接在离线环境启用，需要先准备对应平台的依赖包。
# 需要人工审批的工具

工具列表的 `requires_approval` 为 true 时，客户端先以同样的 `knowledge_base_id` 和 `arguments` POST 到 `/api/external/v1/tools/{tool_name}/approvals`，使用接入 Bearer 密钥认证。管理员在工具中心审核后，客户端可 GET `/api/external/v1/approvals/{approval_id}` 查询状态。状态为 `approved` 时，将返回的 `id` 作为 `approval_id` 添加到原工具调用中。参数、接入、知识库和工具必须一致，批准仅能使用一次，申请 15 分钟后过期；执行时仍会检查当前授权和工具状态。MCP 调用同样接受 `approval_id`，申请和状态查询目前使用 REST。审批参数会保存在本地数据库，供管理员审阅。
