# 第三阶段验收报告 — 2026-11-01

实现基线 `a6c2421a1a70df6cb34d53b1d5931a5c39aaf228`；代码提交 `76a7e3e`；分支 `codex/v2-document-repository`。本次仅第三阶段，没有修改 main。代码、本报告及机器可读评测记录一并提交推送；最终文档提交 SHA 见 `git log -1`（报告不写自身循环哈希）。

## 验收结论

统一检索、证据验证和工具服务已实际实现，完整回归 **309 passed、0 failed、7 条第三方弃用警告，42.11 秒**。新增第三阶段测试40项。旧269项仍通过。真实本地 BM25 未退化，批量权限检查显著降低规模查询开销。

真实 Dify semantic API 可用，但当前数据无法完成冻结100题同语料向量/混合质量验收。三次真实查询各返回40候选，均因缺少唯一有效的本地版本映射而拒绝作为证据。没有用 Fake Vector 冒充真实召回成绩。真实模型重排未配置，没有相关质量提升声明。

## 实际架构与文件

| 文件 | 实际职责 |
| --- | --- |
| app/retrieval_v2/models.py | 严格输入、预算、过滤、scope、Evidence、Response 模型 |
| access.py | 每KB单连接快照，批量文档ACL，可撤销授权，版本/映射最终重核 |
| planner.py | 无LLM的确定性查询分类、通道选择、候选放大、显式模式覆盖 |
| coordinator.py | 12槽共享线程调度、截止时间、容量超限与故障状态 |
| adapters.py | 独立 Tantivy、Exact 与 Dify semantic-only 请求 |
| evidence.py | Repository 原文、版本、Chunk/Block/位置归属校验，外部映射与唯一子串校验 |
| fusion.py | 加权RRF、每通道去重、稳定排序、文档聚合、文档多样性 |
| reranker.py | none默认，可替换协议与真实HTTP适配器，预算与失败回退 |
| service.py | 多KB统一编排、候选总预算、上下文扩展、诊断、最终权限复核 |
| tools.py / api.py | 17个真实只读工具、JSON Schema、权限治理/审计、最小REST入口 |
| app/tool_registry.py | V2工具加入既有注册与配置，正确输入/输出schema |
| app/access.py / config.py / main.py | tool.advanced、可选重排配置、路由及现有权限接线 |
| app/search/tantivy_adapter.py | 支持章节对应的Chunk allowlist在Top-K之前过滤 |
| tests/test_retrieval_v2.py | 40项新测试，真实本地索引与明确标记的外部contract fake |
| scripts/benchmark_unified_retrieval.py | 固定真实100题、synthetic2001规模、证据完整性与遗留题复测 |
| scripts/smoke_vector_v2.py | 只读真实Dify探测，本地库隔离副本，无远端上传 |

没有新增平行原文库，没有修改既有 Document/Version/Block/Chunk/Table 数据表定义。现有 `tool_configs` 在启动时幂等增加17个工具；调用审计继续写 `tool_calls`，审批消费复用 `tool_approvals`。新增 `tool.advanced` 权限枚举不自动授予普通用户。

详细请求示例、工具清单、分页参数与后续接入见 [UNIFIED_RETRIEVAL_V2.md](UNIFIED_RETRIEVAL_V2.md)。

## 实际机制与边界

- Planner：普通语义 lexical+vector，技术编号 lexical+exact，原文所有位置 exact，数字表格扩大候选；手动模式优先。文档/章节由显式scope/filter约束，不猜指代。
- RRF：`weight/(k+rank)`，不用原始分数相加；同一KB/document/version/chunk仅保留一个证据，各来源rank/score保留。精确出现不重复计分；纯Exact保留原文顺序与签名continuation。
- 多级读取：find_document聚合最强Chunk、章节/表格ID；结果默认每文档最多3个Chunk；相邻扩展最多2、读取工具最多5。整章/整表用独立受限读取，超预算拒绝并可改用分页单元格。
- Reranker：none；可通过V2_RERANK_URL/API_KEY/MODEL设置Cohere兼容服务。测试只证明协议、排序与回退，未运行真实模型。
- Evidence：明确当前/历史状态；旧索引与builder不匹配拒绝，明确历史版本可读。检查Block、章节、页码、表格归属；非投影Chunk逐字对原文。表格投影有标志，原始表格/单元格从Repository读取。没有可信几何时不补造。
- Dify：先GET知识库确认high_quality/vendor，再POST retrieve 强制semantic_search、关闭rerank；官方OpenAPI确认query<=250、Top-K、metadata条件、无retrieve分页。为避免猜远端metadata，当前本地映射后过滤，可能不足Top-K。无重试、有限超时/响应大小，能力不明确、安全映射失败均报告降级。
- 权限：候选生成前建立实时scope，最终再次检查KB/组织成员关系/角色、文档版本、构建、revision、映射摘要。支持显式批量ACL callback；不跨请求缓存授权。向量仅把授权且能映射的候选送入证据。
- 工具：高层7个，低层10个；低层额外要求tool.advanced。配置禁用/审批开关有效，调用结果和错误有trace。旧Agent工具目录没有自动改为V2，旧UI审批入口未全面迁移；内部ToolService可使用已有ApprovalStore签发/消费的审批。

## 实际测试

```sh
.runtime/bin/python -m pytest tests/test_retrieval_v2.py -q
# 40 passed
.runtime/bin/python -m pytest -q
# 309 passed, 7 warnings in 42.11s
git diff --check
```

完整输出：[UNIFIED_RETRIEVAL_TESTS_2026-11-01.txt](UNIFIED_RETRIEVAL_TESTS_2026-11-01.txt)。

覆盖五种检索模式、多KB/元数据/文档/版本过滤、未知字段及预算越界、空索引/空结果、单与多后端超时、RRF数学/权重/去重/稳定性/文档多样性、重排失败回退、Dify纯语义请求/能力拒绝/映射内容拒绝、跨KB/租户/权限撤销、并发换版与映射变化、已删历史文档、旧索引、伪造locator拒绝、HTML合并单元格分页、章节/表格/跨Chunk读取、NUL原文偏移与续查绑定、配置开关/一次性审批/审计、最小REST及旧接口回归。

外部故障测试中的FakeVector明确仅用于契约、融合、异常和权限测试。测试中的模型排序实现仅是协议测试，不是实际模型效果。

## 固定真实100题对照

```sh
.runtime/bin/python -m scripts.benchmark_unified_retrieval --kind real --output data/unified-real-accepted-20261101
```

复跑时输出目录必须不存在sources.db。重新按固定SHA256寻找原100PDF，校验原Markdown字节数，保持原question_id/问题/来源/数据划分。基线清单SHA256 `34459dfc5e1cc16f020b763eb54018bf82dc9f0bb033bca85a6cd2c49cef31a3`。没有使用第二阶段操作测试后被删文档/改标题的库冒充原语料。本轮468 Chunk。

| 同轮路径 | 来源PDF Hit@1 | Hit@5 | Hit@10 | MRR@10 | P50 ms | P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 第二阶段 LocalSearchService | 79% | 90% | 92% | 0.837179 | 39.04 | 43.98 |
| V2 lexical，文档cap=100 | 79% | 90% | 92% | 0.837179 | 9.63 | 11.67 |
| V2 lexical，默认文档cap=3 | 79% | 90% | 92% | 0.837179 | 9.37 | 11.45 |

这里 Hit@K 是正确来源PDF在前K个Chunk里出现，不是答案准确率，也不是证据Recall。未调优冻结测试题，没有声称独立泛化提升。

V2默认路径组件 P50/P95 ms：权限准备1.06/1.25，Evidence4.46/5.88，Fusion1.06/1.22，内部总计9.23/11.26。外层测量还包含模型构造/序列化；各项percentile不能直接相加。

结果与逐题记录：[UNIFIED_RETRIEVAL_REAL_2026-11-01.json](UNIFIED_RETRIEVAL_REAL_2026-11-01.json)。

## Synthetic 规模

```sh
.runtime/bin/python -m scripts.benchmark_unified_retrieval --kind synthetic --output data/unified-synthetic-accepted-20261101
```

2001份合成文档、34915Chunk，100次查询；不是2001份真实PDF。

| 路径 | P50 ms | P95 ms |
| --- | ---: | ---: |
| 同轮第二阶段路径 | 640.92 | 775.65 |
| V2 lexical cap=100 | 46.67 | 84.42 |
| V2 lexical cap=3 | 43.38 | 57.47 |

V2默认组件P50/P95：权限准备16.01/19.29，Evidence2.42/10.14，Fusion0.52/0.57，内部总计42.84/56.60 ms。历史第二阶段报告P95约1008ms，硬件负载及运行时刻不同，应优先比较本轮775.65ms对57.47ms。权限callback在benchmark为允许全部，不包含真实LDAP网络耗时；真实API仍重核既有权限，未放宽。查询warm-cache，测试运行期间存在普通开发负载；不是硬实时SLA或并发压测。

报告：[UNIFIED_RETRIEVAL_SYNTHETIC_2026-11-01.json](UNIFIED_RETRIEVAL_SYNTHETIC_2026-11-01.json)。

## 真实Vector与不可开展的评测

```sh
.runtime/bin/python -m scripts.smoke_vector_v2 --output data/unified-vector-final-20261101
```

使用现有.env凭据，日志/报告不写密钥。真实公开API确认high_quality语义接口；远端目录24文档，has_more=false。本地原7文档库克隆回填后探测，不修改生产库。只读核对中7份本地文档可对应远端目录，但其中仅3份hash属于固定100PDF，实际retrieve的候选没有唯一有效的本地版本映射。

| 实际问题 | 原始候选 | 已验证证据 | 后端 ms | 结果 |
| --- | ---: | ---: | ---: | --- |
| 预算 | 40 | 0 | 4195.45 | insufficient_evidence |
| Token | 40 | 0 | 2475.45 | insufficient_evidence |
| API | 40 | 0 | 3642.14 | insufficient_evidence |

拒绝原因均记录 `external_ambiguous_or_missing_version_mapping`。这是版本来源核验失败，不是API失败，也不证明语料不存在答案。无批量上传、无伪造映射、无按文件名强匹配。详细机器记录：[UNIFIED_RETRIEVAL_VECTOR_SMOKE_2026-11-01.json](UNIFIED_RETRIEVAL_VECTOR_SMOKE_2026-11-01.json)。

同一100题的Vector Only、BM25+Vector、RRF混合质量指标均为**未开展**；当前语料/映射条件不满足可比性。Exact加入和RRF数学行为通过真实本地/契约测试，但没有把它包装成真实100题混合提升。真实模型Reranker亦未配置。内网MinerU/Dify验收未执行。

## 证据级评价及遗留8题

真实100题共返回1000条证据：引用可验证率100%、当前版本一致率100%、重复证据率0%、非空查询覆盖100%。synthetic本轮也返回1000条，同项完整性指标一致。这些指标衡量来源可核查，不证明内容与问题相关或答案正确。

无人工gold证据范围，`evidence_recall` 与 `correct_table_recall` 明确为null。已生成8道题的gold_context、目标文档限定检索结果及原文locator候选，状态一律 `pending_human_review_not_gold`：[UNIFIED_RETRIEVAL_EVIDENCE_REVIEW_2026-11-01.json](UNIFIED_RETRIEVAL_EVIDENCE_REVIEW_2026-11-01.json)。人工审核通过前不能作为gold调参或评测。

| 遗留题 | 第二阶段Top10 | V2 lexical | V2文档多样性 | 结论 |
| --- | --- | --- | --- | --- |
| tatqa_test_9083 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9084 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9085 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9097 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9098 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9119 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9144 | 未命中 | 未命中 | 未命中 | 未恢复 |
| tatqa_test_9157 | 未命中 | 未命中 | 未命中 | 未恢复 |

没有新引入来源排名退化。文档多样性在此集合未改善来源Top10。EvidenceResolver能验证目标文档限定候选，但不能凭其证明找到正确计算操作数。已有平铺PDF文本、缺失公司身份与结构表格、9098跨块证据、9157原表标签差异仍存在；详见第二阶段逐题归因，未假设Vector必能解决。对这8题的Vector新增候选/RRF变化因同语料向量缺失无法评估。

## 已知风险与后续接口

1. 真正向量端到端证据尚未通过，需要确认平台入库产生的dataset/document/version映射以及远端候选来源；不能靠放宽相似匹配掩盖问题。
2. 远端Top-K后本地过滤可能不足候选；未来可在明确远端metadata配置后增加服务器过滤或专用索引映射迁移。
3. 当前唯一子串校验较保守，改写、重复段落、超过4MB版本可能拒绝；当前schema无稳定segment映射的历史文档不会自动获可信页码。
4. SQLite与原生Tantivy调用不可强杀，整体为软截止；有限12槽防止超时请求堆积。最终权限安全校验不可因超时略过。
5. 历史版本读取/Exact可用；旧版本不在现行Tantivy代次时不声称支持历史BM25。
6. 新API已可用，但现有Agent/前端/MCP及审批UI未全面迁移。需要审批的V2工具可经内部既有ApprovalStore使用，不能声称完成新的UI审批流程。
7. 结构读取/现有Repository本身仍受早期解析质量限制，不补造扫描件表格、bbox或单元格；大表格需要分页。

第四阶段对接 `ToolService.invoke`、`catalog()`、`UnifiedRetrievalService.search`、`RetrievalResponse`/`Evidence`。本次没有ReAct+ToolNode、checkpoint/interrupt、工具恢复、计算Agent或第五阶段MCP全面迁移。完成本阶段后停止。
