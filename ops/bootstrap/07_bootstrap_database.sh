#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <db_password>"
  exit 1
fi

DB_PASSWORD="$1"
DB_PASSWORD_SQL="${DB_PASSWORD//\'/\'\'}"
ROLE_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'mai_tai'")"
DB_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'project_mai_tai'")"

if [[ "$ROLE_EXISTS" != "1" ]]; then
  sudo -u postgres psql -c "CREATE ROLE mai_tai LOGIN PASSWORD '$DB_PASSWORD_SQL'"
else
  sudo -u postgres psql -c "ALTER ROLE mai_tai WITH PASSWORD '$DB_PASSWORD_SQL'"
fi

if [[ "$DB_EXISTS" != "1" ]]; then
  sudo -u postgres psql -c "CREATE DATABASE project_mai_tai OWNER mai_tai"
fi
