#!/usr/bin/env bash
# Run a throwaway PostgreSQL 16 + pgvector cluster without Docker or root.
#
# Needs `initdb`, `pg_ctl` and the pgvector extension on PATH, e.g. from
#   conda create -n gsm -c conda-forge python=3.12 postgresql=16 pgvector
#
# Usage: scripts/local_pg.sh start|stop|status|url|reset
#   GSM_PGDATA  data directory      (default: .local/pgdata)
#   GSM_PGPORT  TCP port            (default: 55432)
set -euo pipefail

cd "$(dirname "$0")/.."
PGDATA_DIR="${GSM_PGDATA:-.local/pgdata}"
PORT="${GSM_PGPORT:-55432}"
SOCKDIR="$(cd "$(dirname "$PGDATA_DIR")" 2>/dev/null && pwd || echo "$PWD/.local")/pgsock"
USER_NAME="gsm_owner"

url() {
  echo "postgresql+psycopg://${USER_NAME}@localhost:${PORT}/gsm"
}

start() {
  mkdir -p "$(dirname "$PGDATA_DIR")" "$SOCKDIR"
  if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
    initdb -D "$PGDATA_DIR" -U "$USER_NAME" --auth=trust --encoding=UTF8 --locale=C >/dev/null
  fi
  if ! pg_ctl -D "$PGDATA_DIR" status >/dev/null 2>&1; then
    pg_ctl -D "$PGDATA_DIR" -l "$PGDATA_DIR/server.log" -w \
      -o "-p ${PORT} -k ${SOCKDIR} -c listen_addresses=localhost" start >/dev/null
  fi
  for db in gsm gsm_test; do
    psql -h localhost -p "$PORT" -U "$USER_NAME" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1 \
      || psql -h localhost -p "$PORT" -U "$USER_NAME" -d postgres -qc "CREATE DATABASE ${db}"
  done
  echo "PostgreSQL running. GSM_DATABASE_URL=$(url)"
}

stop() {
  pg_ctl -D "$PGDATA_DIR" -m fast stop >/dev/null 2>&1 || true
  echo "stopped"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) pg_ctl -D "$PGDATA_DIR" status ;;
  url) url ;;
  reset) stop; rm -rf "$PGDATA_DIR"; start ;;
  *) echo "usage: $0 start|stop|status|url|reset" >&2; exit 2 ;;
esac
