# Knowledge Runtime V2

截至 2026-11-01，第三阶段统一检索、Evidence Resolver 与 Tool API V2 已实现，详见 [UNIFIED_RETRIEVAL_V2.md](UNIFIED_RETRIEVAL_V2.md) 和本文末尾增量。第二阶段索引与运维方案见 [LOCAL_SEARCH_V2.md](LOCAL_SEARCH_V2.md)。下文保留早期设计记录；固定字符 Chunk 已由结构感知 Builder 取代。第四、第五阶段未实施。

## 第二阶段架构增量

原文 Repository → 独立 Chunk Build → Tantivy 派生索引代次。业务检索经 LocalSearchService 授权后调用 BM25 或 Exact；不经过 Dify 执行 BM25。新增检索构建和结构关系表位于原 sources.db，没有建立平行的原文 Repository。DocumentVersion 不随分块配置改变，Chunk ID 包含原文版本和 Builder 版本。

索引管理器采用复制旧代次、文档级增量写入、计数/哈希校验、SQLite 发布指针的机制；读服务在新代次发布前继续读取旧代次。删除先记录墓碑和可重试意图，再执行外部清理。详细版本边界、回滚约束和性能局限见上述运维文档。

补强后，索引快照、dirty revision 和检索元数据由单一白名单定义；正式发布事务内重验完整快照，指针、映射、outbox ACK 原子提交。跨进程查询租约保护代次回收，默认保留当前代次及最多两个兼容历史。Exact 使用独立原文扫描与身份/版本绑定的签名 continuation；新增结构 v3 只修复重复尾块，旧 v2 构建仍可追溯。

## 第一阶段历史设计

实施日期：2026-10-31。范围仅为原文 Repository、结构化版本、迁移和读取服务。默认 Agent 仍使用现有执行图。

## 现有链路审计与改造

- 原文件位于每个知识库的上传目录；`sources` 保存路径、解析状态、远端文档 ID、元数据。旧 `source_versions` 是处理状态历史，`source_revisions` 才是 Markdown 修订快照，二者均保留。
- 原来的 Markdown、extracted.md 和 MinerU 产物会被重解析覆盖。现在覆盖前回填历史，解析成功后在调用 Dify 前，以事务同时写旧修订与新 Repository。远端失败不丢失本地事实；普通重试不产生重复修订，显式重解析产生新版本。
- MinerU 本地响应、公开 API ZIP 和可识别 content list 保存为解析产物；Repository 按内容哈希快照原文件、解析 Markdown、响应、ZIP、元数据及 Workflow 产物。旧版本没有保存过的原件/解析产物不能反推，不将当前文件伪装成历史原件。
- 旧 PDF 页码标记和 DOCX 段落标记保留；新结构包括章节父子关系、原文 Block、独立 Chunk、Markdown/HTML 表格及 DOCX 合并单元格。图片引用留在 Block 属性中，原图片可能位于原件或 MinerU ZIP 内，不提供图片渲染服务。
- Dify 文档 ID 原来位于 `sources`；Segment ID 原来来自远端检索结果，未形成完整本地映射。新增版本化映射表，不捏造 Segment 与平台 Chunk 的一一对应。
- 删除仍经过旧权限与队列链路；Repository 写入墓碑，所有普通读取包括指定旧版本都检查源文档是否仍存在。底层历史 BLOB 暂不物理清理。

## 管理层级与持久化

租户/成员权限 → 知识库 → Document（沿用 source_id）→ DocumentVersion → Page / Section / Block / Table / Cell。Chunk 是独立检索单元，通过 EvidenceLocator 引用原文结构。

每个知识库沿用独立 SQLite 文件，Repository 初始化校验知识库 ID，租户归属使用现有控制库，避免复制会过期的权限数据。

| 表 | 内容 |
| --- | --- |
| document_schema | 本阶段 Schema 版本 1 |
| documents | 稳定文档 ID、知识库、当前版本指针、元数据、删除墓碑 |
| document_versions | 明确版本 ID/序号、完整 Markdown、SHA256、结构构建版本、溯源 |
| document_nodes | Page/Section/Block/Chunk/Table/Cell 的类型、顺序和结构 JSON，按版本索引 |
| document_artifacts | 按 SHA256 去重的不可变文件字节 |
| document_version_artifacts | 版本到产物名称和内容哈希的关系 |
| external_index_mappings | Adapter、索引版本、状态、远端 Document/Segment ID、可选 Chunk ID |
| document_backfill_errors | 可重试回填失败记录，不保存敏感异常详情 |
| legacy_chunk_configuration（控制库） | 旧特殊分隔符配置的迁移归档 |

`source_revisions` 和 V2 发布处于同一个 SQLite 事务；版本内节点 ID 可重现，跨版本 ID 不复用。新版本发布不会提前宣称远端索引成功；远端状态更新才记录映射，同一个远端文档被后续版本使用时旧映射标记 superseded。

EvidenceLocator 使用 Unicode 字符偏移，左闭右开；未知页码用空集合，未知 bbox/坐标系统为 null。MinerU 文本必须与唯一 Block 精确匹配才挂接定位信息；bbox 没有明确坐标约定时只保留在原始属性，不解释为 PDF 坐标。

Chunk Builder 当前为 `chars-1800-v1`：每 1800 字符建立独立 Chunk，保留重叠原文块、章节、页码和表格 ID，支持通过相邻 Chunk 或完整 Block 恢复上下文。不是分词器分块，也不是每段或每章一个 Chunk。`__SPLIT__` 不参与分块；原文出现该字符串仍作为普通内容保存。旧 Dify 特殊分隔符配置归档后转自动分段，远端分段与平台 Chunk 独立。

表格保存原文、零基行列坐标、表头、rowspan/colspan、单元格文本。仅明确的单位标签和支持的数字单位后缀填入 unit，其余保留原文并设 null；没有可靠原始位置时单元格 bbox 为 null。

## 读取服务与权限

内部 `DocumentRepository` 面向可信服务，业务入口必须经过 `DocumentReadService`。每次读取重新执行现有租户/知识库访问控制、document.read 与文档归属检查，不以知道 ID 作为授权。

REST 前缀：`/api/v2/knowledge-bases/{kb}/documents`。

- `/{doc}`、`/{doc}/versions`、`/{doc}/content?version_id=...`：文档、版本列表和完整原文。
- `/{doc}/structure`、`/{doc}/outline`：结构和目录；复合结构读取固定同一个版本。
- `/{doc}/nodes/{node_id}`：Block、Chunk、Table、Cell 等节点，拒绝其他文档或版本的 ID。
- `/{doc}/chunks/{chunk_id}/adjacent?before=1&after=1`：包含自身的邻接读取，前后各最多 20 个。
- `/{doc}/blocks?page=...&section_id=...`：页码和章节交集过滤。
- `/{doc}/tables?query=...`：结构化表格列表，query 为表格原文包含过滤，不是检索索引。
- `/{doc}/mappings`、`/{doc}/artifacts`、`/{doc}/artifacts/{name}`：映射状态、产物清单及原始字节。
- `POST /backfill`：额外校验 document.write，逐文档回填。

版本相关 GET 均接受可选 version_id。旧全文、章节和表格读取已改用 Repository。旧 Agent、部分 grep/页上下文与远端邻接接口暂时保留；没有复制或迁移整套 Tool Gateway。

## 迁移和回滚

部署前停止旧版写入进程，备份控制库、每个知识库 sources.db 和上传目录。控制库的特殊分隔符迁移在初始化时执行，因此也必须提前备份控制库。

```sh
PYTHONPATH=. .runtime/bin/python scripts/backfill_documents_v2.py \
  --database data/sources.db --knowledge-base default-kb
```

其他知识库使用各自数据库路径和知识库 ID。脚本先通过 SQLite backup API 生成 `.v1-backup`，再回填；存在失败返回非零退出码。使用相同命令可重试。无需 MinerU、Dify 或 LLM。

迁移按文档使用 BEGIN IMMEDIATE：结构失败则该文档事务回滚，其他文档可成功；重启后按稳定版本 ID 幂等继续。旧修订和状态历史保留，当前版本存在后普通回填不再把磁盘上的临时内容发布为新版本。没有 Markdown 和修订的文档保留元数据但无可读内容版本。

入库、删除和回填共享跨进程排他文件锁，可重入；目前同一知识库的变更串行化，防止回填抓取正在覆盖的文件。不同知识库可并行。代价是远端慢请求会占用该知识库变更锁。

回滚前停止写入，恢复备份数据库和对应上传目录，再运行旧提交。不要仅恢复一个旧数据库而保留不匹配的新文件；V2 实施后新写入的数据须先导出保留。正常回填不删除旧表或外部数据。

## 第二阶段调用约定

- `index_manifest()`：知识库内当前文档版本和删除墓碑，供索引任务决定增量操作。
- `version(document_id, version_id)`：固定版本、content_hash 和结构构建版本。
- `iter_chunks(document_id, version_id)`：Chunk 的稳定 ID、文本、builder_version、原文定位。
- `nodes` / `node` / `located_blocks` / `adjacent_chunks`：原文恢复和精确引用。
- `put_mapping(document_id, ExternalIndexMapping)`：索引成功后记录 Adapter 的索引代次和映射，校验 Chunk/版本归属。
- `mappings(document_id, version_id)`：查询已记录状态。

这些是内部索引任务接口，不作为未经授权的外部 API 暴露。Tantivy BM25、Dify Vector、Exact/Grep 必须作为独立适配器；本阶段尚未创建任何索引或索引任务。后续索引器需要处理构建期间版本变化、墓碑、幂等提交及失败重试。

## 已知限制

- 旧文件未保存的页码、bbox、历史原件、Segment 映射无法补造；MinerU 结构无法精确匹配的部分仍保存在原始产物中。
- 复杂嵌套/畸形 HTML、旧版带多余空行的 Markdown 表格可能只保留原文，无法恢复完整结构。没有语义表格单位推断。
- SQLite BLOB 快照会增加数据库大小，当前没有历史保留期限或垃圾回收。删除限制业务访问，不代表立即擦除所有字节。
- 当前 Chunk 固定字符长度，可能切开表格；可通过 table_id 读取完整表格。尚无分页式大规模节点遍历或性能容量承诺。
- 普通读请求可惰性回填旧文档，首次读取耗时可能增加；正式部署建议先执行备份回填脚本。
- 未接入真实内网 MinerU/Dify 验收；外部联调与第二至五阶段均未完成。

## 第三阶段增量（2026-11-01）

新增 `app/retrieval_v2/`：Repository → 实时授权快照 → 确定性 Planner → 独立 Tantivy / Dify semantic / Exact → EvidenceResolver → RRF / 可选模型重排 → 文档多样性与上下文 → 最终权限/版本/映射重核。统一工具服务为后续 Agent 与 MCP 的共同入口。

`/api/v2/retrieval/search`、`/api/v2/retrieval/tools` 和工具调用入口已可用，17 个只读工具分高/低两层，复用治理与审计。低层要求 `tool.advanced`。旧执行图、旧工具及旧接口保留。没有 Document Repository 表的破坏性迁移；新工具配置幂等初始化。

原文证据必须验证 Chunk/version/Block/section/table 归属与原文一致性；Dify segment 不能当作本地 Chunk。当前语料的真实 Dify 映射不足，拒绝未验证候选是显式降级，不能理解为没有相关知识。

详见 [第三阶段接口](UNIFIED_RETRIEVAL_V2.md)、[验收](UNIFIED_RETRIEVAL_ACCEPTANCE_2026-11-01.md)。上文保留前两阶段架构记录。第四阶段 ReAct、checkpoint 与第五阶段全面迁移未实施。
