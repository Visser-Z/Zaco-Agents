#!/usr/bin/env bash
# Email a password reset link to one user. The link signs them in and the app
# asks for a new password. There is no "forgot password" button, so an admin
# runs this for them.
#
#   bash infra/send-reset.sh person@example.com
#
# The login service answers the same whether or not the address has an
# account, so a typo is not reported here; check the address first.
set -euo pipefail

EMAIL=${1:?usage: send-reset.sh <email>}
URL=${PUBLIC_URL:-https://zacon-gw.happyground-50582879.southafricanorth.azurecontainerapps.io}
KEY=$(curl -s "$URL/api/health" | grep -oE '"supabase_anon_key":"[^"]*"' | cut -d'"' -f4)

code=$(curl -s -o /tmp/send-reset.out -w '%{http_code}' "$URL/auth/v1/recover" \
  -H "apikey: $KEY" -H "Content-Type: application/json" \
  -d "{\"email\": \"$EMAIL\"}")
if [ "$code" = "200" ]; then
  echo "Reset email sent to $EMAIL from DoNotReply@zacodasboard.co.za."
else
  echo "The login service answered HTTP $code:"; cat /tmp/send-reset.out; echo
  exit 1
fi
