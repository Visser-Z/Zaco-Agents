#!/usr/bin/env bash
# Run one migration file against the live Azure database.
#
#   bash infra/db/migrate.sh supabase/migrations/0030_app_settings.sql [database]
#
# Default database: zacon_live. Opens the server to this machine's address for
# the run and closes it again on exit, whatever happens. Stops at the first
# error, then asks PostgREST to reload its schema so a new table or column is
# served straight away.
set -euo pipefail
export MSYS_NO_PATHCONV=1

FILE=${1:?usage: migrate.sh <migration.sql> [database]}
DB=${2:-zacon_live}
RG=rg-zacon
KV=kv-zacon-za1
PG=pg-zacon-za1
HOST=$PG.postgres.database.azure.com
BIN=${PG_BIN:-/c/Program Files/PostgreSQL/17/bin}

[ -f "$FILE" ] || { echo "No such file: $FILE"; exit 1; }
case "$DB" in *[!a-z0-9_]*|"") echo "Database name must be lower case letters, digits, underscores"; exit 1;; esac
# psql.exe is a Windows program and cannot open Git Bash paths like /c/...
WINFILE=$FILE
command -v cygpath >/dev/null && WINFILE=$(cygpath -w "$FILE")

IP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create -g "$RG" -s "$PG" -n temp-migrate \
  --start-ip-address "$IP" --end-ip-address "$IP" -o none
close() {
  az postgres flexible-server firewall-rule delete -g "$RG" -s "$PG" -n temp-migrate --yes -o none 2>/dev/null \
    && echo "Closed the server to this machine again."
}
trap close EXIT

PGPASSWORD=$(az keyvault secret show --vault-name "$KV" -n pg-admin-password --query value -o tsv)
export PGPASSWORD
psql() { "$BIN/psql.exe" "host=$HOST dbname=$DB user=zaconadmin sslmode=require" "$@"; }

echo "Running $(basename "$FILE") on $DB"
psql -q -v ON_ERROR_STOP=1 -f "$WINFILE"
psql -q -c "notify pgrst, 'reload schema'"
echo "Done."
