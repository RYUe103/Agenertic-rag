# 本地普通用户体验（2026-11-03）

新增 `app/local_accounts.py`、`scripts/create_local_user.py`；登录入口支持本地普通账号，身份为 `local_user:用户名`，与本地管理员及 LDAP 身份隔离。账户仅由主机操作员创建，不提供开放注册。密码使用现有 PBKDF2 哈希；账户存储在数据目录 `local-accounts.db`，文件权限 0600。重复创建拒绝，不覆盖密码。

创建命令：`.runtime/bin/python -m scripts.create_local_user 用户名`，交互输入至少 12 位密码。默认 viewer 角色，不自动授权现有知识库。用户名不能与管理员同名。此批未实现密码重置、禁用及会话撤销等完整账户管理生命周期；不要通过直接修改数据库实现这些操作。

体验用户 `experience` 已创建，密码不写入文档。前端仅调整登录说明及普通用户身份标签，未重设计界面。

验证：`.runtime/bin/python -m pytest -q tests/test_local_accounts.py tests/test_api.py`：38 passed，5 warnings。`cd frontend && npm run build` 成功，保留体积警告。
本地 8765 HTTP 验证：真实账号登录 200，is_admin=false，管理员权限接口 403，可见知识库为空，首页 200。未进行浏览器视觉验收、LDAP 实网验收或真实知识库检索验收。
