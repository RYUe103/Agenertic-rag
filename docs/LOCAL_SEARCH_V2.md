# 第二阶段：本地检索、升级与运维

实施日期：2026-10-31。仅包含结构化 Chunk、Tantivy BM25、Exact/Grep、索引发布与删除恢复。没有向量融合、RRF、ReAct 或 MCP 契约迁移。

## 组件及实际依赖

| 文件 | 职责 |
| --- | --- |
| app/chunk_builder.py | 结构感知分块、句子边界重叠、表格行分组 |
| app/document_chunks.py | 现有 Repository 的构建版本、SQL 结构定位与分页扩展，同一个 SQLite |
| app/search/tokenization.py | 中英文及技术标识共享预处理 |
| app/search/tantivy_adapter.py | 真正 Tantivy 索引、字段 BM25、授权候选过滤 |
| app/search/index_manager.py | 代次、校验、增量、切换、回滚、失败恢复 |
| app/search/exact_search.py | 受限原文字符串/大小写/正则扫描 |
| app/search/service.py | 候选生成前授权和返回前复核 |
| app/search/api.py、worker.py | 管理/检索 REST 和持久化变更轮询 |

依赖固定为 `tantivy==0.27.0`、`jieba==0.42.1`、`regex==2024.11.6`，已加入 pyproject.toml 与 Linux AMD64 依赖清单。没有 bm25s。Dify 和原 Agent 检索行为未在本阶段替换。

实际验证环境：macOS 15.6 ARM64、Python 3.13.5；Tantivy 自报 `v0.27.0, index_format v7`。Linux ARM64 的 `python:3.11-slim` 容器实际完成安装、写入、查询和重新打开索引。PyPI 元数据给出 Python >=3.11；macOS universal2 与 Linux ARM64/AMD64 ABI3 wheel 均已核对官方 SHA256。AMD64 容器镜像拉取超时，未完成 AMD64 执行验收，也未重打包完整离线发布包。

## Chunk 构建规则与迁移

默认配置为 size=1800 字符软目标、overlap=180 字符，版本 `structure-v2-feddd4887b5a`。版本由算法名称和配置哈希组成。参数支持 size 200..16000、overlap 0..size/3。新算法必须增加算法版本，不得用同一版本更换规则。

- 原文依然由不可变 DocumentVersion 保存；检索构建保存在 chunk_builds / retrieval_chunks，chunk_selection 选择当前构建。改变构建配置不增加原文版本号。
- 尽量按段落、章节组织，标题关联正文；长段落优先按句末和换行拆分，重叠只回退完整语句。单个不可分句或一行表格允许超过目标大小，不强制破坏标识或数字。
- 页码、Section、Block、Table ID 保留在 Locator；标题上下文同时用于字段检索。字符偏移仍指向原始 Markdown，不指向规范化后的字符串。
- 普通表格为独立 Chunk，保留 Table 原文和 Cell。长表按行分组，将行列、跨度、原始字符串保存在结构化 JSON 检索投影中，重复表头、单位、章节/可识别表名和跨组 rowspan 单元格；Locator 指向完整原表，cell_ids / table_rows 定位具体单元格。
- 新发布文档直接生成结构化 Chunk。第一阶段已发布的 `chars-1800-v1` Chunk 不覆盖、不删除，仍可按旧 ID 获取。旧版本第一次执行索引同步会幂等构建结构化检索 Chunk。
- `DocumentVersion.builder_version` 仍表示原文结构规范化版本，Chunk.builder_version 表示检索分块版本，二者不混淆。

内部调用：`repo.documents.rebuild_chunks(document_id, version_id, ChunkConfig(...), activate=True)`；`select_chunk_build` 切换已存在的构建。`IndexManager.sync()` 完整发布新索引后，查询才看到新的构建。不会因为切换 Chunk 配置立刻破坏正在服务的旧索引。

## 分词、字段和 BM25

分词版本 `jieba-0.42.1-hmm0-nfkc-tech-v1`：先 NFKC 和大小写规范化，再 Jieba 搜索分词（HMM=False），并补充保留连字符、斜杠、小数、乘号等技术标识 token。索引和查询调用同一实现，Tantivy 使用注册的 whitespace tokenizer 消费预分词文本。索引 tokenizer 版本与运行代码不匹配时拒绝查询，要求重建。

`GB/T 31840.3-2025`、`ZC-YJLHV22 26/35kV 3×500mm²`、`U-10Zr` 有实际检索测试。原文不规范化、不改动精度；需要完全一致的技术标识匹配时使用 Exact。BM25 查询最多 512 字符和 64 个不同规范化 token，超限显式报错。

| 字段 | 处理 |
| --- | --- |
| knowledge_base_id/document_id/version_id/chunk_id/builder_version | raw 索引并存储，用于候选过滤、校验和追溯 |
| page/table_id/document_type | raw 字段；页和表格可多值过滤 |
| title | 文件名/标准名/标准编号，权重 3 |
| identifiers | 正文与标题中技术标识，权重 4 |
| section | 章节名及可识别表名，权重 2 |
| content | 正文或结构化表格投影，权重 1 |
| 结构化元数据 | 版本 Manifest 中保存，候选生成前按顶层 Source 字段等值过滤，不重复写入倒排索引 |

得分由 Tantivy 的 BM25 计算，不是词频简单相加。权重通过固定样例排序测试，尚非针对真实领域数据调参所得最优值。Top-K 为 1..100，返回原始 score、稳定 ID、原文 Locator、匹配文本偏移和表格单元格坐标。原始内容取自 Repository，最长返回 12000 字符并标记截断。规范化可能改变字符长度，所以匹配位置仅对能直接匹配原文的 token 给出，不伪造 NFKC 后的坐标；完整匹配请用 Exact。

## 索引发布与恢复

每个知识库一个索引目录：`<sources.db 的父目录>/tantivy/<generation_id>`。同知识库只允许一个构建者，文件锁跨线程和进程生效；构建期间旧代次保持只读服务。

1. `search_changes` 是事务内持久化 dirty outbox；新版本、元数据变化、构建选择、删除写入 revision 计数。
2. 在短暂文档锁下固定期望的 DocumentVersion、Chunk Builder 和元数据快照。
3. 增量构建复制当前索引代次；Tantivy 删除变动文档旧条目，仅添加变动文档的新 Chunk。全量重建从空索引开始。这是文档级增量倒排更新，但磁盘仍需复制当前完整索引。
4. commit 并等待 merge 完成，检查文档计数、Chunk 计数和文件 SHA256，fsync 文件。
5. 发布前重新检查文档集合、版本、构建选择和墓碑。构建期间有变化则本代次 failed，旧指针不变，重试。
6. SQLite 事务内把新代次设 ready、旧代次 superseded，切换 lexical_current 并写 ExternalIndexMapping。只确认未发生变化的 outbox revision。

pending 对应 outbox；building/ready/failed/superseded 在 lexical_generations；删除意图和完成状态在 document_deletions，Document 有墓碑。成功清理后旧 Tantivy 映射标记 deleted。

后台 worker 默认启用（`LOCAL_INDEX_ENABLED=true`），每 5 秒检查已初始化知识库的 dirty 状态，失败保留待重试；mock 模式不自动启动 worker。旧库先手动备份回填。多服务器竞争由文件锁串行化。重启发现 building 将其标记失败并重新同步。查询打开索引时校验文件清单，后续通过文件 stat 变化触发重校验；损坏返回 503 而不冒充空结果，worker 或管理员可从 Repository 重建。没有后台全盘持续校验。

代次回滚只允许文档集合及原文版本仍一致的历史代次，同时恢复该代次的 Chunk 选择；不能复活已删除文档，也不能用索引回滚代替原文版本管理。默认保留历史代次用于回滚，暂无自动 GC；积累磁盘空间是明确的运维成本。

## 删除生命周期

delete_source 首先检查路径安全，再事务记录 pending 删除意图和文档墓碑。普通 get/list/历史读取立即拒绝；清理程序单独用 include_deleting=True 获取已授权任务的源信息，然后按原流程删除 Dify 文档和本地文件，最后清理旧 Source 记录、标记 deleted。远端删除失败保留本地文件和 pending 状态，任务可重试。已完成的删除重复调用成功返回。

Tantivy 候选生成排除墓碑，物理索引条目稍后由 worker 清理。不会等待外部服务成功才禁止读取。V1 已有的原始产物 BLOB 保留政策不变，也没有在此阶段增加物理擦除历史的任务。

失败清理可重复调用既有 `DELETE /api/agents/{agent_id}/sources/{source_id}`（仍需知识库写权限）；失败任务详情保留 source_id。队列正在运行的删除在重启后恢复 pending；已失败的旧队列任务没有新增专用重试 UI，使用上述 DELETE 接口重试清理，不重新开放文档读取。

## 权限、Exact 与大文档边界

知识库/租户权限沿用现有 authorize_document_read 和 document.read。每个请求先取得授权文档集合，再作为 Tantivy term_set 的 Must 过滤，之后计算 Top-K；返回前再次校验。不会用先全库 Top-K 后剔除的方式制造静默漏召回。当前授权回调逐文档执行，规模很大时应将既有权限解析批量化，不能删除权限校验来提速。

Exact 与 BM25 独立：对 Repository 原文做精确字符串、大小写可控匹配或 regex 扫描，支持明确指定历史 version_id，跨 Chunk 匹配通过原文偏移关联 Chunk。无需健康的 Tantivy 索引。每次最多 100 个文档、256 字符模式、20 MB 字符扫描预算、100 个结果、2 秒扫描时间；拒绝反向引用、环视和部分高复杂度表达式，regex 引擎超时中断。到达限制返回 partial/reason/scanned_chars，不能当作完整结果。

Repository 新增 chunks_page、chunks_by_ids、text_window；相邻 Chunk 使用版本/构建/ordinal SQL 范围读取，页码和章节通过 node_locations 关系索引定位。公开读取限制 Chunk 数、文本窗口 100000 字符，超过 2M 字符的单项 JSON 返回 413 并提示分页。内部历史接口保留，巨大的单表/单句仍可能需要 text 分页查看。Exact 扫描预算指进入扫描后的时间，不包含授权阶段；大量小文档的授权成本已计入实测查询延迟。

## 实际接口

前缀 `/api/v2/knowledge-bases/{kb}/local-search`，使用平台现有登录/授权。

```text
POST /bm25    {"query":"GB/T 31840.3-2025","top_k":10,"document_ids":["..."],"filters":{"page":3}}
POST /exact   {"query":"U-10Zr","case_sensitive":true,"document_ids":["..."],"versions":{"文档ID":"版本ID"}}
POST /exact   {"query":"[0-9]+\\.[0-9]+%","regex_mode":true,"max_chars":2000000,"timeout":1}
GET  /status  管理权限；代次状态，不暴露整个原文 Manifest
POST /sync    {} 或 {"full":true}；管理权限
POST /sync    {"chunk_size":2400,"overlap":180}；构建并发布新 Chunk 配置
POST /rollback {"generation_id":"32位代次ID"}；管理权限
```

文档读取前缀 `/api/v2/knowledge-bases/{kb}/documents/{doc}`：`/chunks?builder_version=...&after=...&limit=...`、`/chunks/{id}/adjacent`、`/nodes/{id}`、`/blocks?page=...&section_id=...&after=...`、`/text?start=...&limit=...`。块和 Chunk 游标是返回节点的 ordinal，文本游标是 next_start。

本机管理员命令（不绕过远端用户 API 权限；拥有数据库文件本身视为运维授权）：

```sh
PYTHONPATH=. .runtime/bin/python scripts/local_index.py --database data/sources.db --knowledge-base default-kb sync
PYTHONPATH=. .runtime/bin/python scripts/local_index.py --database data/sources.db --knowledge-base default-kb bm25 --query '电缆'
PYTHONPATH=. .runtime/bin/python scripts/local_index.py --database data/sources.db --knowledge-base default-kb exact --query 'U-10Zr'
# 其余 action：status、rebuild
```

升级前备份控制库、所有 sources.db 和上传目录，停止旧版写进程；执行第一阶段备份回填脚本，再 local_index sync。Schema 2 是追加式迁移：chunk_builds、retrieval_chunks、chunk_selection、node_locations、search_changes、document_deletions，以及索引管理器的 lexical_generations / lexical_current。已有文档/历史表和 Dify 映射不删除。

代码回退到第一阶段前必须停止 worker，不能让第一阶段代码读取正在删除但仍有 Source 行的数据，因为旧代码不识别 pending 删除。完成删除重试或从一致备份恢复后再启动旧版。生产升级未在本次自动执行，性能测试使用隔离目录。

## 验证与性能

最终回归命令和数量见 REFACTOR_ROADMAP.md。新增检索测试覆盖中英文、技术标识、字段排序、表格单元格、跨 Chunk 正则、超时、预过滤、租户与撤权、Builder 迁移/回滚、版本发布、删除重试、损坏恢复、故障注入、并发查询和跨进程访问。

```sh
PYTHONPATH=. .runtime/bin/python scripts/benchmark_local_search.py \
  --output data/local-search-benchmark-phase2-validated --count 100 \
  --metadata data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl
```

运行需要新的输出目录，防止把增量状态误计作全量构建。脚本按路径排序、SHA256 去重，使用本地 PyMuPDF 从实际 PDF 提取文本，不调用 MinerU/Dify；1 份文件不可用被跳过，实际纳入 100 份。绝大多数是 TAT-DQA 财务 PDF，不能代表中文技术标准的真实业务性能。详细文档哈希、问题 ID 和逐题排名见 `LOCAL_SEARCH_BENCHMARK_2026-10-31.json`。

| 指标 | 实测 |
| --- | --- |
| PDF / Chunk | 100 / 951 |
| 原 Markdown 总字节 | 504791 |
| 单个有效 Tantivy 代次字节 | 409996（不含 Repository、历史代次） |
| 本地解析和 Repository 入库 | 1.242 秒 |
| 全量索引 | 1.054 秒 |
| 单文档新版本增量同步 | 1.016 秒（同内容重解析，单文档更新） |
| 删除派生索引 | 0.079 秒（不含 Dify 网络清理） |
| 99 文档全量重建 | 0.995 秒 |
| 新实例加载和校验 | 3.170 ms（操作系统缓存热，不是整机冷启动） |
| 100 次查询 P50 / P95 / max | 54.514 / 61.465 / 68.863 ms |

查询性能是包含逐文档授权及 Repository 验证的本机热查询，10 条财务查询各重复 10 次。另取来源 PDF 在本次语料中的前 100 道 TAT-DQA 标注问题，Top-1/5/10 **Chunk 结果包含正确源 PDF** 的比例分别为 79% / 89% / 90%，MRR@10=0.832083。这个定义按 Chunk 排名计数，同一 PDF 的多个 Chunk 会占位，不是去重文档排序。没有宣称答案准确率、证据 Recall 或表格计算通过。

## 第三阶段衔接

`LocalSearchService.search()` 返回 BM25 score 与稳定版本/Chunk/Locator；`exact()` 返回原文范围、Chunk ID、命中及上下文；`IndexManager` 提供 sync/state/rollback。Dify Vector 仍独立。第三阶段再实现统一规划、RRF、Reranker 和高层工具契约；本阶段已停止于这些接口。
