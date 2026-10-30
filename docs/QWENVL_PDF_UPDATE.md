# PDF 视觉解析、Markdown 预览与资料库分页

版本：`2026.10.19-qwenvl-pdf-preview.1`

## 内网更新

此包是现有安装的代码更新包，不需要重新安装 Python 或下载依赖。继续使用已有 `.runtime`。
包内不包含 `.env`、资料数据、配置数据库和密钥，覆盖代码后这些内容保持原样。

将 `agentic-rag-jit-qwenvl-pdf-preview-update.tar.gz` 放在 `/data/yuyang/kb/v1/`，在运行服务的终端按 Ctrl+C，或用部署时的进程管理器停止服务。随后执行：

```bash
cd /data/yuyang/kb/v1/agentic-rag-jit
tar -czf ../kb-code-before-qwenvl.tar.gz app static prompts README.md pyproject.toml docs
cd /data/yuyang/kb/v1
tar -xzf agentic-rag-jit-qwenvl-pdf-preview-update.tar.gz
cd /data/yuyang/kb/v1/agentic-rag-jit
./run.sh
```

在另一个终端检查：

```bash
curl -sS http://127.0.0.1:8765/api/health
```

应包含 `"build_version":"2026.10.19-qwenvl-pdf-preview.1"`。访问原有 `/kb/` 地址并强制刷新。
首次启动会自动为配置数据库增加字段，旧知识库默认沿用当前解析方式。

## 配置入口

知识库连接 → 选择知识库 → PDF 解析：

- 沿用系统默认：保持原有全局 MinerU / 本地解析行为。
- 本地文本 / OCR：使用现有本地解析器和已配置 OCR。
- MinerU：使用现有环境中的 `MINERU_URL` 等配置。
- Qwen-VL：为每个知识库填写兼容接口 Base URL、API Key 和视觉模型名。

例如百炼兼容 Base URL 可填写 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
内网模型填写自己的兼容接口前缀；仅追加 `/chat/completions`，不会补 `/v1`。
密钥留空表示保留当前值，不回显到浏览器。内网无法连接公网时必须填写内网可达模型服务。

默认规则：不超过 10 页一次提交全部图片，超过 10 页每 3 页顺序处理，DPI 为 200。
两种提示词可分别编辑，长文档提示词支持 `{{page_start}}`、`{{page_end}}`、`{{page_total}}`。
长文档从上一批末尾携带最多 2000 字符辅助衔接，只是上下文参考，并非全文记忆。
表格跨批次的合并仍需通过实际文件检验；可通过调整批大小和提示词改善。

Qwen-VL 输出直接保存为 `extracted.md` 和 `document.md`，即便勾选 AI 改写也不会再调用改写模型。
如果明确勾选 Dify Workflow，则 Workflow 仍会处理视觉输出。Dify 接收最终 Markdown，源文件平台接收原文件。
新设置影响后续解析，不自动重做已成功入库的文件。

每份文件目录会保存渲染图、分批 Markdown 和 `metadata.json` 中的解析记录。
失败错误会指出批次和页范围，已完成的批次文件可用于排查；重试会重新解析，不保证跳过成功批次。
模型输出被服务端长度限制截断时会报错，请减小批大小或调整模型服务输出上限。
渲染图会占用额外磁盘空间。

## 资料库

资料库现在走后端分页，每页 15、30、50 或 100 份，支持文件名搜索。
勾选框只选择当前页；未选择文件时，批量抽取仍沿用原来的后端批处理规则。
点击每行 Markdown 查看最终、原始、改写预览、Workflow 或视觉分批版本。
支持渲染与源码切换；渲染隐藏定位注释，源码保留原始内容。
异常摘要只统计空内容、`[无法辨认]` 和替换字符，不代表 AI 内容正确性审核。

旧 `/sources` 接口继续保留。新增接口为：

```text
GET /api/agents/{agent_id}/sources/page?page=1&page_size=30&query=关键词
GET /api/agents/{agent_id}/sources/{source_id}/markdown?version=document
```

## 验证范围

已验证本地 PDF 渲染、短/长文档分批、提示词选择、配置存储与密钥隐藏、分页及预览接口。
使用模拟模型验证视觉调用流程，未在本次运行中调用真实 Qwen-VL 服务。
部署后请先用一份短 PDF 和一份超过 10 页 PDF 验证真实模型的识别效果及输出容量。
