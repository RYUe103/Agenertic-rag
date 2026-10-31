# 第四阶段 ReAct Agent 验收

日期：2026-11-01。仓库 RYUe103/Agenertic-rag，分支 `codex/v2-document-repository`，基线 `9f0c479`。代码提交 **`3153d068f1bda47bc48dd4974c609cb645c80d7d`**，已成功推送同名远端分支。本文和完整测试输出作为后续报告提交推送；报告提交自身 SHA 由 git log 查看，避免循环哈希。main 未改。

## 结论与范围

第四阶段的 opt-in 核心架构已经实现：真正的 LangGraph StateGraph + 官方 ToolNode、共享 V2 ToolService、官方 SQLite checkpoint、interrupt/Command(resume)、实时授权与审批绑定、调用回执、证据复核、Decimal 计算、预算、SSE 和必要前端兼容。

**完整回归 426 passed、0 failed、7 条第三方弃用警告，55.08 秒。** 相对 343 项基线新增 83 项。前端 TypeScript 检查和生产构建通过，pip check 无依赖冲突，git diff --check 通过。构建仍有现有大 chunk 提示；未进行浏览器视觉或 Linux/Docker 验收。

真实 qwen-plus 完成章节读取、结构化表格→计算、审批暂停→独立 Python 进程恢复，并有机械引用验证记录。**不是全场景答案质量验收通过。** 普通检索仍有查空/缺引用，某些样本被安全拒答；定向成功样本明确给定导航或工具要求，不能据此宣称自主规划质量或 Recall 提升。

没有生产数据库迁移/回填、批量解析/上传、Dify 远端写入、旧工具删除或第五阶段 MCP/REST 全面迁移。真实语料使用之前保存的 MinerU Markdown 隔离副本，没有将 Fake Model 测试当成模型效果。

## 架构与 State

实际节点关系：START → agent；有 tool_calls → approval → tools(ToolNode) → observe → agent；无调用或预算终止 → validate → END。模型可在任意轮选择搜索/精确匹配/读文档/章节/表格/扩展/计算；同轮多次只读调用由官方 ToolNode 并行调度。

State：messages、thread/run/principal/agent/scopes/allowed_tools/budget/question、evidence/scope_checks、approvals/blocked、iterations/tool_call_count/retrieval_rounds/failures、usage/elapsed_seconds/stop_reason/calculations/final_answer。messages 使用 add_messages，其他字段由串行节点聚合替换。证据 registry 保存稳定 ID、版本、locator、内容 hash、来源/trace，不永久复制每次全文；必要短片段仍存在有界 ToolMessage 内。

工具 schema 复用 catalog/input_schema；模型别名 v2__name 严格映射 v2.name。原 17 个工具保留治理，新增第 18 个共享工具 v2.calculate。默认七个高层知识工具加计算，低层仍须 tool.advanced 和 Agent react_tools。没有将旧写工具批量开放。

## 持久化、审批与幂等

正式使用 langgraph-checkpoint-sqlite 3.1.3 的 SqliteSaver，路径 APP_DATA_DIR/agent-v2/checkpoints.db。executions.db 保存 threads/calls/approval_bindings；OS flock 阻止本机多进程同 Thread 并发，重启自动释放锁。目录默认 0700，数据库/锁 0600。

审批继续使用现有 ToolApprovalStore：绑定 actor、Agent、Thread/run/tool_call_id、KB/文档范围、工具、规范参数、配置、版本/映射摘要。interrupt 真实暂停图；恢复重新认证、读取原 checkpoint，以 Command(resume) 继续。参数不可由恢复请求替换，过期/拒绝/篡改/撤权/换版/配置变更拒绝。已完成回执不重跑，已消费批准不复用；崩溃时 running 回执保守标为不确定，拒绝盲目重试。执行预算配置改变亦拒绝复用旧执行。

跨 Python 进程测试确认：第一次已完成检索，第二次读取等待审批；新解释器加载 checkpoint 后只执行未完成读取和后续模型决策，检索审计数量仍为 1。真实模型测试也由脚本启动两个独立恢复子进程，未重发原始问题。

SQLite 是单机方案，非分布式事务。审批表与执行绑定表之间极端崩溃可能留下不能消费的孤立 pending 申请，依原审批清理接口回收。当前只支持只读/纯计算，未声称任意远端副作用 exactly-once。细节见 [REACT_AGENT_V2.md](REACT_AGENT_V2.md)。

## 证据和计算

EvidenceRegistry 用 EvidenceResolver 从 Repository 重新构造正文/位置；LLM 文本不注册为证据。默认 verified_current，只有用户显式历史范围才允许 verified_historical。明确的当前 version_id 可直接使用，不误判为历史。

最终引用校验 ID、本轮归属、版本/权限/文档存活、locator 与内容 hash；缓存恢复和下轮消息也重核范围。伪造 ID/页码、跨文档或旧版本拒绝。服务器生成授权的版本内容链接。引用机械有效不等于所有自然语言结论被支持，answer_correctness 默认 not_assessed，完整标准型号/工况/统计口径适用性验证未实现。

计算工具仅允许专用 Decimal 运算，34 位 ROUND_HALF_EVEN，支持四则/百分比/比值/平均/min/max/科学计数法和明确线性单位转换。输入须来自用户消息或已验证证据的原文范围，唯一值可自动精确定位；重复值须明确偏移，缺失数据/未知单位/不兼容维度/零除/任意代码拒绝。结果保留原值、来源与解析出的偏移，最后再复核。用户纯数字计算直接渲染服务端结果；证据计算仍必须引用证据，不能绕过答案验证。

## 真实模型验收

环境：macOS ARM64，Python 3.13.5，LangGraph 1.2.18、prebuilt 1.1.1、langchain-core 1.7.3、checkpoint 4.3.1、SQLite saver 3.1.3、Tantivy 0.27.0。

模型：现有配置 qwen-plus，Chat Completions 原生工具调用。隔离文档是既有提交产物 2983 字节，SHA256 `1491a965a83f32675d2e50c93b18d7539fa735c84d1f61f492ebedb0db355677`，Repository 恢复 3 个章节、4 个表格。不是此次重新调用 MinerU/PDF 解析质量验收。

本轮隔离服务未配置 Vector Adapter，用真实本地 Tantivy/Exact 验证 vector_unconfigured 的降级路径，**不是实际 Dify 云故障实测，也没有重复宣称第三阶段真实 Hybrid 验收**。生产旧库映射限制仍按第三阶段报告处理。

| 场景 | 工具次数 / 检索次数 / LLM 次数 | 实际顺序 | 主动耗时秒 | tokens | 引用结果 |
| --- | --- | --- | ---: | ---: | --- |
| 普通检索 | 2 / 1 / 3 | find_document → read_document | 11.19 | 27994 | 拒绝：最终缺引用 |
| 年份/章节一般请求 | 2 / 1 / 3 | find_document → read_document | 10.99 | 28206 | 通过，但没有真正调用章节工具 |
| 一般结构表请求 | 1 / 0 / 2 | read_document_table | 2.10 | 14828 | 当时触发 output_budget，安全拒答 |
| 用户给定数据增长率 | 1 / 0 / 2 | calculate | 2.64 | 14984 | 通过，服务端数值规则通过 |
| Hybrid 降级 | 1 / 1 / 2 | search_knowledge | 8.66 | 19127 | 返回本地 degraded 证据，但模型最终缺引用，拒答 |
| 审批及两个进程恢复 | 2 / 1 / 3（累计） | 暂停 → find_document → 暂停 → read_document | 11.70（累计） | 28035（累计） | 最终通过，已完成工具未重跑 |
| 显式章节 ID 定向验收 | 1 / 0 / 2 | read_document_section | 3.40 | 15671 | 通过 |
| 显式结构表计算定向验收 | 2 / 0 / 3 | read_document_table → calculate | 7.11 | 28177 | 通过，原文操作数及算术规则通过 |

表格预算问题后增加了 partial/续读与 bounded outline；后两项是在此修复后运行，显式给出导航/工具要求，不能混入无提示自主性对照。一般样本失败不删除，报告保留早期 A/B 两轮结果与错误。最后若干取消/配置/引用元数据保护修改由本地回归覆盖，未对每个微调重复全部云测试。

来源为隔离 Repository 的当前版本；检索路径有真实 lexical/exact，读取路径为 repository。引用定位没有猜测页码。表格计算的两个操作数来源均为已验证 evidence，独立 Decimal 重算与工具结果一致；这只证明提取范围和算术，不证明模型选对会计口径。用户数值题增长率 5.414012738853503…%，独立规则核对通过。

所有 token/迭代数在一个 run 内累计，恢复阶段不能再叠加累计值；主动耗时不含人工等待和新解释器启动。除用户计算与表格算术规则外，没有人工 gold 答案审核。报告不含问题全文、答案全文、文档正文、API Key 或令牌；完整私人运行输出留在 git ignored 的 data 目录。

机器记录：[REACT_AGENT_REAL_2026-11-01.json](REACT_AGENT_REAL_2026-11-01.json)。

## 测试与复现

```sh
.runtime/bin/python -m pytest -q
# 426 passed, 7 warnings in 55.08s
.runtime/bin/python -m pip check
# No broken requirements found.
cd frontend
npm run typecheck
npm run build
# tsc + vite 成功；现有大 chunk 提示
```

早期专项运行 81 passed，随后增加预算变更和取消最终答案测试，最终完整回归包含全部 83 项新增。覆盖：无工具问候、真实 Tantivy 搜索、多轮搜索后章节/表格/Exact/扩展、并行读取、空查/错误参数重试、超时/取消/循环预算、低层权限、Thread/Agent/用户/KB 隔离、多轮证据更新、跨进程恢复、文件锁、损坏 checkpoint、重复/不确定回执、多审批恢复、过期/拒绝/篡改/配置/版本变更、撤权/删除/伪引用/伪页码/历史范围、表格来源计算、单位/精度/代码注入拒绝、API/SSE/配置回滚/传输契约。旧 343 项回归继续保留。

LLM 控制流单测采用明确 ScriptedModel，工具链实际使用官方 LangGraph/SQLite、Tantivy 和 Repository；它们不是问答质量测评。期间确实出现过审批数字规范化问题、当前版本误拒绝、计算偏移误差、表格输出预算和一个测试期望偏移写错，已修复并重跑。最终无失败。

完整日志：[REACT_AGENT_TESTS_2026-11-01.txt](REACT_AGENT_TESTS_2026-11-01.txt)，工作区路径已脱敏。

真实复现命令（首轮必须用新隔离目录，配置来自现有环境，不打印密钥）：

```sh
.runtime/bin/python -m scripts.smoke_react_v2 --output data/react-v2-real-20261101-final
.runtime/bin/python -m scripts.smoke_react_v2 --output data/react-v2-real-20261101-final --targeted
```

## 改动文件、兼容和回滚

| 文件 | 修改 |
| --- | --- |
| app/agent_v2/state.py、runtime.py | StateGraph/ToolNode、审批节点、预算/事件、真实恢复、模型工具桥接 |
| app/agent_v2/checkpoint.py | 官方 SQLite saver、线程归属、flock、回执、审批绑定和归档 |
| app/agent_v2/evidence.py | 运行级引用、原文/版本/权限复核、最终引用验证 |
| app/agent_v2/api.py | 认证运行/状态/恢复/取消/SSE 与 AskResponse 兼容 |
| app/retrieval_v2/calculations.py | 专用 Decimal 计算与来源定位 |
| app/retrieval_v2/tools.py、models.py | 共用 preflight/计算、分页与目录句柄、默认值类型规范化 |
| app/clients/llm.py、llm_pool.py | 原生 tool calls 与现有模型池衔接 |
| app/config.py、config_store.py、schemas.py、agent_registry.py、main.py | 预算、模式/白名单存储、运行接入、Supervisor 安全边界 |
| frontend/src/api/client.ts、pages/QaPage.tsx、pages/AgentsPage.tsx | SSE、模式选择、取消/审批继续、Thread ID 恢复便利功能 |
| static/ | 对应实际构建产物，hash 文件名变化 |
| tests/test_react_v2.py、test_react_v2_api.py、test_calculations_v2.py、test_react_llm_transport.py | 83 项新增测试 |
| tests/test_retrieval_v2.py | 工具数量 17→18，原覆盖保留 |
| scripts/smoke_react_v2.py、pyproject.toml | 隔离真实验收、官方 SQLite saver 与 API 最低依赖版本 |
| docs/ARCHITECTURE_V2.md、REFACTOR_ROADMAP.md、REACT_AGENT_V2.md、本文和 JSON/日志 | 设计、边界、实测和交付 |

控制库新增 agents.runtime_mode（默认 legacy）、react_tools（空旧值使用明确高层默认）；原字段/会话不删除。本次代码只在测试/隔离目录创建或迁移数据库，没有启动正式库迁移。部署新版首次启动时会执行幂等兼容列新增。

切换：问答页 ReAct 开关，或 Agent 设置页 runtime_mode/react_tools，或原 ask payload 指定模式。回滚：模式设 legacy，保留 checkpoint 文件以便后续恢复；不要清空控制库。旧 Supervisor 不被重写；新模式 Supervisor 配置拒绝，新模式成员也不经旧 Supervisor 隐式执行。外部 integration 和旧 retrieve 维持 legacy，未提前做第五阶段接口迁移。

## 尚未完成与接入边界

1. 普通真实模型问答的可靠自主检索、完整内容支持/事实验证、标准型号/工况/统计口径适用性仍需后续评测和补强；此次失败样本已明确保留。
2. Supervisor 的 ReAct 成员编排未实现；独立 ReAct 可用，旧 Supervisor 保留。新 Thread 记录尚未合并进旧 AgentRuns 列表，可通过专用 API 查询，工具审计已区分 agent-react-v2。
3. Dify 云真实新 Agent 全库问答、真实 Vector Recall/Hybrid Recall、Reranker、内网 MinerU/Dify 和生产迁移未验收。旧生产向量来源问题没有因此自动解决。
4. SQLite/OS 锁仅单机；软超时不能强杀原生调用；取消可能等到单次请求返回。context 字符预算不含服务端 schema token 的全部开销，真实 token 成本仍较高。
5. 前端 SSE 为状态与校验后最终答案块，不是模型逐 token 输出；只做构建/API 测试，没有浏览器视觉验收。
6. 不接入有副作用旧工具，未验证远端写操作 exactly-once、分布式恢复或复杂 Sandbox。审批双 SQLite 文件极端失败留下的无效申请由过期清理处理。
7. Responses 协议有契约测试，没有真实服务实测；ReAct 暂用同一个 Planner 模型链决策和作答。

第四阶段核心已具备后续工具网关接入条件，**不代表全生产上线或语义质量全面验收**。代码和报告推送后停止，等待明确要求第五阶段。
