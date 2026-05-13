#!/usr/bin/env sh
set -eu

mkdir -p /app/config /app/storage/imports /app/storage/exports /app/storage/logs

if [ ! -f /app/config/config.yaml ]; then
  cp /app/config.example.yaml /app/config/config.yaml
fi

export ANYSQL_CONFIG="${ANYSQL_CONFIG:-/app/config/config.yaml}"
export ANYSQL_STORAGE_BACKEND="${ANYSQL_STORAGE_BACKEND:-postgresql}"
export ANYSQL_QUEUE_BACKEND="${ANYSQL_QUEUE_BACKEND:-redis}"
export ANYSQL_DATABASE_URL="${ANYSQL_DATABASE_URL:-postgresql+psycopg://anysql:${POSTGRES_PASSWORD:-anysql}@postgres:5432/anysql}"
export ANYSQL_REDIS_URL="${ANYSQL_REDIS_URL:-redis://redis:6379/0}"
export ANYSQL_SERVER_PORT="${ANYSQL_SERVER_PORT:-8765}"

until nc -z postgres 5432; do
  echo "waiting for postgres..."
  sleep 2
done

until nc -z redis 6379; do
  echo "waiting for redis..."
  sleep 2
done

if [ "${ANYSQL_RUN_MIGRATIONS:-true}" = "true" ]; then
alembic upgrade head
fi

case "${1:-api}" in
  api)
    exec uvicorn anysql.main:app --host 0.0.0.0 --port "$ANYSQL_SERVER_PORT"
    ;;
  worker)
    exec python -m anysql.worker
    ;;
  scheduler)
    exec python -m anysql.scheduler
    ;;
  migrate-local)
    shift
    exec python -m anysql.migrate_local "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
