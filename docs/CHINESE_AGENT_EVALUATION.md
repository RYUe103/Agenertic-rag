# 中文多文件知识库端到端评测

日期：2026-11-01。分支：`codex/v2-document-repository`。开始时本地 HEAD 和远端均为 `1b9309af3da2fcec882a8c228dbedf45ac1c5adf`，工作区干净。

**交付的是评测系统、冻结50题语料和5题先导运行的可核验结果，不是“中文问答准确率100%”的验收结论。** 最终严格评分为1题correct、4题not_assessed；后4题是裁判引文验证失败，不能算作Agent答错，也不能算作答对。

## 数据审计

实际下载/读取的版本、文件大小、字段、缺失答案数量见 [CHINESE_DATASET_AUDIT.json](CHINESE_DATASET_AUDIT.json)。

| 来源 | 实际审计内容 | 是否用于本轮问答 |
|---|---|---|
| RGB | 固定commit `65ec39e40e7dc9abb50e9bf1b4f32be3f6f16615`。`zh_refine.json` 300条/3,893,494字节，`zh_int.json` 100条/3,024,012字节，`zh_fact.json` 100条/320,481字节。后缀虽为json，实际是JSONL。 | 是 |
| CRUD-RAG | 固定commit `1aace383994e1f68efa12cf2a8e2dadfb4102ceb` 的 `data/crud_split/split_merged.json`，26,866,460字节。单文档800条、双文档797条、三文档797条；这些记录均有非空questions/answers，分别有news1、news2、news3；另有event_summary 2000、continuing_writing 1999、hallu_modified 1268。 | 只审计，未跑其语料 |
| T2Retrieval | 固定revision `921dd3af6e78d1ae7ee0368aa8d7eaee02c8f08e`，只读取数据卡/API。dev含118,605篇corpus、22,812条queries、118,932条qrels。字段分别为`_id,text,title`、`_id,text`、`query-id,corpus-id,score`。 | 未下载Parquet；无答案标注，不能测答案准确率 |

来源：RGB `https://github.com/chen700564/RGB`；CRUD-RAG `https://github.com/IAAR-Shanghai/CRUD_RAG`；T2Retrieval `https://huggingface.co/datasets/mteb/T2Retrieval`。

RGB声明CC BY-NC-SA 4.0，数据包附完整许可证及归属说明；其非商业、署名和相同方式共享条件随派生数据保留。CRUD README展示Apache-2.0标识，但GitHub license元数据为空，新闻原文的独立权利没有进一步核实，本轮不再分发其新闻。T2数据卡声明Apache-2.0。

RGB `evalue.py` 的实际语义：答案外层列表是必答项，内层列表是同项别名；`zh_int` 的positive是证据组的嵌套列表；`positive_wrong`与fakeanswer是反事实材料，negative是干扰标注而非“内容必假”。拒答协议使用noise_rate=1向单题仅提供negative，不是独立的原生无答案标签。把全部材料汇入共享知识库后，不能沿用该条件擅自把问题标成无答案。

CRUD确实存在可直接抽取的双/三文档问答和参考答案，不再停留于论文介绍；本轮选择RGB先完成可复现适配，没有下载8万篇新闻或模型权重。RGB初次获取曾使用约12MB仓库归档；最终构建器只获取3份固定版本中文文件、许可证和协议文件。

## 现有链路审计与复用

审查了Agent runtime/api/evidence/citations/context/state/checkpoint、检索service/tools/fusion/models、旧引用评测脚本及相关测试。最终没有修改任何 `app/` 文件。

| 现有部分 | 本轮使用方式与边界 |
|---|---|
| SourceRepository / DocumentRepository | 新建隔离sources.db，原始TXT写入版本；平台构建Block/Chunk，保存内容哈希与version_id。只载入语料文件，不载入questions.jsonl或标签。 |
| IndexManager / UnifiedRetrievalService | 构建真实Tantivy索引，保留generation与manifest哈希。Agent工具仍自行选择auto检索；独立retrieval模式显式测试lexical。隔离环境不配置Dify，因此没有真实向量或Hybrid闭环声明。 |
| ReActRuntime / ToolService | 原有LangGraph/ToolNode、严格权限和引用验证、原预算。每题新execution目录，整个运行共用同一KB与全量文件。 |
| Checkpoint与ToolMessage | 从真实checkpoint提取调用参数、顺序、原始排名、登记证据、错误和最终visible_evidence。最终引用不是检索命中的替代指标。 |
| EvidenceRegistry.load | 事后重新读取引用与最终可见证据，复核KB、版本、Locator、hash及可见身份；失败即不给裁判提供该段原文。 |
| Provider usage | 分别保存prompt/completion/total及模型调用数；估算或缺失usage不写成真实0成本。 |
| 旧evaluate_agent_citations | 复用其隔离运行思路和相同核心服务，未复制财报选择/数值答案评分逻辑，也未改旧脚本。 |

## 两个分开的语料快照

### 最终50题包

[benchmarks/chinese_rgb50](../benchmarks/chinese_rgb50)：20条refine事实/干扰题、15条information integration、15条counterfactual题，保持原题原答案与原始ID。共1,452个独立TXT文件，约2.1MB（含标注及manifest）。每个文件是一条完整原始段落，同文本按SHA去重，文件名只含内容哈希。

`passage_mapping.json`逐项记录原文件、原记录ID、positive/negative/positive_wrong字段、嵌套index_path、物理文件ID、字符范围与哈希。gold证据保留组号；嵌套列表不会转成Python字符串。参考答案和标签只在旁路标注中，文件名、文档元数据不泄漏答案或题号。

每题gold文档可能是可替代来源集合，不要求召回每个重复来源才能回答；Recall分母是已有的正例标注，不能解释成问答完整率。跨问题的潜在相关性不完备。整合题存在单篇已包含多项答案的情况，不能仅凭任务标签宣称Agent必须跨文件推理。

保留的缺口：没有原生7道拒答标注；不人为把事实题细分成8道专业术语或5道语义改写，也不生成问题补足建议分布。文件格式本轮是TXT，不是新PDF/DOCX/MinerU解析验收。

### 5题先导运行快照

[benchmarks/chinese_rgb_pilot](../benchmarks/chinese_rgb_pilot)：保留初版10个汇编文件及1,482条来源范围，供核查已发生的5次真实调用。它**不是最终1,452文件基线**。

初版构建器把整合题嵌套列表转为Python列表表示；5道运行题本身都是refine事实题，但同一文件库中存在这种不理想的干扰文本。随后修正构建器，以独立原始段落建立最终50题包，没有重跑这5题。初版使用题号作为conversation元数据（不进入模型问题）；最终入口改为随机conversation，题号仅作评测文件名。

先导Agent核心代码为上述`1b9309a`，生成器当时是未提交脚本，没有在首次请求前记录脚本内容哈希。因此报告明确此限制，不假称先导调用具备最终runner同等的执行代码可复现性。最终runner冻结可执行源码内容哈希、Git SHA、模型/接口/端点哈希、预算、语料、问题与索引版本；执行途中任何源码或配置变化都会拒绝混跑。单独文档提交不改变可执行源码哈希。

## CLI与执行保护

入口：`python -m scripts.evaluate_chinese_agent`，支持 `prepare / retrieval / agent / judge / report`，dataset当前为rgb。

```bash
# 重建官方固定版本50题包（已随仓库提供，不必重新下载）
.runtime/bin/python -m scripts.build_chinese_eval \
  --rgb-dir data/eval/rgb-pinned --download --output data/eval/rebuilt-rgb50

# 只入库/建索引，不运行Agent
.runtime/bin/python -m scripts.evaluate_chinese_agent --mode prepare \
  --pack benchmarks/chinese_rgb50 --output data/eval/chinese-next

# 只检查检索，不计入答案正确率
.runtime/bin/python -m scripts.evaluate_chinese_agent --mode retrieval \
  --pack benchmarks/chinese_rgb50 --output data/eval/chinese-next --limit 5

# 真实Agent，默认5题；本轮没有再次执行此命令
.runtime/bin/python -m scripts.evaluate_chinese_agent --mode agent \
  --pack benchmarks/chinese_rgb50 --output data/eval/chinese-next --limit 5

# 事后评分；不传--model-judge时只运行保守确定性规则
.runtime/bin/python -m scripts.evaluate_chinese_agent --mode judge \
  --output data/eval/chinese-next --limit 5 --model-judge

# 离线重新统计已有结果：不调用模型、不重放Agent
.runtime/bin/python -m scripts.evaluate_chinese_agent --mode report \
  --output data/eval/chinese-next --limit 5
```

整套50题命令已准备，但**未执行**；需在独立output使用`--mode agent --limit 50`，不能在已冻结5题选择的目录中改为50题混跑。

执行前写attempt journal、采用OS锁；已完成结果直接复用，中断后只继续未开始题。已有started但缺少最终结果的调用标记不确定，保持checkpoint供诊断，不自动重新发请求，不伪装成checkpoint恢复。已保存的裁判结果复用，裁判不确定调用同样不自动重试。单题工具仍由Agent决定，runtime只接收原问题、随机conversation与固定KB scope，不接收gold文件/答案/题号。

## 评分与失败归因

确定性规则只对**整个短回答与某个标注别名完全等价**的情形给correct；引用序号可以剥离。`170`不会匹配`70`，否定句、附加断言、多项长答不因包含字符串而给正确，转为not_assessed等待事后评审。无答案标签不明确时不猜拒答分数。

事后裁判读取原问题、原始reference、原文gold范围、实际回答，以及通过Repository重新校验的实际引用。答案评价与逐claim引用支持分开；要求准确引文、合法来源ID、每个claim完整覆盖。当前可用裁判与Agent均为qwen-plus，是独立调用而非独立模型，存在同模型偏差。没有真人评审；报告的静态核查由Codex完成，不冒充人工gold。

检索记录每次工具返回的排名以及Hit@5/10、Recall@5/10、MRR。区分文档命中、正例段落范围重叠、最终模型可见证据和最终引用。公开RGB没有最小答案span标注，因此“Gold Evidence”实际为**正例段落范围**，不宣称答案span召回。

失败归因使用trace、错误码、实际可见证据和有引文的语义裁判：retrieval_miss、evidence_not_visible、semantic_misinterpretation、incomplete_answer、unsupported_answer、tool_error、budget_exhausted、incorrect_abstention等。wrong_document、distractor_misled、hallucination需要具备可复核依据的语义评审，不能只看最终答案猜测。精确重复请求标记为疑似unnecessary_tool_loop；是否必要仍需上下文判断。未确认的原因保留unknown。不能把评分器失败记成Agent错误。

## 真实先导结果：5次Agent + 5次事后裁判

完整机器报告：[CHINESE_AGENT_EVALUATION.json](CHINESE_AGENT_EVALUATION.json)。裁判原始输出及失败：[CHINESE_AGENT_JUDGMENTS.json](CHINESE_AGENT_JUDGMENTS.json)。每题均通过真实ReAct自主调用`v2.search_knowledge`；实际请求未强制指定mode或正确文件。没有运行50题，也没有重跑成功或失败样本。

| 指标 | 结果与解释 |
|---|---|
| 严格答案评级 | correct 1、not_assessed 4；不是1对4错，更不是5/5正确 |
| 已评审正确率 | 1/1，只覆盖20%样本，不能作为总体准确率 |
| 引用技术校验 | 5/5通过 |
| 逐claim语义支持 | 1题通过、4题未获得有效裁判结果 |
| 最终引用命中gold文档 | 5/5（初版汇编语料粒度较粗） |
| 首次检索文档Hit@5 / MRR | 1.0 / 1.0 |
| 首次检索正例段落Recall@5 / @10 | 0.53 / 0.75 |
| Agent调用与工具 | 15次实际模型调用，5次工具调用，均一次搜索后回答/引用修复 |
| Agent Provider Tokens | 126,051；平均25,210.2/题，prompt/completion逐题保存 |
| Agent耗时 | 累计56.50秒，平均11.30秒/题 |
| 上下文压缩 | 0 |
| 裁判成本 | 5次调用，无重试，10,748 tokens，累计76.26秒；与Agent成本分列 |
| 总实际tokens | 136,799（Agent + 事后裁判） |

首次裁判提示的来源ID示例采用占位写法，4份输出出现`gold:<0:0>`、数组作为quote或不连续/不精确引文，被验证器拒绝。不是没有参考答案，也不是Agent运行失败。最终提示已改为显式列举允许的source值及字符串引文要求；本轮不为提高通过数再次调用裁判。现有失败原样保留，尚未证明修改提示后真实裁判通过率提高。

初版字符串评分曾把5题都标correct，且最初从citation_metadata错误取document_id导致命中0；这些错误已修正并在独立离线审计报告中作废，原始生成内容和checkpoint保留。没有把事后评分修正包装成重新运行Agent。

### 逐题记录与静态核查

下表的静态核查不改变机器评分，也不等同真人复核。

| ID | 原题 / 参考答案 | Agent答案概述 | 机器评级 / 静态核查 | Tokens / 秒 |
|---|---|---|---|---|
| 0001 | 印控克什米尔地区寺庙踩踏事故死亡人数 / 12人 | 12人死亡，附今年1月、地点和14人受伤 | not_assessed（引文失败）；核心死亡数字与原文相符，“今年”缺少年份，不可解释为2026年 | 24,584 / 12.19 |
| 0002 | RCEP具体包括哪些国家 / 东盟、中国、日本、韩国、澳大利亚、新西兰 | 东盟十国逐个列名加中日韩澳新，共15国 | not_assessed（引文失败）；六组参考要求已答，但引用没有逐一列出十国名称。裁判要求额外解释印度退出超出题意，不据此判错 | 25,032 / 10.60 |
| 0003 | 第七届香港立法会议员宣誓仪式的时间 / 1月3日 | 2022年1月3日上午11时，重复三种表述 | not_assessed（来源标签失败）；实际引用含该时间，存在答案冗余 | 25,673 / 13.09 |
| 0004 | 首趟RCEP班列的起点？ / 南宁 | 南宁国际铁路港 | not_assessed（引文失败）；实际引用写首趟班列从该铁路港发出，与参考对应 | 24,858 / 10.06 |
| 0005 | 香港第七届立法会选举有多少个议席？ / 90 | 90个议席 | correct，引用supported；原文明确90个议席全部产生 | 25,904 / 10.56 |

5题stop_reason均为answer；最终可见正确段落数量依次4、2、4、3、2。没有仅因最终引用命中就把未引用候选视为可见。全部逐题原答案、逐claim引文、工具调用参数、原始排名、失败和费用可从JSON检查。

## 最终50题包的本地检查（不是Agent问答成绩）

已真实创建 `data/eval/chinese-frozen50-ready`，完成1,452文件的版本化入库、Chunk构建、Tantivy索引与哈希冻结。对前5个原问题独立运行lexical检索，**0次模型调用**：文档Hit@5为5/5，MRR均值0.8667，Recall@5为0.4447、Recall@10为0.8236。每文件即原始段落，故此包的文档与段落指标重合。

结果见 [CHINESE_RETRIEVAL_SMOKE.json](CHINESE_RETRIEVAL_SMOKE.json)。它与10文件先导运行分开，不用两者的指标比较推断答案质量改善。最终1,452文件基线还没有真实Agent运行；50题已准备就绪。

## 测试、代码与兼容性

实际运行：

```bash
.runtime/bin/python -m pytest -q tests/test_chinese_evaluation.py
# 29 passed, 5 warnings, 1.90s
.runtime/bin/python -m pytest -q
# 552 passed, 7 warnings, 95.49s
.runtime/bin/python -m pip check
# No broken requirements found
```

新增29项测试覆盖嵌套来源与别名、原文CR/LF保持、文件哈希与篡改检测、来源范围、反事实标签、答案隔离、完整短答与否定边界、引用伪造/跨来源/漏claim拒绝、实际LangGraph checkpoint工具轨迹、重复执行/不确定中断保护、选择锁、配置与版本变化、离线报告无模型调用、真实usage与未评审分母隔离。旧523项回归保留，没有修改Agent或旧测试期望值。

核心新增文件：

- `scripts/build_chinese_eval.py`：固定版本下载、逐段构建、去重与完整来源关系。
- `scripts/evaluate_chinese_agent.py`：隔离Repository/索引、5种模式、冻结校验、执行保护、答案与证据事后报告。
- `scripts/chinese_eval/common.py`：哈希、原子文件写入、锁与数据包核验。
- `scripts/chinese_eval/scoring.py`：严格确定性评分、裁判引文验证和可归因错误分类。
- `scripts/chinese_eval/trace.py`：从真实checkpoint提取调用与可见性、独立检索指标。
- `scripts/import_chinese_pilot.py`：只读导入旧5题生成结果，来源重建及离线审计；不重放Agent。
- `tests/test_chinese_evaluation.py`，两个benchmark快照及本报告的机器JSON。

不增加依赖，不改Agent核心、REST/MCP、前端或MinerU/Dify；没有修改正式业务数据库。报告发布的是公开RGB问题/材料与脱敏执行指标，不提交.env、令牌、业务私有文档、SQLite/Checkpoint数据库。

## 尚未验证与下一步最小事项

1. 新版1,452文件语料的真实Agent答案质量尚未验收；50题仅准备就绪。
2. 4份裁判结果未获有效引文，提示修正只有确定性契约测试，没有追加真实调用验证。需独立审查或人工评审后才可填补评分，不应直接采纳原始correct/partial标签。
3. 暂无真人复核；同模型裁判还出现题外背景要求，应在独立审查中重点检查。
4. 真实先导5题均为事实题，未验证跨文档综合、反事实抗干扰、拒答和复杂中文表达。拒答需构建可证明在整个共享语料中无答案的标注；不能仅去掉单题positive。
5. 没有真实Vector/Dify、PDF/DOCX解析或生产部署验收；CRUD/T2只完成数据审计。
6. SSE前端和并行吞吐没有修改或专项评测；执行不确定时保留状态供审查，不提供自动重放掩盖中断。

实现/测试/数据包提交：`3bed999b9f3703ce9814bcafcd1f329890ef6238`。报告单独提交，其SHA以本文件Git历史和最终回复为准；交付时通过`git ls-remote`核对远端与本地HEAD一致。公开语料保留原始空白与CR/LF，`.gitattributes`只对语料路径禁止行尾转换和无意义的空白告警。至此停止，等待独立代码审查。
