#!/usr/bin/env bash
# Apache Superset with Cost Guard, local only (http://127.0.0.1:8088).
#   scripts/superset.sh setup   one time: Superset's database, admin login, BigQuery connection
#   scripts/superset.sh         start the server (Ctrl+C stops it)
# Superset lives in .venv-superset (Python 3.11); Cost Guard checks every query
# on the BigQuery database before it runs (superset/cost_guard_hook.py).
cd "$(dirname "$0")/.." || exit 1
set -a; . ./.env; set +a
export SUPERSET_CONFIG_PATH="$PWD/superset/superset_config.py"
export FLASK_APP=superset
SUPERSET=.venv-superset/bin/superset
HOME_DIR=.superset_home

if [ "$1" = "setup" ]; then
  mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"
  [ -f "$HOME_DIR/secret_key" ] || (umask 077; openssl rand -base64 42 > "$HOME_DIR/secret_key")
  if [ ! -f "$HOME_DIR/admin_password" ]; then
    (umask 077; openssl rand -base64 12 | tr -d '/+=' > "$HOME_DIR/admin_password")
  fi
  "$SUPERSET" db upgrade || exit 1
  "$SUPERSET" fab create-admin --username admin --firstname Cost --lastname Guard \
    --email admin@localhost --password "$(cat "$HOME_DIR/admin_password")" || exit 1
  "$SUPERSET" init || exit 1
  "$SUPERSET" set-database-uri -d "TheLook (BigQuery)" \
    -u "bigquery://$GCP_PROJECT/cost_guard_thelook" || exit 1
  echo "Setup done. Login: admin / the password in $HOME_DIR/admin_password"
  exit 0
fi

exec "$SUPERSET" run -h 127.0.0.1 -p 8088 --with-threads
