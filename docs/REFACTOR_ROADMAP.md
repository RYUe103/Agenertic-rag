# V2 重构路线与阶段验收

2026-10-31，开发分支 `codex/v2-document-repository`，基线为 `codex/platform-frontend-upgrade` 的 `9691b39`，未改 main。

| 阶段 | 状态 | 内容 |
| --- | --- | --- |
| 1 | 已实现并验证 | 版本化 Document Repository、原文结构、平台 Chunk Builder、读取服务、迁移兼容 |
| 2 | 主体与补强已实现并验证 | Tantivy BM25、Exact 续查、原子发布/ACK、完整回滚校验、租约 GC、版本化尾块修复 |
| 3 | 已实现并完成本地验收；同语料真实向量验收待联调 | Unified Retrieval、确定性规划、RRF、可替换 Reranker、Evidence Resolver、17 个 Tool API V2 |
| 4 | 未开始 | LangGraph ReAct + ToolNode、表格计算、证据验证、checkpoint 工具恢复 |
| 5 | 未开始 | MCP/REST 统一迁移、旧工具退役、全链路评测和性能优化 |

当前架构见 [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md)，第三阶段接口见 [UNIFIED_RETRIEVAL_V2.md](UNIFIED_RETRIEVAL_V2.md)。本次停止在第三阶段，不自动执行第四阶段。

## 第三阶段验收（2026-11-01）

### 第三阶段 Dify 映射补强

- 基于第三阶段 `76a7e3e` / `65bd7f9`，本次补强了上传回执、固定版本/内容hash、索引状态/禁用状态、丢失回执恢复、严格历史来源验证和失败细分类。
- 复查原三问120候选全部来自当前本地Repository之外；本地7份对应远端文档全部disabled。扩大到Top100每问返回63条，仍没有这7份。不能通过放宽Evidence验证或按文件名绑定解决。
- 从历史正式入库manifest找到7份仍enabled的文档；以其中一份保存的MinerU Markdown与远端原上传文件全字节SHA256一致，在隔离Repository恢复映射。真实Vector63候选/2证据，真实Hybrid包含lexical+vector共同贡献的RRF证据；换版后旧证据0条，撤权拒绝。
- 完整回归343 passed、0 failed；新增34项。没有生产数据库迁移和任何Dify上传、启用或删除。
- 同语料100题Vector/Hybrid Recall、答案质量、内网服务验收仍未完成。第四阶段可对接已验证的核心接口，但生产7文档的启用/来源迁移需单独授权处理。
- 详见 [映射闭环验收](DIFY_MAPPING_ACCEPTANCE_2026-11-01.md)。本次仍停在第三阶段，不开始ReAct/ToolNode或checkpoint。

### 第三阶段初次交付记录

- 基于 `a6c2421`，仍在 `codex/v2-document-repository`；没有改 main、迁移生产文档或上传 Dify 数据。
- 统一多 KB 检索、批量权限快照与最终重核、独立 Tantivy/Vector/Exact、加权 RRF、文档聚合、多样性、受限上下文、可插拔重排和原文版本校验已实现。
- V2 工具共 17 个，独立 API 与内部服务复用同一核心，接入已有工具配置、一次性审批与调用审计；底层工具新增 `tool.advanced` 权限。
- 完整测试 309 passed，0 failed；100 PDF/100 问题 Hit@1/5/10 保持 79%/90%/92%，8 道遗留未命中未恢复，无排名质量提升声明。
- 2001 文档/34915 Chunk 同轮 P95：旧路径 775.65 ms，V2 57.47 ms；此为 warm-cache 本机 synthetic 测量。
- 真实 Dify semantic API 已探测，24 份远端文档不能与冻结100份语料对照；三次真实查询共120候选未通过本地版本映射，未作为可信证据返回。真实向量/混合质量验收、实际模型重排和人工证据标注仍待完成。
- 完整边界与报告见 [第三阶段验收](UNIFIED_RETRIEVAL_ACCEPTANCE_2026-11-01.md)。第四阶段只在用户明确授权后开始，接口为 `ToolService.invoke` / `catalog` / `UnifiedRetrievalService.search`。

## 第二阶段补强验收（2026-11-01）

- 开发基线 a803ff3，仍在 `codex/v2-document-repository`；main 未改动。
- 完整回归 `.runtime/bin/python -m pytest -q`：269 passed、0 failed、7 条第三方弃用警告。新增补强测试 40 项，含真实 Tantivy、2001 文档 Exact、撤权/版本失效、发布竞态及跨进程租约清理。
- 原 100 PDF / 100 question_id 固定复测，Chunk 951 → 468；结构 v3 消除重复递减尾块，保留旧结构 v2。
- 原文输入 SHA256 和 Markdown 字节保持不变；Hit@1/5/10 从 79%/89%/90% 变为 79%/90%/92%，MRR@10 从 0.832083 变为 0.837179。仅为来源 PDF 命中，不是答案正确率。
- 2001 份 synthetic 记录覆盖不同 ID、年份、标题、长度和 120 行表格；与真实 PDF 测试分开，未调用 Dify。
- 完整性能、运行边界、7 份实际业务文档副本检查、迁移/运维风险及 Git 状态见 [补强验收报告](SEARCH_HARDENING_ACCEPTANCE_2026-11-01.md)；原 10 道未命中题见 [逐题归因](SEARCH_MISSES_2026-11-01.md)。
- 未进行 Linux AMD64、Docker、离线打包或内网服务验收，未额外收集真实 PDF。正式数据库未迁移。

以上为第二阶段补强当时的验收记录；第三阶段状态以本文顶部为准。

## 第二阶段主体历史验收（2026-10-31）

- 在第一阶段提交 f53e64b 基础上修改，分支仍为 codex/v2-document-repository，main 未改动。
- `.runtime/bin/python -m pytest tests/test_local_search.py -q`：26 项新增检索测试通过；使用真实 Tantivy，非 mock BM25。
- 全套 `.runtime/bin/python -m pytest -q`：229 passed、0 failed，7 条第三方弃用警告；`git diff --check` 通过。
- macOS ARM64 / Python 3.13.5 和 Linux ARM64 / Python 3.11 的 Tantivy 写入、查询、重新打开索引实际通过。AMD64 wheel 已核验，AMD64 容器镜像下载超时，运行验证未完成。
- 100 份真实 PDF、951 Chunk；原 Markdown 504791 字节、有效索引 409996 字节；全量构建 1.054 秒，查询 P50/P95 为 54.514/61.465 ms。
- 100 道 TAT-DQA 来源标注问题，Top-1/5/10 Chunk 结果中的正确 PDF 命中率为 79%/89%/90%；MRR@10=0.832083。不是问答准确率。逐题数据见 LOCAL_SEARCH_BENCHMARK_2026-10-31.json。
- 当前运行数据库未自动迁移或部署；测试数据在 data/local-search-benchmark-phase2-validated 隔离目录，未向 MinerU/Dify 上传本次测试文件。
- 现有业务 sources.db 的隔离副本重复回填成功：7 个文档、309 个结构化 Chunk，第二次 Tantivy 同步为 noop。原业务数据库保持原样。
- 删除回归按新要求更新：Dify 删除失败保留原文件以便重试，同时普通读取必须立即拒绝。没有放宽旧权限。

第二阶段主体交付时尚未实现统一检索；现第三阶段已按顶部记录实施。第四、第五阶段保持未开始。

## 第一阶段历史验证

- `.runtime/bin/python -m pytest -q`：最终 203 passed，0 failed，7 条第三方弃用警告（21.79 秒）。其中新增 Repository 测试 19 项。
- `cd frontend && npm run build`：TypeScript 和 Vite 构建通过；仍有现有 UI 依赖包超过 500 kB 的体积警告，构建产物同步到 static。
- `git diff --check`：通过。
- 本地现有 `data/sources.db` 曾复制到临时目录进行两次实际回填：7 个源文档均成功，重复执行后仍为 7 个版本、1038 个结构节点，原业务数据库未迁移。该记录是本地历史数据验证，不是内网服务验收。

新增 Repository 测试覆盖：新 PDF/DOCX/TXT 的本地解析入库；旧修订和当前原文回填；重复迁移、重启、故障回滚重试；重解析和普通重试版本稳定性；旧版本读取与删除限制；Markdown/HTML 表格、DOCX 合并单元格；页码、章节、Block/Chunk 定位及邻接；MinerU 位置保守处理；跨文档/知识库 ID 拒绝、租户权限撤销；映射状态与 Chunk 归属；旧分隔符不参与 Chunk 构建。全套回归包含旧 Agent、工具、接口、权限和队列测试。

测试期间修复了两个隔离问题：mock 模式仍初始化外部 MinerU，以及模型池会继承本机配置发起真实 LLM 请求。现在 mock 模式保留模型绑定但禁用真实密钥。工具开关回归改为比较禁用前后调用次数，避免假设一次 Agent 请求只调用一次该工具，仍验证禁用后无新增执行。

## 核心实现位置

- `app/document_models.py`、`document_structure.py`：模型、表格规范化、独立 Chunk Builder。
- `app/document_repository.py`、`document_lock.py`：事务存储、回填、映射、读取服务、跨进程互斥。
- `app/document_api.py`、`main.py`：统一读取入口和现有授权规则。
- `app/db.py`、`ingestion.py`：旧修订和 V2 原子发布、重解析/重试/删除衔接。
- `app/mineru.py`、`document_parser.py`：结构产物保留、DOCX 合并表格。
- `app/retrieval.py`：旧全文/章节/表格读取切换到 Repository。
- `app/config_store.py`、`clients/dify.py`、`schemas.py`、知识库前端：旧分隔符迁移和默认值移除。
- `scripts/backfill_documents_v2.py`：备份后回填；`tests/test_document_repository.py`：独立验收测试。

## 后续依赖与保留事项

第二阶段直接调用 `index_manifest`、`version`、`iter_chunks`、`put_mapping` 和 `mappings`。BM25 已明确选定 Tantivy，不能换成 bm25s 或 Dify full_text_search；精确文本、标准编号、正则匹配作为独立能力保留。Dify 只作为向量适配器的最终约束在后续迁移完成，本阶段保留旧接口。

真正的审批 checkpoint 恢复继续列入第四阶段：稳定 thread_id、持久化 checkpointer、interrupt/resume、重复恢复与写入幂等。当前仍是旧的重新执行语义。改写文档等新写入工具、表格计算、混合检索和 MCP 统一迁移均没有提前实施。
