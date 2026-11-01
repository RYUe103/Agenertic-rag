# 第四阶段上下文、Token 与工具输出优化：固定 10 题验收

日期：2026-11-01；分支：`codex/v2-document-repository`；基线：`425b80e`。
实现提交：`9853b5e2e07a5c545ad3bec5b239571b9b388c5a`。本报告和脱敏逐题 JSON 另行提交；报告自身 SHA 使用 `git log -1 --format=%H -- docs/AGENT_CONTEXT_OPTIMIZATION_10.md` 查询。交付回复记录实际远端推送校验结果。

## 结论与边界

完成上下文视图、按工具输出预算、长证据、计算来源和安全分页补强，未修改主图结构、Repository、解析器、排序算法或生产数据。只运行附件指定的 10 题一次，没有额外模型裁判，没有重跑原 100 题或已完成请求。

预算停止从 7/10 降至 0/10；引用技术通过从 0/10 升至 6/10，标准文档命中 5/10。未达到至少 7 题获得有效引用的优化目标，也不能把 8 个 answer 停止状态当作成功答题。

**本轮没有实现总体 Token/耗时降低**：实际 Agent Token 从 593,802 增至 650,605（+9.57%），累计单题耗时从 205.15 秒增至 249.39 秒（+21.56%）。更多题得以继续执行，且有 5 次引用修复；工具观察变短不意味着整个 Run 更便宜。问题已从保守预算提前停止，进一步暴露为数字定位、回答完整性与引用可见性问题。

## 审计与实际修改

原 `model_call()` 将消息和 Schema 的 UTF-8 字节数直接作为输入 token 预留。英文 JSON、长 ID、表格及重复元数据会显著抬高预留；累计实际 usage 尚未达到总限额时，下一轮就因过大的预留而停止。工具结果中候选文档的 chunk_ids、table_ids、section_ids 等长数组和证据正文反复进入消息历史，原有两处统一 2,000 字符限制又迫使部分问题继续检索。

| 核心文件 | 实际变化 |
|---|---|
| `app/agent_v2/context.py` | 独立 TokenBudgetManager、tool_output_profile、独立模型输入视图及旧观察压缩 |
| `app/agent_v2/runtime.py` | 分类型预算、规范化计算参数、上下文装箱、真实 usage 累计、可见来源及逐调用指标 |
| `app/agent_v2/evidence.py` | 取消统一 2K、精确前缀范围、分段精读、表格边界保护、延迟读取导航 |
| `app/agent_v2/citations.py` | 精简候选元数据、重复元数据去重、分页导航、保留可信短来源编号 |
| `app/agent_v2/state.py` | 新增单次上下文 token 上限；旧 checkpoint 使用兼容默认值 |
| `app/retrieval_v2/tools.py` | 目录分页、表格分页、明确计算错误原因；小表原接口保留 |
| `app/retrieval_v2/table_page.py` | 原文行范围、表头/单位/坐标保留，跨行合并单元格原子分页 |
| `app/retrieval_v2/calculations.py` | Source 别名 Schema、精确千分位/Unicode 负号解析和数字边界校验 |
| `app/retrieval_v2/models.py` | find_document 导航起始序号，不改底层排序 |
| `scripts/evaluate_agent_citations.py` | 严格题号白名单、冻结语料复用、新目录、禁止重放已启动请求、裁判显式 opt-in |
| `tests/test_agent_context.py` | 新增 19 项测试；原 473 项均保留 |

另外审阅了 checkpoint、retrieval service、LLMClient 和 LLMModelPool。模型调用使用现有原生工具协议；usage 来自返回的 response_metadata，模型池仍透传响应。未替换这些组件，也未引入摘要模型调用。

## 预算与可信上下文

总 token 120,000、单次输出 2,000、总时限 300 秒、context_chars 80,000、工具 8 次、检索 4 轮均与原批次一致。新增单次上下文窗口策略 32,000 tokens，这是运行时保守配置，不声称等于服务商型号的最大窗口。

未安装对应 Qwen tokenizer，本次使用明确标注的估算器：ASCII 字符约除以 3，CJK 按 1.5，其他 Unicode 按其编码宽度估算，另加 framing 和 25% 安全余量。它不是精确 token 数，更不把 UTF-8 字节数写成实际消耗。Provider usage 存在时替换预估，缺失才估算兜底，绝不重复相加。对观测到的低估提高后续估算系数，并持久化到 checkpoint。

本次 51 次成功模型调用：预估输入合计 804,542，真实 prompt 合计 641,998；1 次预估低于实际，其后启用了校准。记录单次窗口、输出预留、Schema、观察、历史、剩余 Run token、真实 usage、估算差异和压缩前后字符数。最终代码还细分 evidence_tokens；此字段是运行后补充的统计，原 10 题日志不伪造该字段。

Checkpoint/收据保留完整可信工具结果，包括结果列表、证据和 scope 摘要。模型仅接收 allowlist 观察；压缩不修改 checkpoint 消息，而用 model_copy 构造独立视图。保持 Assistant ToolCall/ToolMessage 的次序与配对，保留当前问题、系统目标、最新观察及计算回执。旧长观察变为需重新读取的导航；不会把摘要注册为 Evidence。

模型本轮实际收到的证据决定 visible_evidence。被压缩掉的旧 Source 不再有引用或计算资格；再次使用必须重新读取并重校权限/版本。Source 编号从 checkpoint 中的映射恢复，不重编号为其他证据。可信证据档案上限与工具调用上限绑定，模型输出仍受单次预算限制。

## 长度策略与读取能力

| 类型 | 证据片段上限 | 模型观察上限 | 续读方式 |
|---|---:|---:|---|
| Search | 4,000 字符 | 16,000 字符，且受 Run output_chars 限制 | 文档/Chunk/Locator ID 读取 |
| find_document | 不凭候选生成原文证据 | 6,000 字符、首屏 20 候选 | document_start / next_document_start |
| 精读/章节 | 12,000 字符 | 24,000 字符 | 精确原文偏移或 next_after |
| read_document | 单片段 12,000，服务端单次最多 32,000 字符 | 24,000 字符 | next_start；目录首屏 12 项、outline_after |
| 表格 | 原文行组最多 12,000 字符，不任意截行 | 24,000 字符 | node_id + start/next_start；保留表头、单位、行列坐标 |
| Calculate | 无新原文证据 | 6,000 字符 | 结构化结果和可修正错误码 |

前缀证据重新解析精确范围，Locator.end-start 与内容长度对应并重新生成 hash/身份；不把 4K 前缀标为完整 Chunk。无法安全缩短的投影或不完整表格不注册引用，返回读取导航。完整表格与小表接口保留；大表只分页完整行组，跨行合并单元格不拆开，表头原文单独具有精确定位。未能确认的单位仍为空。

嵌套 HTML 表格、单个行组超过预算、表格 payload 超过 1MB 时安全拒绝；本轮未实现任意大型/嵌套表格的全功能分页，也未做大型表格性能验收。目录分页使用版本内稳定排序和位置游标，避免 Section/Table 相同 ordinal 导致漏项。

## 截断分类与逐工具统计

三个指标分别为：result_partial（存在后续结果页）、evidence_content_truncated（证据前缀/不完整表格延后读取）、observation_budget_truncated（模型观察预算或候选导航窗口限制）。最后一种包含候选首屏裁剪，不能全部叫作原文严重截断。

| 工具 | 调用数 | result_partial | evidence_content_truncated | observation_budget_truncated | 可继续读取 | 内部结果字符合计 → 模型观察 |
|---|---:|---:|---:|---:|---:|---:|
| v2.search_knowledge | 15 | 0 | 12 | 15 | 15 | 1,497,428 → 220,856 |
| v2.calculate | 18 | 0 | 0 | 0 | 0 | 9,173 → 4,403 |
| v2.read_document | 3 | 0 | 0 | 0 | 0 | 28,437 → 12,820 |
| v2.find_document | 1 | 0 | 0 | 1 | 1 | 20,344 → 3,516 |
| v2.read_document_section | 1 | 0 | 0 | 0 | 0 | 48,801 → 7,568 |

search_knowledge 的候选/目录元数据共 223,039 字符，其中跨观察重复元数据 21,564 字符；精简移除了 203,684 字符候选结构。内部结果字符包含完整 artifact 和重复表示，不等于旧版本模型输入，不能将内部/观察差额宣称为相同请求的实际 token 节省。

脱敏 JSON 按每个 tool_call_id 保存候选数、证据前后数量、字符前后数量、metadata/duplicate 字符、可续读标记和原因。普通指标无文档正文。原始私有结果单独保留在 data/，未推送。

## Calculate 诊断与修复

通过旧 checkpoint 中的真实调用参数、原文片段和基线 calculate 实现离线复现原 49 次调用：4 成功、19 evidence identity 缺失、21 operand_not_unique_supply_offsets、5 operand_source_mismatch。21 次“非唯一”实际上有 17 次零匹配、4 次多匹配，因此不能全部归因于数字重复。43 个操作数曾把 S 编号放入 evidence_id；一次调用有多个操作数，此数字不是调用失败数。

现在允许 source_id，兼容真实 evidence_id 和历史 evidence_id:S1 用法。先检查当前 Run 映射、模型可见性、知识库、权限、版本和内容哈希，再规范化为真实 evidence_id。审批绑定、ToolApprovalStore 消费、执行及收据幂等键使用同一份规范化参数。Source 冲突或隐藏来源直接拒绝。

数字必须逐字匹配原文：千分位仅在分组合法时归一化给 Decimal，Unicode 负号保留原文偏移；句尾标点不误认为小数，不能把 1,200 中的 200 当独立数字。重复数字仍需准确偏移；不推断缺失操作数或单位。错误 reason 和修正提示不携带私有原文。

本次 10 题计算调用 18 次、3 成功；失败为 operand_not_unique_supply_offsets 10、operand_source_mismatch 4、unit_not_unique_supply_offsets 1。未再出现 identity not_found，但数值位置和单位定位仍未解决。仅对这 10 题比较：基线 9 次调用/1 成功，优化后 18 次/3 成功；不能拿原全部 100 题的 49 次直接作为相同样本比较。

## 固定 10 题逐题对照

题干、目标文档及标准答案未提供给回答 Agent。基线直接读取原脱敏报告，未重新运行旧模型。下表时间均为 Agent elapsed_seconds，不包含旧版额外支持裁判。

| 题号 | stop_reason 前 → 后 | 引用校验前 → 后 | 来源命中前 → 后 | 实际 token 前 → 后 | 工具数前 → 后 | 计算成功前 → 后 | 压缩次数 | 秒前 → 后 |
|---|---|---|---|---:|---:|---:|---:|---:|
| tatqa_test_9064 | token_budget → answer | rejected → passed | 0 → 1 | 56,317 → 108,389 | 3 → 4 | 0 → 0 | 4 | 15.35 → 31.67 |
| tatqa_test_9082 | token_budget → answer | rejected → passed | 0 → 0 | 61,805 → 65,979 | 4 → 3 | 0 → 0 | 1 | 27.54 → 24.54 |
| tatqa_test_9102 | token_budget → answer | rejected → passed | 0 → 1 | 68,837 → 24,123 | 5 → 1 | 0 → 0 | 0 | 26.03 → 8.91 |
| tatqa_test_9130 | token_budget → consecutive_failures | rejected → rejected | 0 → 0 | 51,332 → 44,319 | 3 → 4 | 0 → 0 | 1 | 15.65 → 21.39 |
| tatqa_test_9144 | token_budget → answer | rejected → rejected | 0 → 0 | 63,992 → 104,176 | 4 → 5 | 0 → 1 | 6 | 17.83 → 24.75 |
| tatqa_test_9149 | token_budget → answer | rejected → passed | 0 → 1 | 77,265 → 38,331 | 5 → 2 | 1 → 1 | 0 | 34.80 → 14.40 |
| tatqa_test_9106 | token_budget → answer | rejected → passed | 0 → 1 | 70,069 → 78,349 | 4 → 5 | 0 → 1 | 0 | 21.27 → 36.54 |
| tatqa_test_9069 | consecutive_failures → answer | rejected → passed | 0 → 1 | 64,425 → 72,283 | 4 → 5 | 0 → 0 | 3 | 18.96 → 40.68 |
| tatqa_test_9111 | consecutive_failures → consecutive_failures | rejected → rejected | 0 → 0 | 64,367 → 90,121 | 4 → 7 | 0 → 0 | 4 | 16.52 → 28.08 |
| tatqa_test_9086 | answer → answer | rejected → rejected | 0 → 0 | 15,393 → 24,535 | 1 → 2 | 0 → 0 | 0 | 11.20 → 18.43 |

| 题号 | 原 output_truncated | 新 partial / evidence / observation 调用次数 |
|---|---|---|
| tatqa_test_9064 | True | 0 / 4 / 4 |
| tatqa_test_9082 | True | 0 / 3 / 3 |
| tatqa_test_9102 | True | 0 / 0 / 1 |
| tatqa_test_9130 | True | 0 / 1 / 1 |
| tatqa_test_9144 | True | 0 / 2 / 3 |
| tatqa_test_9149 | True | 0 / 1 / 1 |
| tatqa_test_9106 | False | 0 / 0 / 1 |
| tatqa_test_9069 | True | 0 / 1 / 1 |
| tatqa_test_9111 | True | 0 / 0 / 1 |
| tatqa_test_9086 | False | 0 / 0 / 0 |

恢复指标：6 题从无有效引用变为技术通过；其中 5 题命中标准来源。7 个原预算停止题均不再以 token_budget 停止，但 9130 改为连续计算失败，9144 被引用可见性校验拒绝，因此不能算这 7 题全部恢复成功。

### 引用与回答人工样例核对

- 9064：引用命中，但问题问百分比，回答只给英国资产 6.9 million，没有回答百分比；不算完整解决。
- 9082：引用通过但未命中目标文档，返回 165.0/134.3 million 等数值；不能宣称正确。
- 9069：回答 5.47 million，与目标表/参考值对应，但没有调用 calculate；仍存在模型心算绕过计算工具的问题。
- 9102：回答利息费用 814 million，与引用及参考值对应。
- 9106：Decimal 平均值 303,118，表头明确 thousands of USD；回答换算为 303,118,000 USD。算式及表头可核对，但单位换算由模型完成，不能称为工具验证了全部单位推理。
- 9149：85−80=5，引用的表说明 in millions，与回答增加 5 million 对应。
- 9144：计算成功，但最终引用 Source 已从当前模型视图移除，返回 source_not_model_visible。安全拒绝正常，Agent 没有完成必要的重新读取。
- 9130、9111：连续计算失败；9086：最终无有效引用。

没有做模型支持裁判；没有宣称 6/10 是答案准确率。以上核对只覆盖有限数值及来源，不替代独立人工全量语义验收。

## 可复现执行与验证

冻结目录：`data/citation-context-optimization-10-20261101/`；保留每题私有响应、数据库、manifest、代码文件哈希和配置。复用原 100 文档 SQLite 快照及文本哈希，仅重建隔离索引，未修改原始 100 题报告。

```bash
.runtime/bin/python -m scripts.evaluate_agent_citations \
  --output data/citation-context-optimization-10-20261101 \
  --frozen-from data/citation-quality-100-20261101-run1 \
  --workers 2 --isolated-executions \
  --question-ids tatqa_test_9064 tatqa_test_9082 tatqa_test_9102 \
    tatqa_test_9130 tatqa_test_9144 tatqa_test_9149 tatqa_test_9106 \
    tatqa_test_9069 tatqa_test_9111 tatqa_test_9086
.runtime/bin/python -m pytest -q
.runtime/bin/python -m pip check
cd frontend
npm run typecheck
npm run build
```

最终完整回归 **492 passed / 0 failed / 7 warnings，67.97 秒**；新增专项 19 项。pip check 无依赖冲突；TypeScript 与 Vite 构建通过，存在已有大于 500KB 的 bundle 提示。未修改原测试来改变通过率。

覆盖长精读、候选去重/分页、表格安全分页及精确偏移、真实 usage 累计、临界预算、Schema 预留、配对/原 checkpoint 不变、Source 隐藏/冲突/撤权、计算审批规范化、恢复幂等与禁用额外裁判。原有跨进程 checkpoint、旧 Agent、REST/V2 和权限回归继续通过。

**真实测试版本说明**：10 题运行期间没有调参、改运行代码或重新挑题。运行后进一步修复了目录 ordinal 重复分页、补候选分页参数、保留小表旧返回形状、加入超大表/部分投影保护和 evidence_tokens 指标。这些收尾修改仅通过本地自动化测试；新增 Schema 字段可能影响后续实际 token，不能把本次 10 题数据当作最终提交逐字节版本的重新验收。JSON 保存真实运行代码哈希及这些差异，遵守不追加真实重跑的要求。

## 剩余问题与停止点

1. 最终回答仍可能缺失问题要求的指标、错表或错来源；技术引用验证不验证语义蕴含。
2. 数字/单位定位仍失败，错误提示未能可靠引导模型纠正；模型可能自行心算或换算。
3. 压缩后的旧 Source 需要模型重新读取，当前模型有时仍引用旧编号而被拒绝。
4. 总体 Token 与耗时没有下降；启发式 tokenizer 有误差，未验证所有模型/服务商窗口。
5. 大型目录/表格新增分页有自动化覆盖，但这 10 题没有触发真实表格分页调用；超大或嵌套表格仍有限制。
6. 没有真实 Dify 向量/Hybrid 对照，没有生产共享 SQLite 并发或内网部署验收。

本次到此停止，不进入第五阶段、不迁移 MCP、不重跑 100 题、不扩大真实样本。
