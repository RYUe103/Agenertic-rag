# 原始 10 道 Top-10 未命中题逐题检查

基线固定为 `LOCAL_SEARCH_BENCHMARK_2026-10-31.json` 的原 100 个 PDF SHA256 和原 100 个 question_id，没有重选样本、替换答案或注入公司名。输入仍为 PyMuPDF 提取文本，不是 MinerU 服务验收。复测只改变平台实现和有版本号的 Chunk Builder；分词器、字段权重未调参。

下文“排名”是正确来源 PDF 首次出现在 **Chunk 排名** 的位置，不是去重文档排名，更不是答案或证据命中率。v2 诊断 Top-100 记录来自修复尾块前的隔离复跑；v3 使用相同文本重新构建。大于 100 的情况只写“Top-100 未见”，不猜实际排名。

所有 10 题的正确 PDF 都在原始 SHA256 清单中、解析成功且实际写入 Tantivy。限制为目标 PDF 的检索均有结果，未发现漏入库或完全漏索引。这 10 题的提取文本均未形成结构化 Table 节点：财务表格被 PyMuPDF 展平为行文本，行列语义和表头绑定不可可靠恢复。数字还在不等于表格证据已经正确解析。

原结构 v2 在段落结束后继续 overlap 回退，生成多个包含相同尾部的递减 Chunk。v3 停止这种尾部重发；历史 v2 原文、Chunk 和算法均保留。所有小 Block 的原文仍由 Chunk 覆盖；下面另行指出证据跨块，不将“文字未丢失”误当作“上下文组织合理”。

## 1. tatqa_test_9073

问题：What was the percentage point increase in the 'Others' category of UMC's wafer sales from 2017 to 2018?

来源：`03a98443a7286862ffe1b6bcc2193e54.pdf`。标准答案 1 个百分点；依据 Others 的 8.9（2017）和 9.9（2018）。

- 8.9、9.9、Others、2017、2018 均在提取文本及原 v2 Chunk 3 中。没有发现关键数字缺失。
- v2 共 32 块，多个短尾块重复 Others/数值；v3 为 4 块。来源排名从诊断第 11 升至第 3。
- `percentage / point / increase / category` 不在原文分词集合；需要比较两个年度值，BM25 不表达运算关系。
- 归因：尾块冗余及排序是已观察到的因素；表格结构展平仍存在。修复后来源进入 Top-10，但未验证计算答案正确。

## 2. tatqa_test_9082

问题：What was the amount of net operating loss carryforwards as of December 31, 2019, for aci-worldwide-inc?

来源：`0551beccb943904e051f6f2a1519bd3a.pdf`。答案 23,030（表格单位为 thousands）。

- “Net operating loss carryforwards”、2019 和 23,030 都在原 v2 Chunk 0；该 PDF 的 4 个 Chunk 在 v3 保持 4 个。
- 关键词 aci、worldwide、inc 不在该页文本，文件名也是数据集 ID，无法通过公司名区分多个财务 PDF。
- 来源诊断排名第 12 → 第 7。该文档自身分块数没有减少，排序变化与全语料去除重复尾块后 BM25 统计变化一致，不能说是该文档自身证据被补回。
- 归因：公司身份信息不足、跨财报通用词竞争及排序；数字可读，结构化表头/单位关联仍未验收。未发现特定技术标识分词错误。

## 3. tatqa_test_9083

问题：What was the value of Net operating loss carryforwards for aci-worldwide-inc in the financial year 2018?

来源：同上，答案 25,745（thousands）。

- 25,745、2018 和指标名称均在 Chunk 0；索引中有该段内容。无关键数字丢失证据。
- aci-worldwide-inc 的公司身份同样缺失，value/year 等词不能唯一定位该表。
- 来源诊断排名第 22 → 第 13，仍未进入 Top-10。
- 归因：排序和查询中公司信息与页面文本不对齐；向量/查询规划可能提供补充，但未经实验不能保证改善。表格仍展平。

## 4. tatqa_test_9084

问题：What was the change in tax credits between 2018 and 2019 for aci-worldwide-inc?

来源：同上，答案 9,064，依据 52,902 − 43,838。

- 两个操作数都在原 Chunk 0，未发现丢失；答案是派生差值，不要求原文出现 9,064 字面值。
- 限定目标 PDF 检索时，原最高块是讨论 NOL/foreign tax credit 的 Chunk 2，而不是目标表格 Chunk 0，说明来源排名还掩盖了证据排序问题。
- 来源诊断排名第 21 → 第 17。
- 归因：通用 tax credits 词匹配、公司身份缺失、运算型问题及证据排序；不是索引遗漏。未来检索和证据验证仍需处理。

## 5. tatqa_test_9085

问题：What was the change in compensation between 2018 and 2019 for aci-worldwide-inc?

来源：同上，答案 2,857，依据 18,791 − 15,934。

- Compensation 与两个操作数在原 Chunk 0。目标 PDF 限定检索的原最高块为 Chunk 3，而非这段表格。
- 原来源诊断第 85；v3 Top-100 未见。不能用其他问题的提升掩盖本题退化。
- 公司名缺失、compensation 只在表中短暂出现，多个其他财报竞争；原文没有完整问句或差值措辞。
- 归因：排序/查询语义不对齐；无关键数值丢失或漏索引证据。不能仅凭日志量化每个排名因素的贡献。

## 6. tatqa_test_9097

问题：What is the increase or decrease in Consolidated Communications Holdings Inc.'s Net income (loss) from 2018 to 2019?

来源：`06f4135ecdf57596ac473ab26bbe4b21.pdf`。答案 30.5，依据 −20.0 − (−50.5)，单位 millions。

- (20.0)、(50.5) 和 Net income (loss) 位于原 Chunk 0；括号负数形式保留。
- 完整公司名称不在页面文本，`increase/decrease` 也不是原文表格标签；这是一道数值差分题。
- 共 2 个 Chunk，来源诊断第 38 → 第 46，仍失败。
- 归因：身份/语义缺口、通用财务词排序；未观察到该题证据被 Chunk 边界拆开。表格列关系未结构化。

## 7. tatqa_test_9098

问题：What is the increase/decrease in Interest expense, net of interest income for Consolidated Communications Holdings, Inc. from 2018 to 2019?

来源：同上，答案 2.2，依据 136.7 − 134.5，单位 millions。

- 两个操作数位于原 Chunk 1；前面的 EBITDA 定义也含 interest expense，因此原限定目标 PDF 的最高块是 Chunk 0。
- 原文数值完整，但财务表跨块且没有结构化表头关联。只返回最高块可能遗漏目标证据。
- 来源诊断第 27 → 第 35。
- 归因：证据分布/表格结构损失、排序和公司名缺失共同存在。后续可利用相邻 Chunk、表格读取及检索融合；本次未提前实现这些 Agent 流程。

## 8. tatqa_test_9119

问题：What is the difference in revenue between 2018 and 2017 for Micron Technology Inc. as reported in their 2019 financial data?

来源：`0ad31a3d0f7aff9ffd4d3a6ebbcb40f1.pdf`。答案 10,069，依据 30,391 − 20,322。

- Revenue、2018/2017、30,391/20,322 都存在，操作数在原 Chunk 1。Micron 一词有出现，但完整公司名及 difference 措辞不完整。
- Chunk 数 7 → 3，来源诊断第 43 → 第 15，仍未进入 Top-10。
- 归因：尾块冗余已修复，通用财报排序及运算语义差距仍在；没有发现操作数分割丢失。需要区分来源与证据/计算验收。

## 9. tatqa_test_9144

问题：What is the sum of total revenue for the years 2017 and 2018 as reported by Avalara in their 2019 financial report?

来源：`0e9c79da0db8e0e9f5e58f9c2bdf6bb8.pdf`。答案 485,257，依据 272,098 + 213,159。

- 两个数值在原 Chunk 2；原文使用 NBSP（不换行空格），所以直接查普通空格的 “Total revenue” 会误以为标签缺失。现有 NFKC 分词能处理该空格，Exact 保留原文差异。
- Avalara 不在页内文本；表格可见数值仍未结构化。sum 属派生运算，不能要求答案字面存在。
- 3 个 Chunk，来源诊断第 16 → 第 25。
- 归因：排序、公司名缺失和运算语义；未观察到 NBSP 导致 BM25 token 丢失。避免把简单 substring 阴性当作解析失败。

## 10. tatqa_test_9157

问题：What is the percentage change in Roper Technologies' total long-term debt from 2018 to 2019?

来源：`1291daaa275e574a7fd08db3068b388a.pdf`。答案 −5.41%，依据 (4,673.1 − 4,940.2) / 4,940.2。

- 关键操作数及 Long-term debt 标签在提取文本尾部，未丢失；原 29 块中多块重复整个尾部，v3 为 10 块。
- 来源诊断第 14 → 第 15；公司名存在，不能沿用前几题“公司名完全缺失”的归因。
- 数据集 gold_context 将 4,673.1/4,940.2 标为 Total debt，而提取文本将它们标为 Long-term debt；提取文本另外有 Total debt 5,275.3/4,941.7。gold_context 还含 /130.0、/540.0 等与提取文本不同的单元格。这些差异未用来更改标签或标准答案。
- 归因：长文中多个 debt/tax/年份段落竞争、表格结构损失和派生运算；分词能识别 Roper/long-term/debt。gold_context 局部差异的最终责任需对源 PDF 图像人工复核，不能仅据文本断言是解析器或标注的单方错误。

## 结论的适用范围

10 题都存在可检索来源和关键操作数，没有“正确 PDF 没入库”的证据。已确认的问题是重复尾块、表格结构展平、公司身份在页面中缺失、以及词面/证据排序不足。v3 修复后这 10 题中 2 题进入 Top-10，8 题仍失败；部分题排名下降。向量检索、重排、计算和证据验证仍属后续阶段，本文不把它们作为已实现或保证奏效的补救。

机器可核对的原排名、新 Top-10 排名、来源 ID、SHA256 和性能记录见 `SEARCH_HARDENING_REAL_2026-11-01.json`。原始标注仍位于本地 `data/t2-ragbench/qa/data/TAT-DQA/test/metadata.jsonl`，详细诊断材料位于独立 benchmark 输出目录的 `misses.json`。
