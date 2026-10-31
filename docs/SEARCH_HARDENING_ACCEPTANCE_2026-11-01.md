# Agentic RAG V2 第二阶段补强验收

日期：2026-11-01（Asia/Shanghai）。范围止于第二阶段，不包含第三阶段。

代码基线：`a803ff3`。本次实现提交：`edc64949a1ce815e83312a4d5bcd1b723bce1d4e`，分支 `codex/v2-document-repository`，已推送至 `origin/codex/v2-document-repository`。本报告和机器数据由后续文档提交保存；该文档提交不改变被测代码，最终 HEAD 可用 `git log -1 --oneline` 核对。main 未改动。

## 实际完成

| 问题 | 实现与验证 |
|---|---|
| Exact 超过 100 文档拒绝 | 改为授权后确定性扫描，2001 文档实际分页查全；不依赖 BM25 命中 |
| 无法续查、截断 regex 可能误判 | HMAC continuation 绑定身份/KB/选项/范围/版本/revision；literal 有界 UTF-8 窗口，regex 完整 subject + 明确预算 |
| NUL 导致 SQLite 文本函数截断 | incremental blob IO 读取 TEXT 的原始 UTF-8；字符偏移与 byte 游标分开，NUL/emoji 有回归测试 |
| 发布漏验标题、业务元数据、revision | 同一读事务构建 Manifest，在正式发布的 BEGIN IMMEDIATE 内完整重验 |
| 发布和 ACK 分离 | 指针、代次状态、外部映射和条件 ACK 同事务；ACK 前异常整笔回滚 |
| outbox revision 清空后从 1 重来 | 新增持久 search_revisions，ACK 不重置；A→B→A 也能检测 |
| Dify/错误/任务状态触发代次 | 显式检索/过滤元数据白名单；无关字段更新为 noop |
| 历史代次无限积累 | 默认保留当前及 2 个兼容历史；跨进程 reader 租约保护 GC，失效目录可回收，诊断行保留 |
| 回滚可能带回旧元数据或删除文档 | 校验 schema/config/tokenizer、来源集合、版本、hash、元数据及构建可用性 |
| 长段落尾部重复分块 | 默认 Builder 升级为 structure-v3-cfb1af48b26c；旧 structure-v2 仍可追溯和重建 |

实际修改的核心文件：

- `app/chunk_builder.py`：有版本的尾块修复。
- `app/document_chunks.py`、`app/db.py`：持久修订号及精确 dirty 触发。
- `app/search/snapshot.py`：统一快照、元数据白名单、schema/config 标识。
- `app/search/index_manager.py`：原子发布/ACK、完整回滚、缓存一致性、查询租约、GC、磁盘诊断。
- `app/search/exact_search.py`、`service.py`：安全续查、原文扫描、候选授权和返回前复核、stale 状态。
- `app/search/api.py`、`app/main.py`：稳定用户身份注入；新增 diagnostics/cleanup；保留旧 status 格式。
- `scripts/local_index.py`、`benchmark_search_hardening.py`：运维及固定语料/合成/业务副本评测。
- `tests/test_search_hardening.py`：40 项新增测试；旧检索及其他回归继续运行。

数据库为追加式迁移：新增 `search_revisions` 和 `exact_cursor_key`；继续使用 `search_changes`、`lexical_generations`、`lexical_current`、`external_index_mappings`，不删除原文、历史版本或 Dify 映射。Manifest envelope 的 schema=2 与 Repository schema 编号分别管理。

## Exact 的机制与边界

搜索按文档 ID、非重叠匹配起点排序。continuation 为 15 分钟 HMAC 凭证，绑定原 query、regex/case、KB、principal、显式 ID/版本/过滤条件、授权范围及候选修订快照。服务重启后能用同库密钥验证；换用户、换范围、更新、删除、撤权会拒绝旧凭证，整库权限仍沿用原 403 逻辑。

每页最多 100 条结果、20000000 个 Unicode 字符、2 秒扫描循环预算；返回 partial/reason、扫描量、完成文档数、字符/字节位置和 continuation。授权与 SQLite 等待不在硬扫描截止内，分别计入 scope_seconds/elapsed_seconds。扫描并非整个 HTTP 请求的硬墙钟超时。

任意 regex 不对不完整 subject 进行结尾/贪婪匹配；放不下整份文档时返回 document_budget 及所需预算。超过最大文档预算时不能保证继续，明确报告限制。恶意/昂贵表达式超时后不跳过未判定部分，续查可能再次超时。字面量有模式长度重叠，可跨 Chunk/读取窗口。零宽匹配不返回，原有反向引用/环视限制保留。

范围元数据上限为 100000 文档或 16 MB；显式 ID 列表最多 10000。UTF-8 输入窗口最多 80 MB，解码/结果/元数据另占内存；没有把输入窗口预算声称为进程 RSS 上限。同凭证重试不会移动服务端游标；预算可能改变返回前缀长度，客户端应按定位键去重并使用返回的新凭证翻页。

## Manifest、发布、回滚及 GC

Manifest 包含知识库、schema、索引配置、tokenizer；每文档包含 ID、version_id、content_hash、builder_version、Chunk 数、原文字节数、检索元数据和持久 revision。初始快照与正式发布快照都在各自短 SQLite 事务内获取，构建和文件 IO 不持有数据库写锁。

新代次完成 Tantivy commit/merge、文档及 Chunk 计数、SHA256 和文件/目录 fsync 后，才进入发布事务。任一相关字段变化则拒绝，旧可用代次继续服务。ACK 只删除相同 document_id+revision 的 outbox。已提交代次不会因之后维护失败被标为 failed。

BM25 请求在整个查询期间持有同一代次共享 flock。GC 只有拿到非阻塞排他锁才删除；catalog 锁封闭“读指针→拿租约”的竞争窗口。锁文件保留避免 inode 重用问题。可用但落后的代次返回 stale；diagnostics 独立报告 usable/pending/stale 和磁盘字节。

`LOCAL_INDEX_RETAIN_HISTORY` 默认 2；回收失效/失败/孤儿目录，保留数据库错误及 Manifest。兼容历史受原文/元数据约束，回滚再做事务内验证。正在使用的旧代次返回 busy，之后重试。失败构建目录在后续成功同步或管理员 cleanup 时清理；不会无条件删光历史。

## 真实 100 PDF / 100 题对比

原 100 个 SHA256、100 个 question_id 固定，原始 Markdown 保持 504791 字节。旧报告 SHA256：
`34459dfc5e1cc16f020b763eb54018bf82dc9f0bb033bca85a6cd2c49cef31a3`。

| 指标 | 2026-10-31 基线 | 补强后 |
|---|---:|---:|
| PDF / Chunk | 100 / 951 | 100 / 468 |
| 来源 Hit@1 | 79% | 79% |
| 来源 Hit@5 | 89% | 90% |
| 来源 Hit@10 | 90% | 92% |
| MRR@10 | 0.832083 | 0.837179 |
| 全量索引 | 1.054 s | 0.810 s |
| 同内容新版本单文档增量 | 1.016 s | 0.102 s |
| 查询 P50 / P95 | 54.514 / 61.465 ms | 54.948 / 61.026 ms |
| 初始当前索引 | 409996 B | 362649 B |

这些是按前 K 个 Chunk 计算的来源 PDF 指标，不是答案准确率、证据召回或表格计算正确率。延迟为一次本机测量，不据微小差异声称统计显著改善。分块尾部修复改变语料词频/长度统计，整体提高 2 个百分点，但也有题目排名下降；没有调权、去掉失败题或添加 gold 公司名。

10 道原未命中问题的逐题文本、操作数、解析/分块/索引/排序因素及不确定性见 [逐题分析](SEARCH_MISSES_2026-11-01.md)。其中原第 9073、9082 题进入 Top-10；其余 8 题仍未命中。Roper 的 gold_context 与提取文本局部标签/数字不同，保留原标签并明确需源 PDF 图像复核，未替换标准答案。

机器记录：[真实结果](SEARCH_HARDENING_REAL_2026-11-01.json)、[逐题证据](SEARCH_MISSES_EVIDENCE_2026-11-01.json)。

## 规模与业务副本实测

设备 Apple M4、macOS 15.6 ARM64、Python 3.13.5，Tantivy 0.27.0、Jieba 0.42.1、regex 2024.11.6。

合成数据为 **2001 个独立 Document ID**，四类中英文模板，变化的年份、区域、编号、数值及元数据，2–20 段普通文档、每 100 份一份 300 节长文档、每 25 份一份 120 行表格。合计 4.35 MB Markdown，不能外推成 2001 份复杂扫描 PDF 的性能或质量。没有上传 Dify。

业务副本来自实际 `data/sources.db` 的 SQLite backup：4 份 PDF、3 份 DOCX，含中文活动方案与英文 NIST 文档。仅在隔离副本回填、索引、更新和删除；测试用 repository.delete 不删除原业务文件。

| 指标 | 100 真实 PDF | 2001 synthetic | 7 实际业务文档 |
|---|---:|---:|---:|
| Chunk 数 | 468 | 34915 | 162 |
| Markdown 字节 | 504791 | 4346655 | 132691 |
| SQLite 字节（测量结束） | 77262848 | 255922176 | 7397376 |
| 初始当前 Tantivy 字节 | 362649 | 6675750 | 125445 |
| 末次含历史总索引字节 | 1116997 | 13745154 | 383753 |
| 导入/Repository s | 1.577 | 7.831 | 0.050 |
| 全量索引 s | 0.810 | 16.881 | 0.241 |
| 单文档新版本增量 s | 0.102 | 0.600 | 0.082 |
| 无关状态同步 s（均 noop） | 0.017 | 0.354 | 0.003 |
| 批量元数据条数 | 100 | 100 | 7 |
| 批量写库 / 索引 s | 0.062 / 0.826 | 0.062 / 1.277 | 0.005 / 0.220 |
| 删除写库 / 索引 s | 0.0009 / 0.054 | 0.0016 / 0.429 | 0.0007 / 0.034 |
| 删除后重建 s | 0.809 | 16.968 | 0.211 |
| 100 热查询 P50 / P95 ms | 54.948 / 61.026 | 933.991 / 1008.191 | 12.291 / 14.308 |
| 新进程加载 s | 0.0087 | 0.0295 | 0.0069 |
| 含进程启动墙钟 s | 0.348 | 0.387 | 0.346 |
| 进程峰值 RSS 字节 | 222216192 | 184172544 | 186679296 |

所有查询包含 SQLite 与逐文档权限复核。新进程是进程冷启动、OS 文件缓存热，不是整机冷启动。全量/增量索引时间不包括导入解析；业务导入时间仅为既有文本回填。SQLite 与总索引大小在更新/删除/重建/GC 后测量，保留历史原文；不是构建时的瞬时磁盘峰值。RSS 为该进程导入、索引和查询期间的最高值。

| Exact/Grep 场景 | 页 / 命中 | 读取字符量（含重读） | 总延迟 |
|---|---:|---:|---:|
| 100 PDF，literal 2019 | 7 / 612 | 505856 | 0.605 s |
| 100 PDF，不存在的 sentinel | 1 / 0 | 493045 | 0.047 s |
| 100 PDF，小数百分比 regex | 3 / 254 | 509108 | 0.268 s |
| 2001 synthetic，literal 2019 | 4 / 398 | 3634658 | 3.368 s |
| 2001 synthetic，不存在的 sentinel | 2 / 0 | 3629096 | 1.614 s |
| 2001 synthetic，UNIQUE-[0-9]{5} | 21 / 2001 | 3659399 | 15.893 s |
| 7 业务文档，不存在的 sentinel | 1 / 0 | 120566 | 0.0045 s |

上述分页扫描均最终 complete。各页预算独立，不能把 21 页的总耗时理解成单页超过 2 秒。业务 smoke 的 10 个查询（篮球、秋游、Token、API、risk、NIST 等）均返回 BM25 结果；risk 的 Exact 首页达到 100 条，诚实报告 partial，而非完整命中数。业务没有人工 gold 问答集，因此不报告准确率。

真实 10 道失效题及可访问业务副本没有结构化 Table 节点，无法宣称已完成真实表格定位验收。Markdown/HTML 表格、合并单元格、投影/原文偏移、单位与数值精度由实际 Repository + Tantivy 测试覆盖，明确属于构造测试。

原始结果：[合成规模](SEARCH_HARDENING_SYNTHETIC_2026-11-01.json)、[业务副本](SEARCH_HARDENING_BUSINESS_2026-11-01.json)。

## 实际命令与测试结果

```sh
.runtime/bin/python -m pytest -q
PYTHONPATH=. .runtime/bin/python scripts/benchmark_search_hardening.py --kind real --output data/search-hardening-real-accepted-20261101
PYTHONPATH=. .runtime/bin/python scripts/benchmark_search_hardening.py --kind synthetic --count 2001 --output data/search-hardening-synthetic-accepted-20261101
PYTHONPATH=. .runtime/bin/python scripts/benchmark_search_hardening.py --kind business --output data/search-hardening-business-accepted-20261101
git diff --check
```

最终完整回归：**269 passed，0 failed，7 warnings，38.46 s**。警告来自 SWIG/ldap3/pyasn1。日志见 [实际 pytest 输出](SEARCH_HARDENING_TESTS_2026-11-01.txt)。运行测试使用真实 Tantivy；权限用隔离用户/知识库，故障和竞态用受控注入，未冒充真实内网服务联调。

新增 40 项覆盖 2001 文档、regex 超时/分页/字面量窗口、NUL/Unicode、跨用户/KB/版本/删除/撤权失效、重启/重放、元数据竞态、A→B→A、Builder/config 变化、发布 ACK 中断、提交后维护异常、回滚兼容性、跨进程读租约与 GC、HTML 跨行单元格、中文/英文长句、全部指定技术标识及旧 API 格式。

开发首轮新增测试有 2 个失败：测试将贪婪正则错误预期为两个匹配；删除竞态使构建计数读取提前报 KeyError。分别修正测试模式及改用快照内 Chunk 计数，随后回归通过。首次真实基准只扫描 TAT-DQA 子目录，缺少原清单中另 3 份 PDF，因此主动失败；改为遍历已有 data 目录按原 SHA256 匹配，没有下载或替换样本。中间诊断与被停止的旧合成运行不作为最终性能数据。

## 兼容、风险和未完成事项

1. 原文、文档版本、Dify 映射、旧 Agent 和外部接口保留；原 /status 列表格式保留，diagnostics/cleanup 为新增。没有实现 RRF、向量融合、ReAct、MCP/Tool Gateway 迁移。
2. 升级前备份并停止旧版写进程。旧 Manifest 会要求重建；旧 Chunk 不自动升级。显式 Builder 升级和 Manifest 迁移说明见 [运维文档](LOCAL_SEARCH_V2.md)。
3. 增量仍复制整个旧索引目录；查询保护会暂缓回收，长查询/持有租约的进程可能使磁盘短时超过保留数量。SQLite 历史原文、诊断行和锁文件不自动清理。
4. 候选授权和快照仍为 O(N)。2001 synthetic 的 P95 约 1 秒，不能宣称已达到大型生产库低延迟目标；后续可批量化既有权限解析，不能绕过授权。
5. regex 极大文档/复杂模式可能无法在硬上限内完成；partial/限制明确可见。授权/SQLite 等待不属于硬请求 deadline。
6. 真实表格恢复与真实 MinerU/Dify 内网验收未完成；未测试 Linux AMD64、Docker 或离线安装，本次按用户范围不实施。不要求收集 2000 份真实 PDF。
7. 八道原未命中题仍失败，没有承诺向量检索一定解决；业务 smoke 无人工答案标签。Roper gold_context 差异需后续人工核对 PDF 图像。
8. 所有测试位于隔离目录，正式库未执行迁移或 Builder 升级。这些边界不阻塞第三阶段代码开发，但生产上线和真实证据/表格质量验收仍需补齐。

本次到此停止。只有用户明确要求“执行第三阶段”后才继续。
