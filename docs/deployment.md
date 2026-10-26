# 部署与开发

## 快速开始

推荐在 Linux 或 WSL 中运行。需要 **Python 3.12、Docker Engine 和 Docker Compose v2**。Web 默认地址为 `http://127.0.0.1:8000`。

### 1. 安装依赖并生成本地配置

在仓库根目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
bash scripts/gen-secrets.sh
```

脚本创建 `.env` 与 `.env.langfuse`，生成本地数据库、管理员和观测服务的随机凭据，重复执行保留已有有效值。使用本机编辑器查看配置，填写模型服务参数；环境文件已被 Git 忽略。

### 2. 配置模型

编辑 `.env`，填写以下必需项：

| 服务 | 配置项 | 说明 |
| --- | --- | --- |
| 聊天 | `CHAT_BASE_URL`、`CHAT_API_KEY`、`CHAT_MODEL` | 支持工具调用的 OpenAI 兼容聊天接口 |
| 向量 | `EMBED_BASE_URL`、`EMBED_API_KEY`、`EMBED_MODEL`、`EMBED_DIMENSION` | 维度填写该服务实际返回的维度 |
| 重排 | `RERANK_URL`、`RERANK_API_KEY`、`RERANK_MODEL` | URL 填写完整重排端点 |

不同服务可使用不同供应商。首次使用先确认模型和服务可用；更换向量模型或维度时使用新的索引集合。仅文本入库不需要配置 MinerU。

### 3. 启动

```bash
bash scripts/start_kb.sh
```

脚本依次启动业务依赖与 Langfuse、执行数据库迁移，再启动一个 RQ worker 和 API。首次启动会创建空库表结构与管理员。打开首页，使用 `.env` 中的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。

在知识库中新建一个库，上传 [示例资料](../examples/documents)，等待文档就绪。选择该库后尝试：

- 快速问答：**P1 故障的首次响应目标和通知要求是什么？**
- 智能推理：**结合上线检查清单和数据安全规范，整理对外发布前的检查项，区分必须项与建议项，并给出来源。**

按 `Ctrl+C` 停止 API 与 worker；容器保留运行。停止容器可执行：

```bash
docker compose -f compose.yaml -f infra/milvus-local.compose.yaml stop
docker compose --env-file .env.langfuse -f infra/langfuse.compose.yaml stop
```

## 配置说明

完整模板见 [.env.example](../.env.example)。

| 配置 | 默认值 / 用途 |
| --- | --- |
| `APP_ORIGIN` | `http://127.0.0.1:8000`，须与浏览器访问地址一致 |
| `COOKIE_SECURE` | 本机 HTTP 为 `false`；HTTPS 部署设为 `true` |
| `DATA_DIR` | 默认 `DATA/runtime`，保存上传文件和本地运行状态 |
| `EMBED_BATCH_SIZE` | 默认 16；按供应商批量接口限制调整 |
| `MILVUS_COLLECTION` | 向量集合名称，首次入库按配置维度初始化 |
| `SEARCH_KNOWLEDGE_CONCURRENCY` | 默认 4，设为 1 时串行搜索 |
| `SKILL_DIRS` | 默认扫描仓库 `skills/`；可配置为 JSON 路径列表 |
| `LANGFUSE_BASE_URL` | 默认 `http://127.0.0.1:3010` |

MySQL、Redis 和 Milvus 默认分别映射到本机 `3316`、`6380`、`19540`，端口均绑定回环地址。多份部署可通过 `COMPOSE_PROJECT_NAME` 与 `LANGFUSE_COMPOSE_PROJECT_NAME` 隔离资源，并在 Compose 与 `.env` 中同步调整宿主端口。

数据库迁移仅接管空数据库或已有 Alembic 版本记录的数据库。已有非空业务库应自行规划迁移，不直接套用初始化命令。

Langfuse 管理员信息位于本地 `.env.langfuse`，使用 `LANGFUSE_INIT_USER_EMAIL` 和 `LANGFUSE_INIT_USER_PASSWORD` 登录。追踪不可用时，应用会记录观测告警；问答仍遵循原有错误处理。

## 开发与验证

```bash
# 离线 Python 测试：使用合成资料，不调用模型或访问业务库
.venv/bin/python -m pytest -q tests

# 前端语法与交互测试，需要 Node.js 22 或更新版本
node --check frontend/app.js
node --check frontend/quick-answer.js
node --test tests/test_frontend_chat.cjs tests/test_toolbox_frontend.cjs tests/test_mcp_frontend.cjs
```

手动验收顺序：登录 → 新建知识库 → 上传示例文档 → 等待就绪 → 检索 → 快速问答 → 打开来源 → 智能推理 → 刷新恢复会话。

| 目录 | 内容 |
| --- | --- |
| `app/` | API、Agent、检索管线、入库、数据访问及外部适配 |
| `frontend/` | 页面、样式、交互及本地 Markdown 渲染器 |
| `migrations/` | 数据库迁移 |
| `infra/` | Langfuse 和本地 Milvus 覆盖配置 |
| `scripts/` | 配置初始化、启动及入库恢复 |
| `skills/` | 可加载的技能与资源 |
| `examples/` | 合成演示资料 |
| `tests/` | 离线机制与前端交互测试 |

项目说明与界面组织参考 [WeKnora](https://github.com/Tencent/WeKnora)，第三方组件见 [第三方声明](../THIRD_PARTY_NOTICES.md)。
