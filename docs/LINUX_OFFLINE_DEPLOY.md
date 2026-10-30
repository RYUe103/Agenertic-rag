# Linux 内网离线部署

## 推荐方式：Docker 离线镜像

联网机器上构建镜像并导出：

```bash
docker compose build
docker save agentic-rag-jit-agentic-rag:latest | gzip > agentic-rag-jit-image.tar.gz
```

将 `agentic-rag-jit-image.tar.gz`、`docker-compose.yml`、`.env.example` 和本部署包复制到内网 Linux 主机。

内网主机执行：

```bash
gunzip -c agentic-rag-jit-image.tar.gz | docker load
cp .env.example .env
# 编辑 .env，填入内网可访问的 LLM、Dify 或 Workflow 地址
docker compose up -d
```

访问：`http://<内网主机>:8765`

首次登录账号默认为 `sansuokb`，项目不内置管理员密码。服务端仅保存 PBKDF2
密码哈希。首次启动前必须通过 `.env` 中的 `ADMIN_USERNAME` 和
`ADMIN_PASSWORD_HASH` 配置账号密码；运行
`python -c 'from app.auth import hash_password; print(hash_password("新密码"))'`
可生成新哈希。

## 无 Docker 方式

Linux 需要 Python 3.11+，在有网络的同版本 Linux 机器下载 wheel：

```bash
python3.11 -m pip download -d wheels -e '.[dev]'
```

将项目和 `wheels/` 一起复制到内网：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --no-index --find-links wheels -e '.[dev]'
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

## 内网配置注意事项

- `APP_MOCK_EXTERNAL_SERVICES=false` 时，LLM、Dify Dataset、Dify Workflow 和文件平台地址必须从 Linux 内网可访问。
- macOS Keychain 不适用于 Linux；Linux 测试环境请优先使用环境变量，后续再接 Vault 或 Docker Secret。
- `.env`、`data/`、真实 API Key、真实知识库原文件不要放入代码压缩包或 Git。
- Linux 不支持 macOS `textutil`；`.doc` 旧格式若必须处理，需要安装 LibreOffice，并在后续增加 Linux 转换适配器。PDF、DOCX、TXT 不受影响。
- `ripgrep` 已写入 Docker 镜像；非 Docker 部署建议安装系统包 `ripgrep`。

## 健康检查

```bash
curl http://127.0.0.1:8765/api/health
```

检查 `status=ok`、`agent_engine=langgraph` 和 `ripgrep_available`。
