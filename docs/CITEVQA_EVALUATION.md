# CiteVQA 评测

`scripts/evaluate_citevqa.py` 使用本地已经入库并由 MinerU 解析的 CiteVQA PDF，通过真实 Agent API 逐题请求回答。它不会下载受限 PDF，也不会生成模拟问题或结果。

准备工作：

1. 从 CiteVQA 官方数据发布页申请并下载标注和 PDF。
2. 将 PDF 上传到一个独立知识库，并等待 MinerU 解析、Dify 索引完成。
3. 确认标注中的文件名与平台文档文件名可对应。

运行示例：

```bash
.runtime/bin/python scripts/evaluate_citevqa.py \
  --annotations /path/to/CiteVQA.json \
  --endpoint http://127.0.0.1:8765/api/agents/@kb:citevqa/ask \
  --bearer "$EXTERNAL_API_KEY" \
  --limit 50 \
  --output data/citevqa/results.jsonl
```

输出包括逐题答案、答案字符集合 F1、文档召回率、页码召回率、耗时和 `run_id`。由于当前平台还没有保存 MinerU 元素级 bounding box，结果用于回答、文档和页码验收，不能直接宣称通过 CiteVQA 的完整元素级 SAA 指标。
