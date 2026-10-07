#!/bin/sh
# 容器启动入口：初始化数据库（幂等），然后启动 API。
set -e

echo "[entrypoint] initializing database at ${DOCS_DB_PATH:-/app/data/docs.db} ..."
python -c "from app.db import init_db; init_db(); print('[entrypoint] database ready')"

echo "[entrypoint] starting API on 0.0.0.0:8000 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
