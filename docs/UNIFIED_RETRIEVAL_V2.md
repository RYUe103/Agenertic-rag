# Unified Retrieval / Evidence Tools V2

第三阶段实现，2026-11-01。主 Agent 仍使用旧执行图；本文件描述可独立调用的新核心服务。

## 核心调用路径

`RetrievalRequest → AccessPolicy.scope → Planner → bounded coordinator → Tantivy / Dify / Exact → EvidenceResolver → weighted RRF → optional reranker → diversity / bounded expansion → live scope recheck`

- Python 入口：`app.retrieval_v2.service.UnifiedRetrievalService.search`。
- 业务工具入口：`app.retrieval_v2.tools.ToolService.invoke`，REST 与未来内部 Agent/MCP 共用此入口。
- `POST /api/v2/retrieval/search` 调用 `v2.search_knowledge`，经过工具开关、权限、审批配置和调用审计。
- `GET /api/v2/retrieval/tools` 返回输入/输出 JSON Schema、层级、权限、风险、预算。
- `POST /api/v2/retrieval/tools/{name}` 请求为 `{"arguments": {...}, "approval_id": null}`。
- 旧 REST、MCP、Agent 和旧工具名称保留；V2 使用 `v2.` 命名空间，未把旧 Dify 全文工具伪装成本地 Tantivy。

示例检索请求：

```json
{
  "query": "金属燃料辐照肿胀的主要影响因素有哪些？",
  "scopes": [{"knowledge_base_id": "default-kb", "document_ids": null, "versions": {}}],
  "mode": "auto",
  "top_k": 10,
  "filters": {"metadata": {}, "document_type": "pdf"},
  "budget": {"timeout_seconds": 5, "backend_candidates": 40, "total_candidates": 200, "context_chars": 24000},
  "fusion": {"rrf_k": 60, "lexical_weight": 1, "vector_weight": 1, "exact_weight": 1.2, "max_chunks_per_document": 3},
  "expand_neighbors": 1
}
```

版本 ID 必须与同一 scope 的文档归属一致。默认只读当前版本；显式 `versions: {"document-id":"version-id"}` 允许历史证据。现行 Tantivy 只索引当前选中版本，已不在当前代次的历史版本应使用 Exact 或版本化读取，不能伪称支持历史 BM25 索引。

响应有两层状态：ToolOutput 的 `success/error` 是调用状态；`data.status` 的 `ok/degraded/empty/insufficient_evidence` 是检索状态。外部后端失败不等于知识库没有相关内容。`trace_id` 与诊断不包含完整查询/原文日志。

## 三个独立后端

### Tantivy

使用现有 `IndexManager.reader()` 跨进程租约，仍由第二阶段控制代次发布和清理。授权范围在候选 Top-K 前限定；页码、表格与章节（Chunk allowlist）也在 Tantivy 查询中约束。保持原字段权重与分词器，不为测试题调整排名参数。

### Dify

读取知识库详情确认 `indexing_technique=high_quality` 和 vendor provider，再 POST `/datasets/{dataset_id}/retrieve`，强制：

```json
{"query":"...","retrieval_model":{"search_method":"semantic_search","reranking_enable":false,"top_k":40,"score_threshold_enabled":false}}
```

配置的 base URL 已包含 API 前缀，不追加重复 `/v1`。无重试，无重定向，HTTP 超时加绝对截止时间，响应上限 2 MB，查询最多 250 字符。经济模式、外部知识库或能力无法确认时报明确降级。

核对来源为 Dify 官方 OpenAPI `https://docs.dify.ai/en/api-reference/openapi_service.json` 的 RetrievalModel 与 retrieve endpoint。官方模型支持 metadata_filtering_conditions，但无法保证每套部署具备同一字段定义，也没有明确的任意本地 document-ID allowlist 或 retrieve 分页。因此当前适配器不猜测远端过滤配置：远端 Top-K 后必须经过本地授权映射和元数据/位置过滤，诊断标记 `local_verified_mapping_after_remote_top_k`。这可能不足 Top-K，不能宣称向量候选已在远端按本地 ACL 预过滤。

远端匹配先要求 dataset + document + 本地 version 的唯一稳定映射，以及 segment enabled/completed。本地历史 `success` 与完成状态兼容；superseded 不可用。Segment ID 绝不当作 Chunk ID。显式 segment→chunk 映射还须验证内容一致；否则仅允许在指定版本中**完全一致且唯一出现**的原文子串，最大校验 4 MB。没有模糊字符串推断页码。定位成功后恢复 Repository 原文，无法确定的候选不进入最终证据，仅保留安全失败计数及原因。

现网探测是公开 Dify 云 API，不是内网部署验收；没有向真实知识库上传任何文件。

### Exact/Grep

复用第二阶段的受限 regex 引擎、Unicode 偏移、SQLite blob 流读取与带 HMAC/用户/范围/版本绑定的 continuation。原文搜索模式返回按 KB、文档、偏移排序的匹配与独立每-KB continuation；复杂正则必须显式 exact 模式。混合模式将匹配归到 Chunk，一个 Chunk 在同一 Exact 通道只计一次 RRF，多个重复出现不能累加刷分。Exact 匹配列表的文本只在 results 中返回一次，metadata/continuation 保持独立。

## 规划、融合与重排

默认规划不调用 LLM：技术编号/显式文字优先 lexical+exact；所有出现位置优先 exact；普通语义问题 lexical+vector；数值/表格类扩大候选（仍受总预算限制）并建议表格/章节/邻接读取。显式 mode 覆盖规则。文档和章节通过结构化 scope/filter 指定，不猜测“这份 PDF”指哪份文档。

RRF 对每通道去重后计算 `weight / (rrf_k + source_rank)`。不相加 BM25 与向量原始分数。融合按 KB/document/version/chunk 或精确 range 去重，保留每条来源 rank/score/generation。稳定并列使用证据 key。文档候选使用最强 Chunk 分数聚合，同时保留 Chunk、section、table ID；默认每份文档最多 3 个 Chunk。文档检索与详细证据读取可分两次调用，不包含隐式额外 LLM 规划。

`expand_neighbors` 最多 2，各种读取工具最多 5，统一 context_chars 截断并标记。章节和表格通过 `read_document_section` / `read_document_table` 单独有预算读取，支持跨块恢复。不会无上限返回整个章节/表格。

默认 Reranker 为 none。`Reranker.rank(query,texts,timeout)` 是可替换协议；提供真实 Cohere-compatible HTTP 适配器。设置 `V2_RERANK_URL`、`V2_RERANK_API_KEY`、`V2_RERANK_MODEL` 后启用（mock 模式禁用）。候选数、每条文本长度、耗时和响应大小均有上限；无效结果/超时/失败恢复原 RRF 排序。模型得分与 RRF/source 分数分开记录。本次未配置真实重排模型，不声称重排质量提升。

## Evidence 与权限

可信状态：`verified_current`、`verified_historical`；拒绝计数区分 stale、unavailable、unverified_external。最终 results 不携带无法验证的外部正文。

校验版本、构建版本、Chunk ID 归属、locator version、Block 归属、page/section/table 关联；非投影 Chunk 必须逐字等于原文范围。表格检索投影显式 `content_is_projection=true`，原表格/单元格保存在 Repository，可通过工具读取。无可靠页码时返回空数组，bbox 保持原有 null，不推算。

`AccessPolicy` 每次请求建立 KB 当前快照：文档存在、未 tombstone、未待删除、业务元数据、版本、选中 builder、durable revision。可注入实时批量文档 ACL，且在输出前再次调用。API 复用现有租户/组织/会话和 document.read 规则。没有跨请求权限缓存。最终也重核 external mapping digest，防止同一请求中映射被更改。并发删除/换版/撤权不返回旧证据；KB 撤权直接拒绝。

## 工具清单与治理

所有工具版本 2、只读，输入额外字段被拒绝，返回包最大 1 MB。read 默认 5 秒，search 最大 30 秒。审计复用 ToolCallStore，不保存文档内容。ToolConfigStore 开关和 requires_approval 始终生效；人工审批继续使用已有一次性审批机制，不引入 checkpoint。

| 层级 | 工具（均带 `v2.` 前缀） | 权限 |
| --- | --- | --- |
| 高层 | search_knowledge, find_document, read_document, read_document_section, read_document_table, find_exact_text, expand_evidence | tool.invoke + document.read + KB membership |
| 底层 | search_bm25, search_vector, grep_chunks, get_chunk, get_adjacent_chunks, get_document_outline, get_document_page, get_table, get_table_cells, get_evidence_context | 额外 tool.advanced |

管理员和已有全局 API key 保持原超级权限语义。普通用户默认没有 tool.advanced，角色配置可授予。第四阶段应从 `catalog()` 选取高层工具，将调用交给同一个 ToolService；不要在 Agent 内复制权限或证据逻辑。

读取输入共同包括 knowledge_base_id/document_id/version_id/max_chars；Chunk/表格使用 node_id；章节 section_id；页 page；正文 start；Block/outline 使用 after；单元格使用 start/limit 分页，可按 row/column 筛选。大表格整体超过预算返回 output_budget，应改用 get_table_cells 分页。

## 预算边界

最多 8 KB，每路最多 100 候选，总候选最多 500，round-robin 防止单通道占满。全局最多 12 个工作线程且无无限等待队列。后端使用约 70% 请求预算，剩余用于证据和最终授权。超时后不会等待远端结束，也不会无限创建新线程。

Python 不能强杀运行中的同步线程；有界外部请求最终结束前仍占一个槽。SQLite/原生 Tantivy 和最终安全重核也不是实时系统可抢占操作，因此总耗时是软截止，超时明确诊断，不能以截止时间跳过授权。依赖正常部署的一库一存储目录，沿用现有 Tantivy 生命周期，未迁移生产索引。

## 运维和兼容

没有修改 Document Repository 表或破坏性迁移。启动时现有 ToolConfigStore 幂等加入新工具配置；增加 tool.advanced 权限枚举，不自动授予普通用户。旧 Agent allowed_tools 未自动扩展，不修改其执行图。环境里尚未回填的旧数据库应按阶段一流程先备份、回填并同步索引。

可复现验收命令见阶段三报告。真实 Dify、内网和完整100题同语料向量效果必须分开验收；无 gold evidence span 时引用可验证率不能当作证据 Recall。
