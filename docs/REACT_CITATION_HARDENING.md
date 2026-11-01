# 第四阶段引用生成与验证补强验收

日期：2026-11-01。仓库 `RYUe103/Agenertic-rag`，分支 `codex/v2-document-repository`；开始前 fetch，HEAD 与同名远端均为 `b76e95c`。仅补强第四阶段，没有实施第五阶段，没有向远端知识库上传或迁移生产数据库。

## 1. 根因及复现范围

先运行未修改的 `scripts.smoke_react_v2.py`，随后在不改变提示词、模型或工具逻辑的条件下，用新诊断脚本记录实际模型调用。固定语料来自既有隔离 `submitted.md`，SHA256 为 `1491a965a83f32675d2e50c93b18d7539fa735c84d1f61f492ebedb0db355677`，2983 bytes，3 个 Section、4 个 Table；普通问题来自 `tatqa_test_9063`。模型为现有 qwen-plus、Chat Completions、temperature=0。

本次旧实现重新运行时普通问题通过，不能声称旧版每次失败。带诊断的旧版七题中章节问题因 `unverified_structure_label` 拒绝，其余四个有证据任务通过；无证据和旧版本案例正确拒绝。此前已保存的失败样本仍保留，读取其工具回执和官方 checkpoint 得到：

第一次无追踪基线运行的 simple、section、table、user_calculation、degraded 均通过，审批跨进程恢复也完成（三个阶段）；其原始脱敏报告留在 `data/citation-before-20261101/report.json`。后续七题对照使用带调用计数的统一脚本，不把第一次的成功运行隐去。

| 历史失败样本 | ToolService | 正文候选/注册 | ToolMessage 实际可见 | 最终原因 |
| --- | --- | --- | --- | --- |
| 普通问题 find_document | success，1 个文档候选 | 0 / 0（设计如此） | 0 | 尚未获得正文 |
| 同轮 read_document | success | 1 / 1 | 1 | no_cited_evidence |
| Hybrid 降级 search_knowledge | success/degraded | 3 / 3 | 3 | no_cited_evidence |

上述失败没有因输出预算丢失证据；注册、observe 状态和实际 ToolMessage 均保留了可信证据。真正故障在最后输出没有符合旧正则的 `[ev_...]` 标记。旧版工具消息还重复携带正文、片段、内部定位和 scope 摘要，增加复制长 ID 的负担。旧表格测试曾出现 output_budget，这是另一类失败，不能混为缺引用。

改造后的同一普通问题也真实出现了一次 `no_cited_evidence`（草稿 845 字符，已注册且可见 3 条），随后一次 repair 成功。该结果证明没有仅靠新提示词假定模型永远遵守格式。详细问题、草稿、原文和 checkpoint 留在被忽略的 `data/`；公开 JSON 只有计数、原因、哈希、耗时和非正文定位。

## 2. 引用与内部记录机制

新增 `app/agent_v2/citations.py`，复用 EvidenceRegistry/EvidenceResolver，不建立第二个证据数据库。

- 服务端 ev ID 仍由证据身份和内容哈希生成；保留 KB、document、version、chunk/range、locator、content_hash、scope_digest、provenance、trace。不同文档、版本、范围不因文本相似而合并。
- ToolNode 并行工具仅返回 artifact。observe 按原工具顺序串行合并，分配 S1/S2，已分配编号不重排，重复身份复用。sources 和 visible_evidence 进入现有 SQLite checkpoint；新 Run 清空。
- 原 calls 回执保留 `_evidence`、`_scope_checks` 和恢复信息。模型 content 仅含片段、Source、标题、真实定位、partial/degraded、导航或计算结果。两个 provider 适配层均经过测试确认不传 artifact。
- find_document 不制造正文证据。read_document 的 content 仍根据真实 start/end 从 Repository 生成引用。输出预算仅裁剪展示：未显示的证据不能被直接引用，内部记录不为凑预算而静默丢弃。
- calculate 继续使用专用 evidence_id 参数；Source 仅用于最终答案，不更改操作数验证。

结构化主路径为 `{"claims":[{"text":"结论","source_ids":["S1"]}]}`。结构、字段和数量有界，每个知识结论必须提供来源；兼容 `[S1]` 文本和本轮旧 ev ID。模型直接写 `[1]` 不会被当成已授权编号，S99 也不猜测匹配。

服务器将 Source 映射到可信 ev，重新 Registry.load，保留页码/章节/表号/URL 检查；按正文首次引用顺序生成 `[1]`、claims、citation_metadata 和真实授权版本链接。前端编号和来源卡片同序，重复引用复用同一编号；悬停展示文档/片段/可用页码，点击定位来源卡片，卡片再打开授权版本。模型给出的链接/图片不进入 ReAct 展示，DOMPurify 和 React 转义继续生效，legacy 渲染不变。

`citation_validation=passed` 仅指引用技术有效；`answer_correctness=not_assessed` 不变。结构化 claims 使未来可以增加逐结论支持性检查，但本次没有用短编号冒充语义蕴含验证。

## 3. 一次修复、预算和恢复

validate 发现有可信证据但缺引用、未知编号或格式错误时，先持久化 repair_attempted，再进入独立 repair 节点。修复只看问题、草稿、重新验证的原文片段和 Source，要求删除无依据断言、重写有依据结论；没有自动追加第一条证据。repair 不提供工具、不重放已完成工具，完成后再次严格校验。失败安全拒答。

没有证据不修复；撤权、删除、版本或 scope 改变不修复。页码/结构标签造假与不可见证据也不会借修复绕过。未验证草稿从会话 messages 移除，防止进入下一轮历史；修复所需 draft 只在私有状态暂存，最终清空。

修复复用 calls 回执和本轮固定调用身份：已完成可复用；崩溃遗留 running 的请求拒绝重发。官方 SQLite checkpoint、ToolNode、interrupt/Command(resume)、ToolApprovalStore 保留。跨 Python 进程审批恢复测试确认既有 Source 不变且已完成检索不重跑。旧 react_v2.1 的缺省预算字段、旧 ToolMessage 及停在 validate 前的 checkpoint 有读取兼容路径；旧 ID 不授予新 Run 权限。

兼容是新版读取旧 checkpoint；旧二进制不认识新增 repair 节点，不应拿旧程序恢复停在 repair 的新 checkpoint。需要回退时可切换 legacy，保留这些执行文件，后续由新版恢复。

设置新增：`AGENT_CITATION_REPAIR=true`、`AGENT_TOTAL_TOKENS=120000`、`AGENT_COMPLETION_TOKENS=2000`。修复共用 total_seconds、context_chars、iterations、output_chars 和 token 上限。wire 消息（含工具参数）及工具 schema 的 UTF-8 字节数用于保守预留，provider usage 用于实际累计；不是模型精确 tokenizer，超时后的远端实际消耗可能不可知。最多一次指一次受控修复节点，原模型池自身的故障转移策略未重写。

执行 completed 语义不变。响应增加 answer_validation、answer_displayable、trusted_evidence_count、citation_validation、claims、citation_metadata。拒绝后只返回安全提示；过期证据不计作当前可信证据。

新增指标覆盖候选、注册和拒绝原因、实际可见量、最终引用量、截断、context_chars、prompt/completion tokens、修复尝试/成功。普通事件不含问题、系统提示、完整响应或私有原文。用户最终 answer/evidence 是授权 API 响应，并非普通诊断日志。

## 4. 真实 qwen-plus 前后对照

同一份原文、同一组问题、同一模型参数；每次建立隔离 Repository/索引，不使用已被 stale 测试修改的版本。路径/版本 ID 因隔离创建不同，原始内容哈希相同。这里 Hybrid 是未配置 Vector Adapter 时的真实本地降级，**不是 Dify 云故障或真实混合召回验收**。

| 场景 | 改前 → 改后引用 | 模型调用数 | Prompt Tokens | 总耗时秒 | 改后情况 |
| --- | --- | --- | --- | --- | --- |
| 普通知识问题 | passed → passed | 2 → 3 | 20,084 → 16,774 | 9.00 → 12.91 | 初答缺引用；1 次修复成功，3 注册/3 可见/1 最终引用 |
| 章节问答 | rejected → passed | 5 → 5 | 61,419 → 44,966 | 20.07 → 18.55 | 改前结构标签不符；改后 12 注册/12 可见/3 引用 |
| Hybrid 本地降级 | passed → passed | 2 → 2 | 19,089 → 15,730 | 7.54 → 3.77 | 3 注册/3 可见/3 引用，0 修复 |
| 表格读取 | passed → passed | 4 → 2 | 38,291 → 15,110 | 15.76 → 8.30 | 1 条表格证据，partial；这一题没有执行计算 |
| 定向表格计算 | passed → passed | 3 → 3 | 28,078 → 23,551 | 11.21 → 12.33 | 真实 calculate，1 引用；独立 Decimal 规则校验通过 |
| 无证据拒答 | rejected → rejected | 1 → 2 | 7,279 → 14,844 | 2.88 → 5.97 | 0 注册/0 修复/0 引用，没有凭空绑定来源 |
| 旧版本拒绝 | rejected → rejected | 2 → 2 | 20,115 → 15,983 | 7.77 → 11.11 | 原有 3 条已过期，最终拒绝，0 修复 |

五个有证据任务最终引用通过率为 4/5 → 5/5；两个安全拒绝案例均未误通过。改后缺引用修复 1/1 成功；样本很小，不能宣称普遍成功率或“错误引用率为零”。模型仍可能遗漏引用。无证据问题成本上升，普通问题修复多一次请求且更慢，均计入上述成本。

Prompt 总计 194,355 → 146,958，下降 24.39%；模型调用总计恰好均为 19，但具体路径不同，不能把总量下降全部归因于 ToolMessage 精简，也不能据此宣称问答更准确。定向计算改前选择 subtract，改后选择 percentage_change，独立算术均可核对；财务口径/年份适用性与任务语义正确性仍未评定。

同一改后普通检索回执内部 JSON 为 13,971 字符，模型 observation 为 3,456，减少 75.26%；对应改前 observation 为 13,865。改后 Hybrid 回执为 11,008 → 2,994 字符；表格回执为 6,822 → 980。内部记录仍保留，不是删除可信字段换取小消息。表格 partial 会明确显示，并保留续读导航。

七题的注册/可见/引用数量与 token/耗时、历史失败诊断记录在 `REACT_CITATION_REAL_2026-11-01.json`。其中首次诊断器只统计 ToolMessage 的可见证据，repair 以 HumanMessage 发送原文；JSON 明确补充 checkpoint 核实的 repair_visible=3，不把原计数 0 当作证据丢失。已更新脚本覆盖这条诊断路径。

## 5. 实际验证

```sh
.runtime/bin/python -m scripts.smoke_react_v2 --output data/citation-before-20261101
.runtime/bin/python -m scripts.smoke_citations_v2 --output data/citation-before-traced-20261101
.runtime/bin/python -m scripts.smoke_citations_v2 --output data/citation-after-traced-20261101
.runtime/bin/python -m pytest -q
.runtime/bin/python -m pip check
cd frontend
npm run typecheck
npm run build
```

最终全量 **464 passed，0 failed，7 条既有第三方弃用警告，66.17 秒**，输出保存在 `REACT_CITATION_TESTS_2026-11-01.txt`；相对 426 项基线新增 **38 项**。原 ScriptedModel ID-copy 测试保留，改从可信 artifact 读取 `_evidence`；新增测试故意遗漏、伪造、使用数字编号、隐藏证据，验证修复失败不会任意附加来源。

覆盖：Source 顺序/并行/去重/版本与范围隔离、跨 Run 拒绝、缺引用修复/失败/一次上限/开关/预算、repair 回执重用和不确定请求拒绝重发、审批跨进程恢复、旧 checkpoint、撤权/删除/换版、页码/URL/结构错误、calculate ID 兼容、元数据与授权版本 URL、Chat Completions/Responses artifact 隔离、未验证草稿不进入后续历史。所有前三阶段、第四阶段原测试均保留。

pip check 无冲突；TypeScript 和生产构建通过，仍有既有 Semi 大 chunk 提示。前端实际 DOM 断言 **8/8**（编号、重复引用、悬停文本、点击锚点、注入移除、legacy），命令：

```sh
npm install --prefix data/citation-dom-check --no-audit --no-fund jsdom@26.1.0
node frontend/tests/citation-dom.mjs data/citation-dom-check
```

jsdom 仅安装在忽略的隔离目录，未增加运行时或项目依赖。另提供 `frontend/tests/citation-smoke.html` 供本地 Vite 浏览器检查。浏览器插件初始化失败（`Importing module "node:process" is not allowed in node_repl`），因此未完成真实浏览器视觉验收；DOM 点击断言不冒充浏览器截图/E2E。

开发中曾发现并修复：新预算检查的取消异常处理、3 项新测试断言错误（过宽字符串检查、未解码版本 URL、上下文长度未实际越界），以及修复前草稿残留于下一轮消息历史的问题。最终结果不包含失败项。

## 6. 修改文件、兼容与未完成项

| 文件 | 变化 |
| --- | --- |
| app/agent_v2/citations.py | 短身份、白名单 observation、结构化解析和安全编号渲染 |
| app/agent_v2/runtime.py、state.py | artifact、串行 Source、一次 repair、预算/指标、兼容恢复 |
| app/agent_v2/evidence.py | 候选/注册拒绝原因诊断，原严格复核保留 |
| app/agent_v2/api.py、app/schemas.py、app/config.py | 结构化引用、独立显示状态、配置 |
| frontend/src/pages/QaPage.tsx、citationHtml.ts、types.ts | 安全引用展示、来源跳转和状态 |
| tests/test_react_citations.py | 38 项新增覆盖 |
| tests/test_react_v2.py、test_react_v2_api.py | 旧测试内部观察和新增事件兼容 |
| scripts/smoke_citations_v2.py | 固定七题、真实模型可见量及脱敏对比 |
| frontend/tests/、static/ | DOM 检查页/脚本与实际生产构建产物 |
| docs/REACT_AGENT_V2.md、REACT_AGENT_ACCEPTANCE.md、本报告及 JSON | 设计与验收 |

没有新增数据库表，没有生产迁移，没有改写 Agent 主循环、检索服务、MinerU、Repository、MCP 或旧工具接口；仅在原图增加有界修复分支。旧 Agent/Supervisor/API 保留，completed 不改变含义。

尚未完成：完整语义支持/标准适用性验证、真实大样本引用可靠性统计、真实 Vector/Hybrid Recall 和最终答案准确率、真实 Responses 服务验收、真实浏览器视觉验收、多副本执行或强制取消远端请求。纯文本 fallback 只能记录整段及其来源，不保证逐句蕴含；结构化 claims 也不能自动证明事实正确。

本次满足第四阶段引用机制补强目标，可继续作为后续接入基础，但不等于生产全面验收。**第五阶段未实施，交付后停止。**

## 7. Git 交付

代码及测试提交：`35dd3ac24ce88956b2c9ce4493f51c5859e9d4fe`。已推送到 `origin/codex/v2-document-repository`，通过 `git ls-remote` 核对该远端 SHA 一致；main 未改。

本报告、设计更新、脱敏 JSON 和测试输出以紧随代码提交的文档提交交付。文档提交自身 SHA 由 `git log -1` 查看，避免循环哈希；最终回复给出该提交及推送核对结果。提交前扫描当前配置中的密钥，未发现匹配；`.env`、data/、私有轨迹和临时 DOM 依赖未入 Git。
