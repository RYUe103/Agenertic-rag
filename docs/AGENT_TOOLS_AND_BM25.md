# Agent 工具和 BM25 实现说明

更新时间：2026-10-31。本文依据当前工作目录代码和已有评测记录整理；工具是否能在某个知识库使用，还取决于授权、管理员开关、配置和执行入口。

## 先看结论

- 平台注册了向量、全文、混合、本地文本搜索、文档读取、数据处理和文档写入工具，但**注册在工具中心不代表问答 Agent 一定能自主选择它们**。
- `search_bm25` 是本项目对 Dify `full_text_search` 模式的封装，本项目没有自行实现 BM25 索引或评分公式，也不能仅凭工具名确认 Dify 服务内部具体使用哪一种评分实现。
- 最近的 29 题 Agent 评测中，`search_bm25` 和 `search_hybrid` 都没有被调用。早期另有一次直接使用 Dify 全文检索的三题检查，但那不是 Agent 问答评测。
- 本地 `grep_files` 是精确文本匹配，不是 BM25；`vector_search` 使用 Dify 配置的检索模式，最近一次测试配置为语义向量检索。

## 系统如何运行

入库的主要路径为：原始文件保存到本地 → 所选解析器（当前测试使用 MinerU 云端）生成 Markdown → 保存本地 Markdown → 上传 Markdown 到 Dify → Dify 建立索引。源文档和 Dify 文档 ID 保存在本地记录中，供 Agent 把远端命中映射到本地文档。

问答使用 LangGraph：规划工具 → 执行检索 → 判断证据是否充分 → 必要时继续规划或读取文件 → 生成答案 → 校验引用。实现见 `app/agent.py` 的 `_build_graph`、`_plan`、`_invoke_tool`；解析和入库见 `app/document_parser.py`、`app/ingestion.py`。

工具分两层：

1. `app/tool_registry.py` 登记工具中心和外部调用的工具；默认授权名称见 `app/agent_registry.py` 的 `DEFAULT_TOOLS`。具体账号和知识库可以有不同授权。
2. `app/agent.py` 的 `_plan` 只把部分工具提供给问答模型：初始包括 `vector_search`、`grep_files`、`keyword_search`、`find_files`、`metadata_search`、`dify_metadata_search`；确定候选文档后可增加文档内搜索、全文和目录；已有分段引用时可增加相邻分段。可用 Python Sandbox 时另有 `execute_python`。

因此，`search_bm25`、`search_hybrid` 等虽然在工具中心有实现和授权机制，当前普通问答规划器的初始列表并不包含它们，不能假设 Agent 会自行选用。工具中心测试、经授权的外部接口和检索测试页面是不同入口。

## 知识检索工具

下表的主要实现均在 `app/retrieval.py`；Dify HTTP 请求在 `app/clients/dify.py`。

| 工具 | 具体实现和适用情况 | 依赖与限制 |
| --- | --- | --- |
| `vector_search` | 调用 Dify `retrieve`；默认检索模式由客户端配置决定，通常为 `semantic_search`。按 Dify 文档 ID 或名称关联本地来源，返回分段、得分及引用信息。 | Dify 和本地文档映射；找不到本地来源的命中会被丢弃。 |
| `search_bm25` | 调用 Dify `retrieve`，显式指定 `full_text_search`，将结果标记为 `bm25`。 | 依赖 Dify 全文索引；当前不是问答规划器的初始候选。名字不证明 Dify 内部一定使用特定 BM25 实现。 |
| `search_hybrid` | 调用 Dify `retrieve`，显式指定 `hybrid_search`。 | 混合检索、排序与服务端配置由 Dify 执行；当前不是问答规划器的初始候选。 |
| `search_knowledge` | 代理调用 `search_hybrid`。 | 同上，不是普通问答规划器的初始候选。 |
| `dify_metadata_search` | 对 Dify 检索附带元数据过滤条件，并将结果映射到本地来源。 | 依赖 Dify 已存在的元数据字段，不能随意虚构字段名。 |
| `grep_files` | 对本地 Markdown 做关键词匹配；有 `rg` 时优先使用 ripgrep，否则使用 Python 搜索。默认给出命中行前后上下文并合并重叠窗口；支持词组、大小写和候选文档范围。 | 需要本地文档登记和 Markdown；适合明确的名称、日期、数字及原文词，不是语义向量检索或 BM25。 |
| `keyword_search` | 在已登记文件的文件名、自定义文本元数据和 Markdown 正文中查找完整关键词，可筛选及按元数据排序。 | 更适合定位和列举文档；依赖本地文件及已采集元数据。 |
| `find_files` | 基于文件名、标准号、标识符及本地数据库搜索候选文档。 | 文件名是哈希值且缺乏可搜索元数据时可能找不到；找到文件并不自动证明正文结论。 |
| `metadata_search` | 按本地记录的标准号、状态、日期、年份及已配置的自定义元数据筛选或排序。 | 只能使用实际采集的字段，正文主题不等于元数据条件。 |
| `search_within_document` | 对问题提取检索词，再限定指定 `source_id` 调用 `grep_files`。 | 已知文档后使用；仍然是本地关键词匹配。 |

`DifyClient.retrieve` 统一调用 `POST /datasets/{dataset_id}/retrieve`，传递 `search_method`、`top_k` 和可选元数据条件；当前代码对该请求设置 `reranking_enable=false`、`score_threshold_enabled=false`。得分是 Dify 返回值，不同模式之间不能直接当成可比较的准确率。

## 文档读取工具

| 工具 | 具体实现 |
| --- | --- |
| `read_full_document` / `get_document` | 读取本地 Markdown，超过限制时截断；`get_document` 复用全文读取实现。 |
| `read_document_section` | 在本地 Markdown 中匹配标题，并读取到同级或更高级标题为止；标题不匹配时返回空内容。 |
| `get_document_page` | 读取指定 PDF 页所对应的解析文本，依赖正文中可用的页码标记；没有可靠页码时不能推断。 |
| `get_document_table` / `extract_table` | 按顺序识别 Markdown 管道表格并提取；不保证识别 HTML 表格、图片表格或所有复杂表格。 |
| `get_document_outline` | 根据 Markdown 标题、目录和可用页码标记生成目录。 |
| `get_document_status` | 从本地记录读取文件、解析及 Dify 索引状态等信息。 |
| `get_adjacent_chunks` | 根据 Dify 文档 ID 和分段 ID 拉取分段，再返回目标分段前后内容；依赖 Dify 可访问且分段 ID 有效。 |

这些方法大多要求文档已在平台来源库登记。前面直接上传 Dify 的测试文档没有自动进入正式本地来源库；最近 Agent 评测是把它们登记到隔离的临时库后运行，并未改变正式知识库。

## 数据、写入和执行工具

| 工具 | 实现及约束 |
| --- | --- |
| `analyze_data` | 工具调用接口对传入的结构化行列做基础统计；不等于通用数据分析 Agent。 |
| `export_csv` / `export_excel` | 根据传入的列和行生成 CSV 或 XLSX 内容供下载；不是将结果写回知识库。 |
| `upload_document` | 接收 Base64 内容，创建异步入库任务；单文件上限 25 MB。 |
| `delete_document` | 调用已有文档删除流程，包括相关索引清理。 |
| `parse_document` | 对已有文档创建重新解析任务。 |
| `rebuild_document_index` | 使用已确认的内容创建重建索引任务。 |
| `execute_python` | 满足 Sandbox 和权限配置时，问答规划器可获得受控 Python 执行能力；默认不可假设启用。 |

前四种文档写操作要求写权限和有效人工审批。审批恢复当前是重新执行请求，不是真正的 LangGraph checkpoint 恢复。以上工具的具体调用和校验主要见 `app/main.py` 和 `app/tool_registry.py`；注册工具与 Agent 问答规划器实际暴露的工具范围有区别。

## BM25 在前面评测中做了什么

### 早期三题：单独的全文检索检查

曾把一份测试 PDF 直接上传 Dify，并对其对应的三道题直接调用 `full_text_search`，Top-K 为 5。三题均返回目标 PDF 和含关键数字的分段，返回的分数显示为 0.0。该检查绕过了 Agent，也没有走 MinerU Markdown 的正式入库路径；因此只能说明当时的 Dify 全文检索接口返回了相关文档，不能据此得出 BM25 质量或排序效果。之后临时 PDF 已删除。

### 本地工具 Agent 测试

最初三题及后续 10 题使用真实 Agent 和模型，但把 Dify 检索替换为空结果，使用临时来源库提供本地 Markdown。10 题的记录显示：`grep_files` 实际调用 7 次、`vector_search` 尝试 10 次（返回空）、`search_within_document` 2 次；未调用 `search_bm25`。结果文件：`data/t2-ragbench/results/agent-local-10.jsonl`。

### 最近 29 题 Agent 测试

对 7 份已索引文档对应的 29 题，在隔离临时来源库中启用真实 Dify 检索和真实模型。日志中实际调用：`vector_search` 11 次、`grep_files` 14 次、`search_within_document` 14 次、`find_files` 1 次、`get_adjacent_chunks` 1 次、`read_full_document` 1 次。**`search_bm25` 和 `search_hybrid` 都是 0 次**。4 题模型请求报错；工具次数不等于题数。记录：`data/t2-ragbench/results/agent-vector-29.jsonl`。

另外，之前单独的 10 题 Dify 召回检查明确指定 `semantic_search`，不属于 BM25 检索，也不是完整 Agent 回答评测。

## 当前限制和下一步

- 要比较 BM25、向量、混合检索，应在同一批已入库文档和相同题目、Top-K 下分别调用三种 Dify 模式，记录目标文档和证据片段命中率、耗时及失败原因；不能直接拿不同模式原始分数当准确率。
- 要测试 Agent 何时会选择 BM25，需要先把 `search_bm25` 和相关参数描述接入问答规划器，并验证授权、工具执行和失败回退；当前仅有工具中心入口。
- 目前 Agent 对无证据或引用校验失败的结果仍可能输出具体数字，且 `evidence_insufficient` 与引用校验未通过并不总是同步；不能用模型自评通过数代替人工或独立评测的答案正确率。
- 29 题测试是一个小样本，且 4 题请求失败，不能外推为整套数据集成绩。
