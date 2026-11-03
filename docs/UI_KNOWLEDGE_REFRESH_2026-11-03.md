# 问答与知识库配置界面调整

方向：按用户参考图采用单列“提问 → 回答 → 引用/轨迹”，沿用 Semi Design 和绿色主色。重点是清楚提问、查看证据，不增加装饰性图像。

## 实现

- 问答页默认选择智能推理，保留标准问答模式；解释能力而非只显示 ReAct V2。
- 输入、问题建议、主按钮集中展示；显示加载、错误、审批、客户端耗时；错误保留输入。
- 回答下方分引用和执行轨迹标签；引用点击阻止 HashRouter 跳转，打开来源标签并定位。
- 切换知识库时丢弃前一个请求的迟到结果；本次 UI 没有改变检索算法和 Agent 推理实现。
- 空知识库提示；390px 窄屏图标导航、表单/问答布局；主按钮反馈、入场和标签切换动效，支持减少动态效果偏好。
- 服务端为新建请求生成平台 KB UUID，普通用户即使发送自定义 ID 也不会采用；编辑不可更名 ID。管理员旧 API 显式指定 ID 暂留兼容，现有数据不迁移。
- 列表显示并复制平台 ID。Dify Dataset ID/API Key 继续由用户填写，并明确与平台 ID/MCP Key 区分。
- 租户、Embedding 标识、解析器只向管理员展示；普通用户创建时使用默认值，更新时保持管理员值。Embedding 标识为空时不猜测 Dify 实际使用模型，也不冒用本地 Milvus 模型。
- 包含前一轮已完成的本地普通账号登录代码、CLI 和测试，避免推送缺少依赖的登录页面；不包含体验密码、数据库或原有 Agent/Milvus 工作区变更。

## 验证与边界

- `.runtime/bin/python -m pytest -q tests/test_knowledge_creation_ui.py tests/test_local_accounts.py tests/test_api.py`：39 passed，5 条已有依赖警告。
- `npm --prefix frontend run build`：成功；保留 bundle 体积警告。静态产物同步发布，因为 Python 服务直接提供 static 目录。
- `scripts/smoke_knowledge_ui_login.cjs`：真实本地普通账号登录、问答空状态、新建表单隐藏管理员字段；390px 文档宽度为390px，无横向溢出。通过 `KB_TEST_USERNAME` / `KB_TEST_PASSWORD` 注入测试账号。
- `scripts/smoke_knowledge_ui_fixtures.cjs`：仅前端 HTTP/SSE fixture，验证结果、引用/轨迹切换、点击引用不离页、503错误与问题保留。不是实际模型/Dify/MinerU 验收，不产生模型调用。
- 浏览器脚本通过 `PLAYWRIGHT_MODULE` 指定已有 Playwright 模块位置；默认使用 `playwright`，Chrome headless，地址127.0.0.1:8765。
- 一轮独立截图评审：修正知识库选择空白、手机快捷键冗余、普通用户 Dify 凭据排布。主执行者再次查看桌面/手机/表单/结果截图。未进行全站 UI 改版或真实问答质量评测。
- 截图在 `docs/ui/knowledge-20261103/`；`result-fixture.png` 是明确标记的界面测试数据，其余为空知识库普通用户页面。

## 未改动

三所小智项目、原知识库数据、外部服务配置，以及之前未提交的 Agent、向量适配和评测工作。本次不加入模型信息假数据、未实现的上传按钮或语音按钮。

补充验证：仅含本次暂存文件的隔离快照也运行上述 pytest 命令，39 passed，5 warnings；不依赖工作区未提交的 Agent/Milvus 实现。用户明确以桌面端为验收重点，已完成的基础窄屏兼容保留，后续不再扩展手机设计。
