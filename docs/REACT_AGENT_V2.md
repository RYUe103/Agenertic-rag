# ReAct Agent V2

2026-11-01。第四阶段，基线 `9f0c479`。默认仍为 legacy；没有生产文档迁移、重新解析或远端上传。

## 执行图与工具

```text
START → agent ──无工具/预算终止──→ validate → END
           │
           └→ approval → tools (官方 ToolNode) → observe → agent
                 └ interrupt → SQLite checkpoint → Command(resume=...) → 原暂停节点
```

StateGraph 管理循环与暂停，官方 ToolNode 管理同轮并行，StructuredTool schema 来自 V2 input_schema，业务委托唯一入口 ToolService.invoke。工具名对模型映射为 v2__search_knowledge 等封闭别名，未知名称不可执行。不是旧 plan/retrieve/grade 改名。

原七个高层工具加 v2.calculate 默认可用；十个底层工具须 tool.advanced 且列入 react_tools。模型可见性、执行前、ToolService 和执行后均检查权限/配置。旧写工具未接入。LLMClient.react 支持 Chat Completions function calls 和 Responses function_call/function_call_output，使用现有 LLMModelPool 故障转移。ReAct 决策/生成使用 Planner 模型链；旧 Answer 链继续服务 legacy，未引入双模型切换。真实验收为 qwen-plus Chat Completions，Responses 仅有契约测试。

## 状态与持久化

AgentState 包含 messages（add_messages reducer）、thread_id/run_id/principal/agent_id/scopes/allowed_tools/budget/question、evidence/scope_checks、approvals/blocked、iterations/tool_call_count/retrieval_rounds/failures/usage/elapsed_seconds/stop_reason/calculations/final_answer。除 messages 外由串行节点替换；ToolNode 线程只返回 ToolMessage，observe 统一合并计数和证据。

Thread 稳定绑定 owner、Agent、KB/文档/版本范围与 conversation_id；每轮新 run_id。旧聊天库不替换，客户端历史不作为可信证据导入；新模式使用服务端有界历史，旧助手内容先复核权限/版本才进入下轮上下文，每轮证据重新注册。

路径为 APP_DATA_DIR/agent-v2，按需创建，目录默认 0700、新数据库与锁 0600：

| 文件/表 | 内容 |
| --- | --- |
| checkpoints.db | 官方 langgraph-checkpoint-sqlite 3.1.3 SqliteSaver checkpoints/writes |
| executions.db / threads | owner、Agent、范围、conversation、执行版本、状态、取消标记 |
| executions.db / calls | thread/run/tool_call_id、规范参数绑定、running/completed 与受限结果回执 |
| executions.db / approval_bindings | thread/run/tool_call_id、上下文摘要、原 ToolApprovalStore ID |
| *.lock | OS flock，跨本机进程排他，同线程并发拒绝，崩溃自动释放 |

不使用 MemorySaver。HTTP 状态/恢复/取消/归档先验证 owner、Agent 和当前知识库权限；不返回原始 checkpoint/messages。归档删除 checkpoint、回执与审批绑定，保留 archived 所有权墓碑。运行版本 react_v2.1 不匹配拒绝恢复。数据库损坏不重发原问题。

这是单机本地磁盘方案，不适用于分布式多副本或无可靠锁的共享盘。跨 Thread 复用 AskConcurrency，SQLite 写事务仍串行。崩溃留下 running 回执视为结果不确定，拒绝盲目重放。只接只读工具和纯计算，不宣称任意外部写操作 exactly-once。

## 审批恢复

approval 节点校验 schema、工具、权限、scope；绑定 owner、Thread/run/tool_call_id、Agent、KB/文档范围、参数、配置和 Repository 版本/映射摘要。多 KB 单个审批拒绝。原 ToolApprovalStore 创建申请，额外持久绑定保证节点重入幂等，interrupt 只传 ID、工具及参数摘要。

原管理员 approve/reject API 不变。恢复接口不接受替换工具参数，重新认证后用 Command(resume={interrupt_id:{approval_id}}) 继续原 checkpoint；不重发问题、不重新规划已完成步骤。重新检查过期/拒绝/篡改/撤权/配置/版本，再由 ToolService 原子 consume 一次。失败成为明确工具拒绝 observation。多工具审批依次暂停，完成审批后 ToolNode 才并行执行；回执挡住重复调用与不确定重试。

LangGraph 暂停节点会重入，因此暂停前工作幂等。审批表和执行表在两个 SQLite 文件：极端崩溃可能留下未绑定 pending 申请，只能过期清理，不能据此执行工具。等待审批不计主动预算；已完成节点时间持久保存，崩溃前未 checkpoint 的节点耗时不能精确恢复。

V2 schema 默认值现在先验证类型，消除 5 与 5.0 导致的同义参数哈希差异。旧版本未消费且采用旧数字表示的 V2 申请可能需重新申请；legacy schema 不变。

## 证据、引用和计算

EvidenceRegistry 仅消费真实 ToolService 输出，并用 EvidenceResolver 从 Repository 重建验证。Registry 保存 KB/document/version/chunk/locator、内容 hash/长度、adapter/provenance/trace_id/scope 摘要；LLM 短片段仍以有界 ToolMessage 保存，checkpoint 并非完全无正文。read_document 返回有界 outline，使高层 Agent 可获取章节/表格 ID；表格/Block 列表支持预算边界 partial 和续读。

每次 LLM 调用前、缓存回执重用、跨轮历史和最终引用都复核当前权限/版本/映射。默认当前版本；显式当前 version_id 可用，历史版本必须来自用户 versions 范围。引用格式 [ev_<stable_hash>]，不存在 ID、撤权/删除/换版、locator 改变拒绝；常见页码、章节/表编号保守检查，不接受模型生成来源链接。服务器按真实版本生成授权内容链接。少量问候允许无引用；知识内容缺引用返回不足。用户纯数字计算由服务器渲染结果，不采用模型随意结论。

**citation_validation=passed 只证明引用技术有效。** answer_correctness 默认 not_assessed。自然语言隐含位置、标准型号/电压等级/统计口径的适用性尚无完整语义裁判；不能把 ID 检查称为事实/标准合规验证。

v2.calculate 是第 18 个共享 V2 工具，同样经过权限/配置/审批/审计。仅支持 add/subtract/multiply/divide/percentage_change/ratio/average/min/max/convert，Decimal 34 位 ROUND_HALF_EVEN；科学计数法/指数有界，零除、NaN/Inf、未知换算、复合单位乘积拒绝，没有 eval/Python 执行。

操作数声明 source=user/evidence、原始 value/unit、可选精确范围。唯一值可省略范围，服务器逐字定位；重复值须明确位置。数字片段、无来源单位、缺失数据拒绝。保留解析范围、原值、单位、operation、precision/result，最终再次复核证据。换算限定长度/质量/时间/压力线性单位，不同单位不得直接混算，空单位不意味着系统知道真实单位。

REST 直接 calculate 没有服务器来源上下文时拒绝，不信任客户端自称的已验证正文。当前来源上下文由 Agent registry/本轮消息提供，后续授权客户端可复用同一 ToolService。

## 预算、事件与兼容

复用 profile.max_tool_calls/max_retrieval_rounds/max_evidence_for_answer。Settings 新增 agent_max_iterations=12、agent_total_seconds=180、agent_tool_seconds=30、agent_context_chars=48000、agent_output_chars=16000、agent_consecutive_failures=3，均有校验。错误尝试占预算，同轮超限直接停止。8 槽有界执行池隔离阻塞调用，ToolNode max_concurrency=4。

超时是软截止：原生索引/I/O 不能强杀，迟到结果不采纳，槽直到线程退出释放，禁止无限创建新线程。取消前后检查持久标记，模型请求期间可能等到超时。上下文按字符预算，非精确 token 计量；工具 schema 仍有 token 开销，用量可得时累计。answer_delta 在最终校验后发送，当前不是逐 token 推送。

| 接口 | 行为 |
| --- | --- |
| POST /api/v2/agent/run 或 /stream | agent_id/question/conversation_id，可选 document_ids/versions；后者 SSE |
| GET /api/v2/agent/threads/{id} | 脱敏状态和 pending_approvals |
| POST .../threads/{id}/resume 或 /resume/stream | 原 checkpoint 恢复 |
| POST .../threads/{id}/cancel | 请求取消 |
| DELETE .../threads/{id} | 排他归档清理 |

事件 run_started/thinking/tool_started/tool_completed/tool_failed/approval_required/resumed/evidence_updated/answer_started/answer_delta/completed/cancelled/failed，终帧 result。含 run/thread、attempt 唯一 event_id、顺序，工具含 tool_call_id/trace_id，不输出隐藏推理、系统提示词、密钥或完整日志。

原 ask 接口可传 runtime_mode=react_v2 或设置 Agent.runtime_mode，响应转成原 AskResponse 并增加 thread_id/runtime_mode。默认 legacy；显式 legacy 回退。独立 react_tools 白名单避免将旧写工具自动授权，旧 allowed_tools 继续限制 legacy。控制库只新增兼容字段，本次未对正式 control.db 启动迁移。

前端实际为 React/Vite（不是 Docusaurus）。只改问答与 Agent 设置页：模式、SSE、取消、既有管理员页面审批后恢复、版本内容链接。Supervisor 保持 legacy，react_v2 Supervisor 配置拒绝，含新模式成员的旧 Supervisor 请求拒绝隐式回退。integration 入口仍 legacy，未做第五阶段迁移。新运行状态走 Thread API，审计 channel=agent-react-v2；旧 AgentRuns 列表尚未合并新 Thread 记录。
