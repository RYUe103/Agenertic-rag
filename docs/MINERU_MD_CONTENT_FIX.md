# MinerU `md_content` 适配与历史文件审计

版本：`2026.10.19-mineru-md-content.1`

## 内网覆盖更新

把 `agentic-rag-jit-mineru-md-content-update.tar.gz` 放到 `/data/yuyang/kb/v1/`，然后执行：

```bash
cd /data/yuyang/kb/v1/agentic-rag-jit
tar -czf ../kb-code-before-mineru-fix.tar.gz app static scripts docs README.md pyproject.toml
cd /data/yuyang/kb/v1
tar -xzf agentic-rag-jit-mineru-md-content-update.tar.gz
cd /data/yuyang/kb/v1/agentic-rag-jit
pkill -f 'uvicorn.*8765' || true
nohup ./run.sh > agentic-rag.log 2>&1 &
```

验证版本：

```bash
curl -sS http://127.0.0.1:8765/api/health
```

应包含 `"build_version":"2026.10.19-mineru-md-content.1"`。

当前内网 MinerU 返回顶层 `md_content`。内容可能是没有换行和 `#` 的单行HTML表格；旧客户端没有识别该字段，会将成功响应误判为空并静默回退本地解析。

本版本精确读取 `md_content`，并保存：

```text
files/src_xxx/
├── mineru-original.md
├── mineru-response.json
├── extracted.md
├── document.md
└── metadata.json
```

`metadata.json` 中的 `parser_engine` 为 `mineru`、`local`、`local_fallback` 或 `qwenvl`。资料库Markdown预览会显示实际引擎和回退错误。

默认仍允许MinerU失败后回退，避免批量任务因单份文件中断。需要严格检查时在 `.env` 设置：

```dotenv
MINERU_FALLBACK_ON_ERROR=false
```

修改后重启服务。严格模式下MinerU失败会将解析标记为失败，不会把本地结果冒充MinerU结果。

## 统计历史不一致文件

旧记录不足以离线判断，必须重新调用MinerU比较全文。先抽查最新20份：

```bash
cd /data/yuyang/kb/v1/agentic-rag-jit
./.runtime/bin/python scripts/audit_mineru.py --limit 20 --report mineru-audit.csv
```

确认服务压力可接受后检查全部：

```bash
./.runtime/bin/python scripts/audit_mineru.py \
  --limit 0 \
  --delay 0.5 \
  --report mineru-audit-all.csv
```

中断后续跑：

```bash
./.runtime/bin/python scripts/audit_mineru.py \
  --limit 0 \
  --delay 0.5 \
  --report mineru-audit-all.csv \
  --resume
```

状态说明：

- `same`：现有 `extracted.md` 与当前MinerU `md_content` 全文一致。
- `different`：不一致，需要判断是否重新解析。
- `error`：MinerU调用或响应异常。
- `missing`：原PDF或 `extracted.md` 缺失。

CSV只记录摘要、长度和SHA-256，不覆盖任何现有文档。确认某份需要修复后，可在资料库点击“重新解析”；它会按原流程继续Workflow/AI改写并刷新Dify。
