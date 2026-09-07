#!/usr/bin/env bash
# Prove what a signed-in staff user may actually do, against a real Postgres.
#
# Row-level security cannot be checked from the app: PostgREST answers a delete
# that RLS refuses with 200 and an empty list, so "removed nothing" and "there
# was nothing to remove" look identical. That ambiguity cost several rounds of
# guessing, so this runs the real policies against a throwaway database and
# reports what a staff account can do before and after a migration.
#
# Usage:  scripts/check_rls.sh supabase/migrations/0019_no_admin_gate.sql
# Needs:  docker
set -euo pipefail
MIGRATION="${1:?usage: check_rls.sh <migration.sql>}"
NAME=zacon-rls-check
HERE="$(cd "$(dirname "$0")" && pwd)"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$NAME" -e POSTGRES_PASSWORD=test postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done

# The schema as it stands today: admin-gated UPDATE and DELETE, rows recorded
# by a different account from the one signing in, which is the real situation.
docker exec -i "$NAME" psql -U postgres -q < "$HERE/rls_fixture.sql"

as_staff() {
  docker exec -i "$NAME" psql -U postgres -At -c "
    set role authenticated;
    set local test.uid = '11111111-1111-1111-1111-111111111111';
    $1" | tail -1
}

echo "before $(basename "$MIGRATION"):"
echo "  statements visible : $(as_staff 'select count(*) from public.statements;')"
echo "  statements deleted : $(as_staff "with d as (delete from public.statements where last_sale between '2026-08-01' and '2026-08-31' returning 1) select count(*) from d;")"
echo "  statements left    : $(as_staff 'select count(*) from public.statements;')"

docker exec -i "$NAME" psql -U postgres -q < "$MIGRATION"

echo "after:"
echo "  statements deleted : $(as_staff "with d as (delete from public.statements where last_sale between '2026-08-01' and '2026-08-31' returning 1) select count(*) from d;")"
echo "  payments deleted   : $(as_staff "with d as (delete from public.payments where paid_on between '2026-08-01' and '2026-08-31' returning 1) select count(*) from d;")"
echo "  statements left    : $(as_staff 'select count(*) from public.statements;')"
echo "  anon still sees    : $(docker exec -i "$NAME" psql -U postgres -At -c "set role anon; select count(*) from public.statements;" | tail -1)"
