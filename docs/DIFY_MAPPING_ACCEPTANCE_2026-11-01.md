# 第三阶段补强：Dify Vector 真实映射闭环验收

日期：2026-11-01。仓库 `RYUe103/Agenertic-rag`，分支 `codex/v2-document-repository`。基线 `65bd7f9`，本次代码提交 **`06727f5`**。代码、测试、本文及脱敏JSON一并提交推送；最终报告提交SHA见 `git log -1`，不在报告中写自身循环哈希。

## 结论

**真实Vector和真实Hybrid最小闭环已通过，证据验证没有放宽。** 使用历史正式入库清单中已启用的一份远端文档，其本地保存的MinerU Markdown与下载的远端原始上传文件SHA256完全一致。只在隔离Repository建立当前版本及明确映射。

实际问题 `tatqa_test_9063`：Vector返回63候选，2条通过原文唯一定位；Hybrid返回9条融合结果，其中一条同时具有BM25与真实Vector贡献。更换隔离库当前版本后，旧向量证据全部拒绝。撤销隔离测试策略权限后拒绝访问。

完整回归 **343 passed、0 failed、7条第三方弃用警告，43.84秒**；较第三阶段309项新增34项。没有新Agent循环、checkpoint、MCP迁移、前端重构、生产数据库迁移或远端上传/启用/删除操作。

## 120候选拒绝的实际原因

先读取正式 `data/sources.db` 的只读快照：7份Source，**DocumentVersion=0、external_index_mappings=0**。此前smoke是在隔离副本回填后检索，不能把回填出的legacy指针当作原始上传回执。

当前Dify目录24份，均indexing_status=completed，但其中只有7份enabled，17份disabled。与本地Source指针对应的7份**全部disabled**。剩余17份不属于当前7文档Repository，其中7份enabled、10份disabled。当前24份目录没有同名重复项（不代表历史上从未重复上传）。

| 复查问题 | Top40候选 | 属于本地7文档 | 不属于本地Repository | Top100实际返回 | Top100属于本地7文档 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 预算 | 40 | 0 | 40 | 63 | 0 |
| Token | 40 | 0 | 40 | 63 | 0 |
| API | 40 | 0 | 40 | 63 | 0 |

**直接拒绝原因是外部候选不属于该Repository；本地7文档被禁用解释了为什么无法召回。** 扩大Top-K没有恢复本地7份，不应将此归因于单纯Top40截断。

初次报告只保留聚合计数，没有原120条Segment身份快照，因此不能逐条重建当时那一轮响应。本次真实复查重现120条全部在scope之外，并通过当前目录和Source指针确定结构性原因。不会把本轮响应冒充原轮原始记录。

| 分类 | 本轮可确认数量/范围 | 解释 |
| --- | --- | --- |
| 外部文档不属于当前Repository | 120/120候选 | 主要拒绝原因；不建立虚假映射 |
| 本地Source指针找不到远端Document | 0/7 | 7个ID都真实存在；不是拼写/ID丢失 |
| Source Dataset ID不一致 | 0/7 | 与当前配置Dataset一致 |
| 正式库缺DocumentVersion/回执 | 7/7 Source，原表记录0/0 | 历史未迁移；只在克隆库回填 |
| 远端索引未完成或失败 | 0/24 | completed不等于enabled |
| 本地对应远端文档被禁用 | 7/7 | 即便Segment enabled，也不能作为可用文档处理 |
| 原始上传文件等于本地Markdown | 3/7 | 三份DOCX的已提交.md可建立来源证明，但仍disabled |
| 原始上传文件不等于本地Markdown | 4/7 | 四份上传的是原始文件；其hash等于本地Source原文件hash，不能谎称等于派生Markdown |
| 已证明版本过期 | 不能由原7份legacy记录断定 | 无可靠版本回执，不能把不同格式直接判作旧版本 |
| 同名重复文档 | 当前目录0个重复名称 | 不用文件名进行绑定，历史重复不作无依据结论 |

脱敏实测记录：[DIFY_MAPPING_AUDIT_2026-11-01.json](DIFY_MAPPING_AUDIT_2026-11-01.json)。该审计使用4秒请求间隔控制云接口请求频率，时延包含主动等待，不能作为检索性能基准。中间探测曾遇到HTTP403；后续独立复测成功，未把失败轮记为通过，也未断言403一定是配额原因。

## 代码修改

| 文件 | 修改 |
| --- | --- |
| app/dify_mapping.py | 固定版本上传尝试、原子回执、状态观察、丢失回执恢复、显式远端原文件校验 |
| app/document_repository.py | 组合映射事务能力；generic sync只保留legacy或撤销信任，不能凭成功状态绑定新版本；保留旧指针历史 |
| app/document_models.py | 新增可空来源证明、双hash、submission、远端文件/状态/可用性、精确range字段 |
| app/ingestion.py | 上传不可变版本的临时.md，接收真实ID后原子确认；延迟索引、失败、状态刷新及删除衔接 |
| app/clients/dify.py | update-by-file响应ID必须等于请求ID；mock契约显式提供可用性字段 |
| app/db.py | Dataset/Document指针变化也撤销旧信任；删除时将映射标记deleted |
| app/retrieval_v2/evidence.py | 证明/双hash/远端可用性检查；严格显式range；外部失败原因细分 |
| tests/test_dify_mapping_v2.py | 34项新增生命周期、恢复、内容、权限/版本相关测试 |
| tests/test_retrieval_v2.py | 原测试显式提供新来源证明字段，保留原测试覆盖和更严格验证 |
| scripts/verify_dify_mapping_v2.py | 只读目录、Source及原上传文件审计；所有本地操作在克隆库 |
| scripts/verify_dify_formal_mapping_v2.py | 历史清单+原文件hash恢复一份隔离文档，真实三模式与换版/撤权验收 |

未重构Planner/RRF/主Agent。数据库复用 `external_index_mappings` JSON payload，无新增平行表。新增字段：binding_method、source_content_hash、submitted_content_hash、submission_id、remote_revision、remote_status、remote_enabled、remote_archived、range_start、range_end。旧字段、旧记录保留；旧记录缺证明时安全降级为未验证，不静默授信。

## 新上传版本映射机制

1. 先发布本地不可变DocumentVersion。
2. BEGIN IMMEDIATE固定version及content_hash，记录唯一submission_id；在重用远端ID前先supersede旧回执。
3. actual submitted bytes必须等于固定版本的UTF-8字节，分别记录source_content_hash和submitted_content_hash。本阶段平台统一提交Repository Markdown，二者相等；不会把远端原PDF哈希谎称为Markdown哈希。
4. 用临时文件上传这些固定字节，避免工作Markdown被并发改写。接收真实Document ID后，在同一事务写回执及Source的dataset/document/status。
5. completed且远端身份一致、enabled=true、archived=false后，才允许该回执成为候选。缺可用性字段也不能成为已验证证据。
6. 索引延迟为processing；失败为failed；远端文件ID变化为stale；删除为deleted；新尝试撤销旧尝试。Source普通状态更新只能撤销信任，不能将旧映射升级为新版本成功回执。
7. 请求结果不明确时保留submitting/uncertain，后续重试被阻止，防止无幂等键的Dify create产生不明重复。进程重启后仍有效。
8. `reconcile_dify_submission`以远端原上传文件的完整hash、文件ID和完成状态恢复丢失回执。无法找到可靠远端身份时仍阻止自动创建，不能凭文件名猜测。

重复确认同一尝试/同一远端ID幂等；冲突ID拒绝。并发换版本或删除后，迟到回执只保留为superseded，不更新当前Source指针。跨本地文档重复使用同一有效外部身份拒绝。旧legacy指针改动保留历史记录。

历史恢复方法 `verify_legacy_dify_file` 是显式内部入口：要求已知source dataset/document指针、认证读取的远端原上传文件及完整hash相同。当前只在隔离验收库调用，没有执行生产迁移。Dify list与detail的原文件信息容器不同，已统一读取data_source_detail_dict/data_source_info。

## Segment证据定位

- **A：明确映射**：Segment→Chunk需全文一致且属于指定版本；Segment→range需范围长度、原文片段逐字一致。多个冲突range拒绝。
- **B：版本内唯一定位**：有完整来源证明的文档级映射，在最多4MB的版本内逐字查找，必须唯一；再恢复Repository Chunk或跨Chunk原文范围。不是按相似度猜页码。
- **C：不能确认**：区分scope外、legacy未验证、旧版本、状态/hash错误、原文不存在、多处出现、预算不足等；不返回其外部正文作为证据。

源文件hash一致并不保证每个Dify Segment能逐字定位。本次9条目标文档候选中，2条成功、7条原文字符串不存在（远端解析/表示与保存的Markdown不同）；这7条仍拒绝。没有改为空白模糊匹配或拼接猜测，也没有把Segment ID充当Chunk ID。

## 真实闭环：使用现有启用文档，零远端写入

历史 `data/t2-ragbench/results/formal-ingest-100.json` 中有7个Document ID对应当前仍启用的远端文档；本地保存着对应context的 `mineru-original.md`。这一链路来自历史上传记录，不是文件名推断。

选用一份：本地已提交Markdown与Dify原文件都是2983字节，SHA256均为：

```text
1491a965a83f32675d2e50c93b18d7539fa735c84d1f61f492ebedb0db355677
```

认证下载前后原文件ID一致，远端completed/enabled且未archived。在全新隔离KB中保存这份已存在的提交产物，生成隔离版本，再根据清单ID和全文件hash建立来源证明。**这不是向正式7文档库补造历史版本，也没有将检索到的Segment反向伪造成原文。**

| 实测模式 | 后端候选 | 最终证据 | 总耗时ms | 结果 |
| --- | --- | ---: | ---: | --- |
| Lexical Only | BM25 8 | 8 | 6.94 | ok |
| Vector Only | Dify 63，目标文档9，其中2精确定位 | 2 | 2157.05 | degraded，61条拒绝 |
| Hybrid | BM25 8 + Vector 63 + Exact 2 | 9 | 2126.52 | degraded，但有真实多路融合 |

本题触发既有规划的Exact补充，所以Hybrid包含三路，不能描述成严格的双路消融。Vector请求无Fake，来自真实semantic_search；54条在隔离scope之外，7条目标文档的文本不能精确定位。RRF后去重3条。

可核算的lexical+vector融合证据：BM25 rank=4、Vector rank=58，权重均1，k=60：

```text
1/(60+4) + 1/(60+58) = 0.02409957627118644
```

Repository Chunk原文范围 `[2011,2124)`，远端Segment唯一匹配范围 `[2012,2122)`，状态verified_current。保留Block/Section关系；page_numbers为空，没有猜页码。另一条Vector证据使用原文range，不伪造Chunk ID。完整脱敏provenance、rank、score、locator、内容hash及耗时见 [DIFY_MAPPING_CLOSURE_2026-11-01.json](DIFY_MAPPING_CLOSURE_2026-11-01.json)。报告不包含原文正文、名称、原始用户身份、令牌或下载签名URL。

## 版本、权限与安全降级

真实闭环后，只改变隔离Repository当前版本，Dify不动。再次真实Vector查询仍有63条候选，**已验证结果0条**：9条旧版本映射拒绝，54条scope外拒绝，状态insufficient_evidence，耗时约2001.37ms。

随后撤销隔离AccessPolicy的KB授权，查询抛PermissionError。它证明同一核心服务会执行权限策略，不是生产LDAP用户实测；真实租户成员变更、角色、会话、批量ACL、跨KB隔离仍由完整回归覆盖。

远端403/超时不会被理解为知识库没有答案；Hybrid保留已经验证的BM25结果并标记降级。没有取消旧版本、位置、权限或内容校验来提高通过数。

## 自动化测试与复现

```sh
.runtime/bin/python -m pytest tests/test_dify_mapping_v2.py tests/test_document_repository.py tests/test_ingestion.py tests/test_retrieval_v2.py -q
# 103 passed
.runtime/bin/python -m pytest -q
# 343 passed, 7 warnings in 43.84s
git diff --check

.runtime/bin/python -m scripts.verify_dify_mapping_v2 \
  --output data/dify-mapping-audit-accepted-20261101 --request-spacing 4
.runtime/bin/python -m scripts.verify_dify_formal_mapping_v2 \
  --output data/dify-formal-closure-accepted-20261101
```

复跑必须选择新的隔离输出目录；脚本拒绝覆盖已有sources.db。配置从.env读取，不打印Key。真实云请求可能受限，失败会保留明确诊断；完整自动化测试不依赖真实Dify配额。

新增34项覆盖：新上传双hash/回执、同版本重上传、版本更新、索引延迟与失败/完成、丢失回执与重启、冲突ID、同名不同内容、唯一/重复/不存在的Segment、显式Chunk/range与跨Chunk、历史文件hash校验幂等、真实协议字段容器、父文档禁用/归档/可用性缺失、远端原文件变更、删除、Source指针变更撤销、请求超时/大文档/输出预算、Vector/Hybrid契约。原第三阶段中的撤权、并发版本改变、向量失败BM25降级、RRF数学测试继续保留。

新上传与重试的写入协议使用明确的测试Fake；本次真实云闭环只执行只读API与检索，**没有声称验证了真实云上传恢复、内网服务或生产迁移**。完整输出：[DIFY_MAPPING_TESTS_2026-11-01.txt](DIFY_MAPPING_TESTS_2026-11-01.txt)。

## 尚未解决与第四阶段前提

1. 正式本地7文档对应的Dify文档仍disabled；没有擅自启用。正式库仍保留原状态，未执行版本迁移/回填。
2. 当前旧库缺上传回执。可用完整原文件hash或受控新上传建立证明，不能把所有旧Source指针自动提升为可信映射。
3. 4份原始文件与派生Markdown的hash不同，没有实现跨格式来源证明迁移；本次不将其冒充已验证的当前Markdown版本。
4. 同源9个Segment中仍有7个不能逐字定位，安全拒绝。允许未来在独立验收后增加可复核转换锚点，当前未放宽标准。
5. 无真实云新上传/丢失回执恢复验收；这些能力目前通过本地契约测试。没有单独授权的可写测试Dataset，不写现有生产Dataset。
6. 单文档/单问题只证明来源验证与真实融合通路，不证明Vector Recall、Hybrid Recall或最终答案比BM25更准确。100题同语料对照、真实重排模型、内网验收仍未开展。

**第四阶段核心接口和最小真实证据闭环的接入前提已具备；生产全库可用性与旧数据迁移没有完成。** 后续Agent仍应只消费verified证据，并保留降级语义。本次完成后停止，等待用户明确要求执行第四阶段。
