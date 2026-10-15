#!/usr/bin/env bash
# The four container apps, after infra/setup.sh and the database restore.
# Safe to run again: existing apps are updated to the given images.
#
#   bash infra/deploy-apps.sh <api-image-tag>
#
#   zacon-gw    public. One address for everything (infra/gateway/Caddyfile)
#   zacon-api   internal. The FastAPI app and frontend (Dockerfile)
#   zacon-auth  internal. supabase/auth, the login service
#   zacon-rest  internal. PostgREST, the data service
#
# Secrets live in Key Vault and reach the apps as Key Vault references through
# one user-assigned identity, which may also pull from the registry. Nothing
# secret is printed or written to disk.
set -euo pipefail
export MSYS_NO_PATHCONV=1

API_TAG=${1:?usage: deploy-apps.sh <api-image-tag>}
RG=rg-zacon
KV=kv-zacon-za1
PG_HOST=pg-zacon-za1.postgres.database.azure.com
DB_NAME=${DB_NAME:-zacon_live}
ACR=acrzaconza1
CAE=cae-zacon
ID=id-zacon
AUTH_IMAGE=supabase/auth:v2.197.0
REST_IMAGE=postgrest/postgrest:v14.18
GW_IMAGE=$ACR.azurecr.io/zacon-gateway:1
API_IMAGE=$ACR.azurecr.io/zacon-api:$API_TAG

DOMAIN=$(az containerapp env show -n "$CAE" -g "$RG" --query properties.defaultDomain -o tsv)
PUBLIC_URL=${PUBLIC_URL:-https://zacodasboard.co.za}
KV_URI=$(az keyvault show -n "$KV" -g "$RG" --query properties.vaultUri -o tsv)

say() { printf '\n== %s\n' "$*"; }
kv() { az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv; }
kvset() { az keyvault secret set --vault-name "$KV" -n "$1" --value "$2" -o none; }
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

say "Derived secrets in Key Vault"
kvset auth-database-url "postgres://supabase_auth_admin:$(kv pg-auth-admin-password)@$PG_HOST:5432/$DB_NAME?sslmode=require"
kvset rest-db-uri "postgres://authenticator:$(kv pg-authenticator-password)@$PG_HOST:5432/$DB_NAME?sslmode=require"
if ! az keyvault secret show --vault-name "$KV" -n anon-key -o none 2>/dev/null; then
  now=$(date +%s)
  h=$(printf '{"alg":"HS256","typ":"JWT"}' | b64url)
  p=$(printf '{"role":"anon","iss":"zacon","iat":%s,"exp":%s}' "$now" "$((now + 10*365*86400))" | b64url)
  s=$(printf '%s.%s' "$h" "$p" | openssl dgst -sha256 -hmac "$(kv jwt-secret)" -binary | b64url)
  kvset anon-key "$h.$p.$s"
fi
if ! az keyvault secret show --vault-name "$KV" -n cron-secret -o none 2>/dev/null; then
  kvset cron-secret "$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | head -c 40)"
fi

say "Identity $ID (reads Key Vault secrets, pulls images)"
if ! az identity show -n "$ID" -g "$RG" -o none 2>/dev/null; then
  az identity create -n "$ID" -g "$RG" -o none
fi
ID_RES=$(az identity show -n "$ID" -g "$RG" --query id -o tsv)
ID_PRINCIPAL=$(az identity show -n "$ID" -g "$RG" --query principalId -o tsv)
grant() {
  if [ -z "$(az role assignment list --assignee "$ID_PRINCIPAL" --role "$1" --scope "$2" --query '[].id' -o tsv)" ]; then
    az role assignment create --assignee-object-id "$ID_PRINCIPAL" --assignee-principal-type ServicePrincipal \
      --role "$1" --scope "$2" -o none
  fi
}
grant "Key Vault Secrets User" "$(az keyvault show -n "$KV" -g "$RG" --query id -o tsv)"
grant "AcrPull" "$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)"

ref() { echo "$1=keyvaultref:${KV_URI}secrets/$2,identityref:$ID_RES"; }

# create_or_update NAME IMAGE INGRESS PORT CPU MEM "SECRETS..." "ENV..."
deploy() {
  local name=$1 image=$2 ingress=$3 port=$4 cpu=$5 mem=$6 secrets=$7 envs=$8
  if az containerapp show -n "$name" -g "$RG" -o none 2>/dev/null; then
    [ -n "$secrets" ] && az containerapp secret set -n "$name" -g "$RG" --secrets $secrets -o none
    az containerapp update -n "$name" -g "$RG" --image "$image" --cpu "$cpu" --memory "$mem" \
      --replace-env-vars $envs -o none
  else
    az containerapp create -n "$name" -g "$RG" --environment "$CAE" --image "$image" \
      --user-assigned "$ID_RES" --registry-server "$ACR.azurecr.io" --registry-identity "$ID_RES" \
      --ingress "$ingress" --target-port "$port" --transport http \
      --cpu "$cpu" --memory "$mem" --min-replicas 1 --max-replicas 2 \
      ${secrets:+--secrets $secrets} --env-vars $envs -o none
  fi
}

say "zacon-auth ($AUTH_IMAGE)"
# Invite and password reset emails go through Azure Communication Services
# (acs-zacon, data in Africa) from DoNotReply@zacodasboard.co.za.
AUTH_SECRETS="$(ref db-url auth-database-url) $(ref jwt-secret jwt-secret)"
AUTH_SMTP=""
if az keyvault secret show --vault-name "$KV" -n smtp-password -o none 2>/dev/null; then
  AUTH_SECRETS="$AUTH_SECRETS $(ref smtp-user smtp-username) $(ref smtp-pass smtp-password)"
  AUTH_SMTP="GOTRUE_SMTP_HOST=smtp.azurecomm.net GOTRUE_SMTP_PORT=587 GOTRUE_SMTP_USER=secretref:smtp-user \
   GOTRUE_SMTP_PASS=secretref:smtp-pass GOTRUE_SMTP_ADMIN_EMAIL=DoNotReply@zacodasboard.co.za \
   GOTRUE_SMTP_SENDER_NAME=ZacoAgents GOTRUE_SMTP_MAX_FREQUENCY=60s"
  echo "  email through Azure Communication Services"
fi
deploy zacon-auth "$AUTH_IMAGE" internal 9999 0.25 0.5Gi "$AUTH_SECRETS" \
  "$AUTH_SMTP GOTRUE_API_HOST=0.0.0.0 PORT=9999 GOTRUE_DB_DRIVER=postgres DATABASE_URL=secretref:db-url \
   API_EXTERNAL_URL=$PUBLIC_URL/auth/v1 GOTRUE_SITE_URL=$PUBLIC_URL \
   GOTRUE_JWT_SECRET=secretref:jwt-secret GOTRUE_JWT_EXP=3600 GOTRUE_JWT_AUD=authenticated \
   GOTRUE_JWT_DEFAULT_GROUP_NAME=authenticated GOTRUE_JWT_ADMIN_ROLES=service_role \
   GOTRUE_DISABLE_SIGNUP=true GOTRUE_EXTERNAL_EMAIL_ENABLED=true GOTRUE_MAILER_AUTOCONFIRM=false \
   GOTRUE_EXTERNAL_PHONE_ENABLED=false GOTRUE_LOG_LEVEL=info"

say "zacon-rest ($REST_IMAGE)"
deploy zacon-rest "$REST_IMAGE" internal 3000 0.25 0.5Gi \
  "$(ref db-uri rest-db-uri) $(ref jwt-secret jwt-secret)" \
  "PGRST_DB_URI=secretref:db-uri PGRST_DB_SCHEMAS=public PGRST_DB_ANON_ROLE=anon \
   PGRST_JWT_SECRET=secretref:jwt-secret PGRST_SERVER_PORT=3000 PGRST_DB_POOL=10 \
   PGRST_DB_CHANNEL_ENABLED=true PGRST_LOG_LEVEL=warn"

say "zacon-api ($API_IMAGE)"
# The app's own keys (infra/set-secrets.ps1) are passed only once they are in
# Key Vault: a reference to a missing secret would stop the app starting.
API_SECRETS="$(ref anon-key anon-key) $(ref jwt-secret jwt-secret) $(ref cron-secret cron-secret)"
API_ENV="SUPABASE_URL=$PUBLIC_URL SUPABASE_ANON_KEY=secretref:anon-key SUPABASE_JWT_SECRET=secretref:jwt-secret \
  CRON_SECRET=secretref:cron-secret ZACON_AUTH_REQUIRED=true ZACON_BUILD_SHA=$API_TAG"
HAVE=$(az keyvault secret list --vault-name "$KV" --query "[].name" -o tsv | tr -d '\r')
for pair in anthropic-key-intel:ANTHROPIC_API_KEY_INTEL anthropic-key-docs:ANTHROPIC_API_KEY_DOCS \
            anthropic-key:ANTHROPIC_API_KEY technofresh-key:TECHNOFRESH_KEY \
            robot-email:ZACON_ROBOT_EMAIL robot-password:ZACON_ROBOT_PASSWORD; do
  name=${pair%%:*}; var=${pair##*:}
  if grep -qx "$name" <<<"$HAVE"; then
    API_SECRETS="$API_SECRETS $(ref "$name" "$name")"
    API_ENV="$API_ENV $var=secretref:$name"
    echo "  $var from Key Vault"
  else
    echo "  $var not in Key Vault yet, left out"
  fi
done
deploy zacon-api "$API_IMAGE" internal 8000 0.5 1Gi "$API_SECRETS" "$API_ENV"

say "zacon-gw ($GW_IMAGE)"
deploy zacon-gw "$GW_IMAGE" external 8080 0.25 0.5Gi "" "GATEWAY=caddy"

say "Done"
echo "Public address: $PUBLIC_URL"
az containerapp list -g "$RG" --query "[].{name:name, status:properties.runningStatus, ingress:properties.configuration.ingress.external}" -o table
