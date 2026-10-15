#!/usr/bin/env bash
# Restore a Supabase backup (scripts/backup_db.ps1) into a fresh database on
# the Azure server, in the order that works, then count what arrived.
#
#   bash infra/db/restore.sh <backup.dump> [database]      default database: zacon
#
# Refuses to touch a database that already holds app tables, so it cannot
# overwrite live data by mistake. Opens the server to this machine's address
# for the run and closes it again on exit, whatever happens.
set -euo pipefail
export MSYS_NO_PATHCONV=1

DUMP=${1:?usage: restore.sh <backup.dump> [database]}
DB=${2:-zacon}
RG=rg-zacon
KV=kv-zacon-za1
PG=pg-zacon-za1
HOST=$PG.postgres.database.azure.com
AUTH_IMAGE=supabase/auth:v2.197.0
BIN=${PG_BIN:-/c/Program Files/PostgreSQL/17/bin}
HERE=$(cd "$(dirname "$0")" && pwd)
# psql.exe is a Windows program and cannot open Git Bash paths like /c/...
command -v cygpath >/dev/null && HERE=$(cygpath -w "$HERE")

[ -f "$DUMP" ] || { echo "No such backup: $DUMP"; exit 1; }
case "$DB" in *[!a-z0-9_]*|"") echo "Database name must be lower case letters, digits, underscores"; exit 1;; esac

say() { printf '\n== %s\n' "$*"; }
kv() { az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv; }

say "Opening the server to this machine for the restore"
IP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create -g "$RG" -s "$PG" -n temp-restore \
  --start-ip-address "$IP" --end-ip-address "$IP" -o none 2>/dev/null
TMPDIR_RESTORE=$(mktemp -d)
close() {
  rm -rf "$TMPDIR_RESTORE"
  az postgres flexible-server firewall-rule delete -g "$RG" -s "$PG" -n temp-restore --yes -o none 2>/dev/null \
    && echo "Closed the server to this machine again."
}
trap close EXIT

ADMIN_PW=$(kv pg-admin-password)
AUTH_PW=$(kv pg-auth-admin-password)
admin() { PGPASSWORD=$ADMIN_PW "$BIN/psql.exe" "host=$HOST dbname=$1 user=zaconadmin sslmode=require" "${@:2}"; }

say "Database $DB (Supabase's sort order: ICU en-US)"
if [ -z "$(admin postgres -At -c "select 1 from pg_database where datname = '$DB'")" ]; then
  admin postgres -q -c "create database $DB template template0 encoding 'UTF8' locale_provider icu icu_locale 'en-US' locale 'en_US.utf8'"
fi
if [ "$(admin "$DB" -At -c "select count(*) from pg_tables where schemaname = 'public'")" != "0" ]; then
  echo "Database $DB already has app tables. Restore into a new database name instead."
  exit 1
fi

say "Supabase roles, schemas and grants"
admin "$DB" -q -v ON_ERROR_STOP=1 -v authenticator_pw="$(kv pg-authenticator-password)" -v auth_admin_pw="$AUTH_PW" \
  -f "$HERE/01_supabase_roles.sql" 2>"$TMPDIR_RESTORE/roles.err" \
  || { grep -v NOTICE "$TMPDIR_RESTORE/roles.err"; echo "Roles step failed"; exit 1; }

say "Login service tables ($AUTH_IMAGE)"
if ! out=$(docker run --rm -e GOTRUE_DB_DRIVER=postgres \
      -e "DATABASE_URL=postgres://supabase_auth_admin:$AUTH_PW@$HOST:5432/$DB?sslmode=require" \
      -e GOTRUE_JWT_SECRET="$(kv jwt-secret)" -e API_EXTERNAL_URL=http://localhost -e GOTRUE_SITE_URL=http://localhost \
      "$AUTH_IMAGE" auth migrate 2>&1) || grep -qE '"level":"(fatal|error)"' <<<"$out"; then
  grep -E '"level":"(fatal|error)"' <<<"$out" | cut -c1-500
  echo "Login migrations failed"
  exit 1
fi

say "User accounts, then their login identities"
for t in users identities; do
  PGPASSWORD=$AUTH_PW "$BIN/pg_restore.exe" --data-only --schema=auth --table=$t --no-owner --no-privileges \
    --exit-on-error -d "host=$HOST dbname=$DB user=supabase_auth_admin sslmode=require" "$DUMP"
done

say "App tables, data, rules and policies"
PGPASSWORD=$ADMIN_PW "$BIN/pg_restore.exe" --schema=public --no-owner --no-privileges --exit-on-error \
  -d "host=$HOST dbname=$DB user=zaconadmin sslmode=require" "$DUMP"

say "Grants and the new-user trigger"
admin "$DB" -q -v ON_ERROR_STOP=1 -f "$HERE/02_after_restore.sql" 2>"$TMPDIR_RESTORE/after.err" \
  || { grep -v NOTICE "$TMPDIR_RESTORE/after.err"; echo "After-restore step failed"; exit 1; }

say "What arrived"
admin "$DB" -At -F ' ' <<'SQL'
select 'statements', count(*) from statements
union all select 'payments', count(*) from payments
union all select 'market_stock', count(*) from market_stock
union all select 'technofresh_days', count(*) from technofresh_days
union all select 'profiles', count(*) from profiles
union all select 'policies', count(*) from pg_policies where schemaname = 'public'
union all select 'tables with RLS', count(*) from pg_tables where schemaname = 'public' and rowsecurity
union all select 'triggers', count(*) from pg_trigger where not tgisinternal
  and tgname in ('on_auth_user_created', 'profiles_block_self_promotion');
set role supabase_auth_admin;
select 'auth.users', count(*) from auth.users;
SQL
unset ADMIN_PW AUTH_PW
