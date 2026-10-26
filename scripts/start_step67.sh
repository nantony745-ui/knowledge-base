#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  api) exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 ;;
  worker) exec .venv/bin/python -m app.ingest.worker ;;
  recover) exec .venv/bin/python -c 'from app.ingest.recovery import recover; print(recover())' ;;
  *) printf '%s\n' '用法: scripts/start_step67.sh api|worker|recover'; exit 2 ;;
esac
