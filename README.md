# Agentic RAG + JIT 文件检索原型

这是一个可在 macOS 本地独立运行的多格式文件知识库原型。它同时使用 SQLite 文件元数据检索、本地 Markdown 精确检索和 Dify 向量检索；模型只接收按需读取的命中上下文，不会一次载入所有文件。

默认登录账号为 `sansuokb`。项目不内置管理员密码；首次启动前必须在 `.env` 中配置 `ADMIN_PASSWORD_HASH`。应用只保存密码哈希。文件入库启用 AI 改写后，可以选择直接入库或预览确认；预览模式确认前不会上传 Dify。

默认启用 Mock 外部服务，因此没有 LLM、Dify 或文件平台账号也能完成文档入库、精确搜索、网页问答和来源链接演示。

## 能力

- 一次选择最多 2000 个 PDF、DOC、DOCX、TXT，前端自动每 25 个分批上传
- SQLite 持久化入库队列，有限并发处理，支持暂停、恢复、失败重试和服务重启恢复
- SHA-256 去重，原文件和标准化 Markdown 本地留存
- PDF Markdown 含页码标记；Word 含段落/表格行标记；TXT 含原始行号标记
- DOC/DOCX/TXT 转成 Markdown 后上传 Dify，文件平台始终接收原始文件
- PDF 可按知识库选择本地解析、MinerU 或 Qwen-VL 视觉解析；10 页以内整份处理，长文档按配置页数分批处理
- MinerU精确读取 `md_content`，保存原始响应和Markdown，并明确记录成功或本地回退
- Qwen-VL 渲染页和分批 Markdown 留存在文件目录，视觉结果直接作为最终 Markdown，不再重复 AI 改写
- 资料库使用后端分页，并可预览最终、原始、Workflow、改写和视觉分批 Markdown
- PDF 扫描页明确标注并记录到 `metadata.json`
- 自动提取标准号、标题、年份、发布日期、实施日期、状态和替代关系
- 本地解析、Dify Markdown 上传、源文件平台上传三段状态独立保存，只重试失败步骤
- 固定参数调用 ripgrep，支持文件范围、多个关键词、正则开关、分页和超时；无 ripgrep 时回退 Python
- LangGraph 有界 Agent 循环：检索规划、证据评分、Planner 重规划、有限重试和引用校验
- 特殊查询保留确定性路由，并通过工具次数和检索轮数双重限制防止无界循环
- 所有回答绑定结构化 evidence 和实际原文件 URL；无证据时明确拒答
- Dify、OpenAI 兼容模型、HTTP/命令行/Mock 文件平台均为独立适配器

## 本地运行

需要 Python 3.11 或更高版本。建议安装 ripgrep，但不是必需项。

```bash
cd /Users/yooyoung/Documents/ChatGPT/知识库
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python -c 'from app.auth import hash_password; print(hash_password("你的管理员密码"))'
# 将上一步输出写入 .env 的 ADMIN_PASSWORD_HASH
uvicorn app.main:app --reload
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。数据写入：

```text
data/
├── sources.db
├── ingest-queue.db
├── ingest-staging/
└── files/{source_id}/
    ├── source.{pdf|doc|docx|txt}
    ├── extracted.md
    ├── preview.md（启用改写预览时）
    ├── document.md
    ├── rendered/page-0001.jpg（Qwen-VL 模式）
    ├── vision-batches/batch-0001-pages-1-3.md（Qwen-VL 长文档模式）
    └── metadata.json
```

### Semi Design 前端

管理平台使用 React、TypeScript、Vite 和 Semi Design，前端源码位于 `frontend/`。首次安装依赖并生成生产资源：

```bash
cd /Users/yooyoung/Documents/ChatGPT/知识库/frontend
npm install
npm run build
```

生产构建会写入项目根目录的 `static/`，由 FastAPI 在 `8765` 端口统一提供页面和 API。日常开发可分别启动后端与 Vite 开发服务器：

```bash
# 终端一：项目根目录
./run.sh

# 终端二
cd frontend
npm run dev
```

开发页面默认位于 [http://127.0.0.1:5173](http://127.0.0.1:5173)，`/api` 请求代理到本地后端。提交前运行：

```bash
cd frontend
npm run typecheck
npm run build
```

## 配置真实服务

编辑 `.env`：

```dotenv
APP_MOCK_EXTERNAL_SERVICES=false
LLM_BASE_URL=https://your-openai-compatible-host
LLM_API_KEY=...
LLM_MODEL=...
LLM_API_STYLE=chat_completions

DIFY_BASE_URL=https://api.dify.ai/v1
DIFY_KNOWLEDGE_API_KEY=dataset-...
DIFY_DATASET_ID=...
DIFY_SEARCH_METHOD=semantic_search
```

`LLM_BASE_URL` 按 OpenAI 兼容接口的要求配置。`DIFY_BASE_URL` 严格按填写值使用，系统不会自动补 `/v1`；例如内网 Dify 的数据集 API 在 `/api/v1` 时，就填 `http://agent.example/api/v1`。

`DIFY_SEARCH_METHOD=semantic_search` 表示 Dify 只负责纯向量检索；本地 `grep_files` 作为独立精确检索工具，由 Agent 根据问题和证据评分自主选择。文件清单意图下，`grep_files` 会联合搜索文件名、自定义文本元数据和本地 Markdown 正文，再按 `source_id` 合并结果。

`LLM_API_STYLE` 可设为 `chat_completions` 或 `responses`。规划阶段优先尝试 Chat Completions 原生工具调用；接口不支持时降级为 JSON Planner，再失败则由确定性规则接管。回答阶段可使用 Chat Completions 或 Responses 风格。

管理员可在“模型连接”中维护多个 OpenAI 兼容模型，配置 Base URL、API Key、模型名、单次超时和优先级。每个 Agent 可分别选择 Planner 与 Answer 模型链，按优先级从小到大尝试。上游返回 `429`、`408`、`5xx` 或发生连接/读取超时时，当前模型调用会立即切换到下一个连接；`400`、`401`、`403` 视为请求或配置错误，不自动切换。同一连接连续失败 3 次后熔断 60 秒；上游 `429` 含 `Retry-After` 时会按该时间暂停使用。原 `.env` 中的 `LLM_*` 配置会自动迁移为 `default` 模型连接。

每个知识库可在“知识库连接 -> PDF 解析”中选择解析器。Qwen-VL 使用 OpenAI 兼容的多模态 `/chat/completions` 接口，Base URL、API Key、模型、短/长文档提示词、分页阈值、批大小和渲染 DPI 均独立配置。默认 10 页以内一次提交全部页面；超过 10 页时每 3 页一批，并将实际解析引擎、页数、批次数和失败批次写入该文件的 `metadata.json`。

MinerU接口优先读取顶层 `md_content`，支持单行HTML表格内容；`content_list` 为JSON字符串时也会解码。成功结果分别保存为 `mineru-original.md` 和 `mineru-response.json`。默认失败后回退本地解析并在 `metadata.json` 标记 `parser_engine=local_fallback`；设置 `MINERU_FALLBACK_ON_ERROR=false` 可禁止回退。资料库中的“重新解析”会从原文件重建Markdown并刷新Dify。

Agent 循环可通过以下参数限制：

```dotenv
AGENT_MAX_TOOL_CALLS=6
AGENT_MAX_RETRIEVAL_ROUNDS=3
AGENT_MAX_EVIDENCE_FOR_ANSWER=12
```

大批量入库参数：

```dotenv
INGEST_WORKER_COUNT=3
INGEST_UPLOAD_BATCH_SIZE=25
INGEST_MAX_FILES_PER_JOB=2000
INGEST_MAX_RETRIES=3
ASK_CONCURRENCY_LIMIT=40
ASK_QUEUE_TIMEOUT_SECONDS=10
```

前端会将一次选择的文件按“最多 `INGEST_UPLOAD_BATCH_SIZE` 个且总体积不超过约 120MB”分批上传，避免触发 Nginx 默认的请求体限制。全部上传完成后才启动后台处理。Worker 数量决定同时解析和上传 Dify 的文件数；内网服务压力较大时可设为 `1` 或 `2`。队列保存在 `data/ingest-queue.db`，服务异常退出后，未完成项会在下次启动恢复。

批量入库的开始、成功、自动重试和最终失败都会写入服务日志，日志字段包含 `job_id`、`item_id`、`filename`、`attempt` 和 `source_id`。最终失败会记录异常堆栈。历史任务的错误原因也保存在 `data/ingest-queue.db` 的 `ingest_job_items.message` 中。

普通概念问题会在 `规划 -> 工具 -> 证据评分 -> Planner 重规划/生成 -> 引用校验` 状态图中运行。每轮工具结果先进入去重后的候选证据池；证据不足时直接交回 Planner 切换检索方式，不再单独调用查询改写模型。最终只将评分筛选出的有效证据交给 Answer 模型。

回答使用 Markdown 输出并在浏览器中安全渲染。`ev_NNN` 证据编号只用于后端引用校验和来源映射，不显示在最终回答、执行轨迹或证据卡片中；最终回答底部统一追加可点击的“引用来源”。

检索规划、证据评分、回答和引用校验的模型请求都固定使用 `temperature=0`。精确文件名或编号由 Planner AI 理解和提取，提取后由本地工具确定性定位；同族文件清单会返回全部匹配文件。需要区分“证据充分”和“引用校验”：前者判断内容能否回答问题，后者只判断回答引用是否指向实际返回的证据，引用校验通过不等于证据充分。

文件意图和文件线索由 Planner AI 从自然语言中提取，不再使用业务编号正则提前分流。Planner 识别为具体文件内容问题后，状态机会确定性执行 `find_files -> read_full_document`；唯一文件命中后只保留一条全文证据，不再扩展上下文、改写查询或回到全库向量检索。文件清单意图则只列出 `find_files` 或 `grep_files` 的确定性结果。

多轮追问、“最新”元数据查询、检索失败后的工具切换和上下文扩展均由 Planner 决定，不再由代码关键词或固定工具轮换强制。每轮 Planner 会收到上一轮证据评分、缺失主题、已使用查询、候选 source_id 和会话历史；独立新问题的评分和回答只使用本轮原始问题，不自动继承上一轮文件线索。多轮列举只有在 Planner 显式设置 `exclude_previous_results=true` 时才排除上轮结果。

“关于某主题最新/最早的文件”使用两步流程：先由 `grep_files(intent=rank_documents)` 从文件名、自定义文本元数据和正文中建立候选集合，再由 `metadata_search(source_ids=候选)` 只在候选范围内按配置日期字段排序。主题词不会被自动当作元数据过滤值；文本日期支持 `YYYY-MM-DD`、`YYYY/M/D` 和中文年月日格式，缺少日期的文件始终排在有日期文件之后。

### Answer 提示词与外部回答

每个知识库可在“知识库连接 -> 回答模型”中单独配置 Answer 提示词。`/api/ask` 和 `/api/agents/{agent_id}/ask` 使用该提示词调用本平台 Answer 模型。

外部系统可调用仅检索接口：

```http
POST /api/agents/{agent_id}/retrieve
Content-Type: application/json

{
  "question": "MTR-NS-029 主要说了什么？",
  "conversation_id": "user-123-session-456",
  "history": []
}
```

默认 Agent 也可使用 `POST /api/retrieve`。响应包含 `knowledge_base_id`、`answer_prompt`、`evidences`、`sources`、`evidence_insufficient` 和调试轨迹，不包含生成答案，也不会调用本平台 Answer 模型。外部系统应将用户问题与 `evidences` 交给自己的回答模型，并保留 `[ev_NNN]` 引用标记或使用 `sources` 生成 Markdown 来源链接。

开启外部只读问答 API：

```dotenv
EXTERNAL_API_ENABLED=true
EXTERNAL_API_KEY=请设置独立的高强度随机密钥
```

外部系统请求时必须附加 `Authorization: Bearer <EXTERNAL_API_KEY>`。该 Key 允许调用上述两个只读检索接口、`POST /api/ask`、`POST /api/agents/{agent_id}/ask` 和 `GET /api/agents` Agent 发现接口，不能访问管理、上传、删除或配置接口。`/api/health` 只返回 `external_api_configured` 布尔值，不回显密钥。

外部系统若希望直接使用本平台回答模型，可调用 Ask 接口，参数与 retrieve 相同。响应除 `answer`、`evidences` 外，新增 `sources`，仅包含实际引用文件及其 HTTP/HTTPS URL、页码与位置。调用方可直接使用结构化 `sources.url` 生成引用，不需要再调用 retrieve；缺少有效 URL 时不应自行拼造文件地址。

### 源文件平台

HTTP 适配器约定：

```dotenv
PDF_PLATFORM_MODE=http
PDF_PLATFORM_BASE_URL=https://pdf.example.com/api
PDF_PLATFORM_API_KEY=...
```

系统以 multipart `file` 调用 `POST {BASE_URL}/upload`，保留原始文件名和 MIME 类型，期望返回：

```json
{"file_id":"pdf_xxx","url":"https://example.com/view/pdf_xxx"}
```

命令行/现有 Python 脚本适配器：

```dotenv
PDF_PLATFORM_MODE=command
PDF_PLATFORM_COMMAND=["python3","/absolute/path/upload_pdf.py"]
PDF_PLATFORM_URL_TEMPLATE=https://example.com/view/{file_id}
```

这些环境变量现在作为全局兼容配置。每个知识库可在管理页面的“源文件平台”中选择：

- `继承全局配置`：继续使用上述环境变量。
- `仅保存本地`：不调用外部平台，引用指向本系统 `/api/files/{id}`。
- `HTTP 文件平台`：为该知识库单独设置 Base URL 和 API Key。
- `命令行适配器`：为该知识库单独设置命令和 URL 模板。

平台接收 PDF、Word、TXT 原始文件；转换后的 Markdown 仍保存本地并上传对应 Dify 知识库。修改平台配置只影响后续上传或重试的文件，已成功上传文件保留原来源 URL。

系统把原文件绝对路径作为最后一个参数传入，直接执行参数数组且不经过 shell。脚本 stdout 应输出上述 JSON。临时 URL 失效时可通过保存的 `file_id` 和适配器 `refresh_url()` 重新生成。

旧版 `.doc` 在 macOS 上通过系统自带的 `textutil` 临时转换为 DOCX 后解析；本地保存和文件平台上传的仍是原始 `.doc`。Dify 接收的是生成后的 `document.md`。

## API

- `POST /api/ingest`：兼容旧版的小批量 multipart 入库
- `POST /api/agents/{agent_id}/ingest-jobs`：创建最多 2000 文件的持久化入库任务
- `POST /api/jobs/{job_id}/upload-batch`：按文件位置分批上传
- `POST /api/jobs/{job_id}/start`：全部文件上传后启动处理
- `POST /api/jobs/{job_id}/pause`、`resume`、`retry-failed`：暂停、恢复和重试失败项
- `GET /api/jobs/{job_id}`：查询批处理进度
- `GET /api/sources`、`GET /api/sources/{source_id}`：资料列表和详情
- `POST /api/sources/{source_id}/retry`：只重试未成功步骤
- `POST /api/sources/{source_id}/refresh-dify-status`：刷新 Dify 后台索引状态
- `POST /api/ask`：`{"question":"..."}`；`answer` 末尾按实际引用证据追加去重后的 Markdown 来源链接
- `GET /api/health`：配置和依赖状态（不返回密钥）

批量入库中，Dify 返回 `processing` 表示文档已上传、仍在建立索引，不是入库失败。
队列会保存 `source_id` 并只轮询 Dify 状态，不会重复解析或上传文件。任务消息和日志会同时显示
`parse_status`、`dify_status` 和 `pdf_platform_status`，便于直接判断卡在哪一步。

当问题同时包含明确时间范围和主题（例如“2026 年的管道等级表”），Planner 先用
`metadata_search.custom_date_ranges` 选出时间范围，LangGraph 将这些 `source_id` 锁定为作用域，再在域内
执行文件名、文本元数据和正文检索。时间范围与主题为 AND 关系；时间筛选结果不直接进入最终证据。

Agent 内部工具位于 `app/retrieval.py`。通用检索包括 `find_files`、`grep_files`、`vector_search`；
文档内操作包括 `search_within_document`、`read_pages`、`get_adjacent_chunks`、`get_document_outline`；
元数据操作包括 `metadata_search`、`compare_document_metadata`、`list_latest_standards`。`read_file_context`
和 `get_source` 作为底层上下文读取能力保留。需要文档或向量片段 ID 的工具，仅在 Agent 已取得对应
`source_id` / `segment_id` 后才会开放给规划模型。

## 多 Agent 与前端配置

网页中的“知识库连接”和“Agent 管理”可以直接维护配置，保存后立即热加载，不需要重启服务。
每个 Agent 固定绑定一个知识库；同一个知识库也可以被多个 Agent 共用，并分别设置 Planner
提示词、工具白名单、最大工具调用数、最大检索轮数和回答证据上限。页面由用户明确选择 Agent，
系统不会自动把问题路由到其他 Agent。

知识库和 Agent 配置保存在 `data/control.db`。Dify API Key 写入 macOS 系统钥匙串，接口和页面
只返回“已配置/未配置”，不会回显明文。`config/agents.yaml` 仅在控制库为空时用于首次初始化，
之后请在前端管理。

默认 `default` Agent 继续使用原来的 `data/sources.db` 和 `data/files`，已有资料无需迁移。新知识库
使用 `data/knowledge_bases/<knowledge_base_id>/`。删除 Agent 或知识库配置不会删除这些本地原文件、
Markdown 和索引文件；仍被 Agent 使用的知识库不能删除。

管理接口为：

- `GET/POST /api/admin/knowledge-bases`
- `PUT/DELETE /api/admin/knowledge-bases/{knowledge_base_id}`
- `POST /api/admin/knowledge-bases/test`
- `GET/POST /api/admin/agents`
- `PUT/DELETE /api/admin/agents/{agent_id}`

Agent 作用域接口为：

- `GET /api/agents`
- `POST /api/agents/{agent_id}/ask`
- `POST /api/agents/{agent_id}/ingest`
- `GET /api/agents/{agent_id}/sources`

旧的 `/api/ask`、`/api/ingest` 和 `/api/sources` 仍映射到 `default_agent`。
通过 HTTP API 问答时，本地 `/api/files/...` 来源会按当前服务地址转换成绝对 URL，因此其他支持
Markdown 的聊天前端也能直接显示并打开来源文件。

## 测试

```bash
pytest -q
```

测试会动态生成 PDF、DOC、DOCX 和 TXT，覆盖页码/段落/行号解析、扫描页标记、哈希去重、Markdown 上传、原文件上传、两种文本搜索路径、上下文读取、Agent 路由和 API。不会访问真实外部服务。

## Docker

```bash
docker compose up --build
```

服务位于 `http://127.0.0.1:8000`，`./data` 持久化到容器。生产环境应放在反向代理后，并设置上传大小限制和恶意文档检查。

## OCR 扩展点

第一版不绑定特定 OCR 引擎。`metadata.json` 的 `pages_without_text` 保存待 OCR 页码，Markdown 对应页含明确标记。可配置一个本地 OCR 命令：

```dotenv
OCR_ENABLED=true
OCR_COMMAND=["python3","/absolute/path/ocr_page.py"]
```

系统将页图 PNG 路径和页码作为最后两个参数传入，读取 stdout 作为识别文本，参数数组不经过 shell。也可按 `app/ocr.py` 的 `OcrProvider` 接口接入 PaddleOCR 或内部服务。
# 多用户与 LDAP

平台保留本地管理员 `sansuokb`，并可通过 `LDAP_ENABLED=true` 开启 LDAP 登录。LDAP 用户登录后只能看到和管理自己创建的知识库；已有知识库迁移时默认归属 `sansuokb`。文件平台、MinerU、模型连接等敏感配置仍由管理员维护，普通用户创建知识库时这些字段会被服务端忽略并继承全局配置。

上传到文件平台时，服务端继续使用管理员配置的接口 Token，同时把当前登录用户的域账号放入 `X-User` 请求头。这样平台凭借域账号保存个人文件归属，不能通过浏览器伪造其他用户身份。

LDAP 最小配置示例：

```env
LDAP_ENABLED=true
LDAP_SERVER_URL=ldap://ad.example.local:389
LDAP_USER_DN_TEMPLATE=uid={username},ou=people,dc=example,dc=local
LDAP_TIMEOUT_SECONDS=5
```

如果域控要求先用服务账号搜索用户，可另行配置 `LDAP_BIND_DN` 和 `LDAP_BIND_PASSWORD`；当前适配器也支持直接使用用户 DN 绑定。
