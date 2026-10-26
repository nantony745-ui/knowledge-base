#!/usr/bin/env bash
# 启动 KNOWLEDGE_BASE：业务栈(compose.yaml + Milvus 租约覆盖)与 Langfuse 观测栈，
# 后台运行唯一 RQ worker，再以热重载方式前台运行 API。
# Ctrl-C 停止 API 与 worker；容器需另行 docker compose down。
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose -f compose.yaml -f infra/milvus-local.compose.yaml up -d --wait

docker compose --env-file .env.langfuse -f infra/langfuse.compose.yaml up -d --wait

.venv/bin/python -m alembic upgrade head

.venv/bin/python -m app.ingest.worker &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app
