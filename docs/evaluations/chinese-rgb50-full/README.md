# RGB50完整运行归档

主报告：`../../CHINESE_AGENT_50_RESULTS.md`；机器可读报告：`../../CHINESE_AGENT_50_RESULTS.json`。

- `manifest.json`：运行前冻结的代码、配置、数据版本及索引身份，复制原件。
- `selection.json`：完整50题选择，复制原件。
- `cases/`：运行结果原件，包含最终输出、工具轨迹、实际候选、事件和Provider Usage。
- `attempts/`：已完成的执行journal原件及对应case哈希；不自动重放。
- `messages/`：遍历全部LangGraph检查点，按首次出现顺序保存去重的消息版本。包含模型工具选择、校验前草稿、ToolMessage原文/产物和安全最终消息。不是原始HTTP抓包，也不是新的模型调用。
- `codex_review.json`：Codex阅读问题、参考、回答和证据后的逐题结论，不是模型裁判API产物。
- `audit.json`：文件哈希、Python/包版本、运行窗口、校验结果及测试命令。
- `verify_delivery.py`：离线完整性校验，无模型请求，不生成答案或替代逐题判卷。

在仓库根目录执行：

```bash
.runtime/bin/python docs/evaluations/chinese-rgb50-full/verify_delivery.py
```

SQLite执行/检查点库保留在本地`data/eval/chinese-rgb50-full`，不推送。公共语料本体已经位于`benchmarks/chinese_rgb50/files`；许可证与来源见该目录的`ATTRIBUTION.md`和`LICENSE.txt`。所有内容对应同一冻结运行，没有事后改写原始答案。
