# 模型设置

首页侧栏的“设置”打开 `/settings`，可分别修改 Chat、Embedding、Reranker 的模型名称、URL 和 API Key。Chat、Embedding 使用 OpenAI 兼容 Base URL，Reranker 使用完整重排接口地址及项目现有协议。

首次打开读取当前环境配置。每组独立保存；密钥留空保留原值，页面仅返回是否已配置，不回显密钥。覆盖配置写入项目根目录 `.env.models`，文件权限为 `0600`，通过临时文件原子替换。该文件由现有 `.env.*` 规则忽略，不提交到 Git。原 `.env` 保留，重启后仍优先读取已保存的覆盖配置。

保存后，普通对话、新启动的快速问答、智能推理、检索请求和新入库任务使用新配置。每个运行保留独立的完整配置与客户端，正在进行的运行继续使用原配置。BM25 缓存及技能目录继续共享。

“检测连接”使用当前表单中的值及填写的新密钥（留空使用已保存密钥），不会保存表单；它会实际请求对应的生成、向量或重排接口，校验响应，并在结束后关闭客户端，最多等待 30 秒。检测仅验证对应接口可用性。更换 Embedding 模型后，已有文档需重新索引；新模型需与现有向量维度兼容。

接口均要求管理员登录，写入及检测同时校验 Origin：

| 接口 | 用途 |
| --- | --- |
| `GET /api/settings/models` | 读取三组当前配置及密钥配置状态 |
| `PUT /api/settings/models/{service}` | 保存单组配置 |
| `POST /api/settings/models/{service}/test` | 检测未保存的配置 |

`service` 为 `chat`、`embedding`、`reranker`。请求字段为 `model`、`url`、可选 `api_key`。

本地验证命令：

```bash
.venv/bin/python -m pytest -q tests
```

在已安装 Node 和 Chrome 的 Windows 终端中运行：

```powershell
node --test tests/test_model_settings_browser.cjs tests/test_toolbox_frontend.cjs tests/test_mcp_frontend.cjs tests/test_frontend_chat.cjs
```

模型接口测试使用本地 HTTP 服务，覆盖三类真实适配器的有效响应、无效密钥、独立持久化、保存失败保留原值和运行中配置隔离。浏览器测试使用本地模拟接口，覆盖桌面和窄屏页面的默认值、保存、连接检测、错误提示与防重复提交，不调用云端付费模型。
