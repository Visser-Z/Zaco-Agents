#!/usr/bin/env bash
# Azure foundation for ZacoAgents in South Africa North. Safe to run again:
# every step skips what already exists. Needs `az login` first.
#
#   bash infra/setup.sh
#
# Creates: resource group, monthly budget alert, Key Vault, PostgreSQL
# Flexible Server (B1ms, Postgres 17), container registry, Log Analytics and a
# Container Apps environment. The database admin password is generated here
# and written straight to Key Vault; it is never printed.
set -euo pipefail
# Git Bash on Windows rewrites arguments starting with "/" as file paths, which
# mangles Azure resource ids like /subscriptions/...
export MSYS_NO_PATHCONV=1

LOCATION=southafricanorth
RG=rg-zacon
SUFFIX=za1
KV=kv-zacon-$SUFFIX
PG=pg-zacon-$SUFFIX
ACR=acrzacon$SUFFIX
LAW=log-zacon
CAE=cae-zacon
PG_ADMIN=zaconadmin
BUDGET_USD=90                     # about R 1 500 at R 16,45 to the dollar
ALERT_EMAIL=${ALERT_EMAIL:-$(az account show --query user.name -o tsv)}

SUB=$(az account show --query id -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv)

say() { printf '\n== %s\n' "$*"; }

say "Registering resource providers"
for p in Microsoft.App Microsoft.ContainerRegistry Microsoft.OperationalInsights Microsoft.KeyVault Microsoft.DBforPostgreSQL; do
  az provider register -n "$p" --wait >/dev/null
done

say "Resource group $RG"
az group create -n "$RG" -l "$LOCATION" --tags app=zacon env=prod -o none

say "Budget alert: USD $BUDGET_USD a month to $ALERT_EMAIL"
az rest --method put \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Consumption/budgets/zacon-monthly?api-version=2023-05-01" \
  --body "{
    \"properties\": {
      \"category\": \"Cost\", \"amount\": $BUDGET_USD, \"timeGrain\": \"Monthly\",
      \"timePeriod\": {\"startDate\": \"$(date -u +%Y-%m-01)T00:00:00Z\", \"endDate\": \"2030-12-31T00:00:00Z\"},
      \"notifications\": {
        \"actual80\":    {\"enabled\": true, \"operator\": \"GreaterThan\", \"threshold\": 80,  \"thresholdType\": \"Actual\",     \"contactEmails\": [\"$ALERT_EMAIL\"]},
        \"actual100\":   {\"enabled\": true, \"operator\": \"GreaterThan\", \"threshold\": 100, \"thresholdType\": \"Actual\",     \"contactEmails\": [\"$ALERT_EMAIL\"]},
        \"forecast100\": {\"enabled\": true, \"operator\": \"GreaterThan\", \"threshold\": 100, \"thresholdType\": \"Forecasted\", \"contactEmails\": [\"$ALERT_EMAIL\"]}
      }
    }
  }" -o none

say "Key Vault $KV"
if ! az keyvault show -n "$KV" -g "$RG" -o none 2>/dev/null; then
  az keyvault create -n "$KV" -g "$RG" -l "$LOCATION" --enable-rbac-authorization true -o none
fi
KV_ID=$(az keyvault show -n "$KV" -g "$RG" --query id -o tsv)
if [ -z "$(az role assignment list --assignee "$ME" --role "Key Vault Secrets Officer" --scope "$KV_ID" --query '[].id' -o tsv)" ]; then
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$KV_ID" -o none
fi

say "PostgreSQL $PG (Burstable B1ms, Postgres 17, 32 GB, 35 day backups)"
if ! az postgres flexible-server show -n "$PG" -g "$RG" -o none 2>/dev/null; then
  PG_PASS=$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | head -c 32)
  # Role assignments take a moment to apply; retry the first write.
  saved=
  for i in 1 2 3 4 5 6 7 8; do
    az keyvault secret set --vault-name "$KV" -n pg-admin-password --value "$PG_PASS" -o none 2>/dev/null && { saved=1; break; }
    sleep 15
  done
  [ -n "$saved" ] || { echo "ERROR: could not save the database password to Key Vault; stopping before creating the server."; exit 1; }
  az postgres flexible-server create -n "$PG" -g "$RG" -l "$LOCATION" \
    --tier Burstable --sku-name Standard_B1ms --version 17 --storage-size 32 \
    --backup-retention 35 --admin-user "$PG_ADMIN" --admin-password "$PG_PASS" \
    --public-access 0.0.0.0 --zonal-resiliency Disabled --tags app=zacon -o none --yes
  unset PG_PASS
fi
az postgres flexible-server parameter set -g "$RG" -s "$PG" -n azure.extensions \
  --value "PGCRYPTO,UUID-OSSP,PG_STAT_STATEMENTS" -o none
# --public-access 0.0.0.0 above creates the 'azure-access' rule: Azure services
# (the container apps) may connect; everything else is refused.

say "Container registry $ACR (Basic)"
if ! az acr show -n "$ACR" -g "$RG" -o none 2>/dev/null; then
  az acr create -n "$ACR" -g "$RG" -l "$LOCATION" --sku Basic -o none
fi

say "Log Analytics $LAW and Container Apps environment $CAE"
if ! az monitor log-analytics workspace show -g "$RG" -n "$LAW" -o none 2>/dev/null; then
  az monitor log-analytics workspace create -g "$RG" -n "$LAW" -l "$LOCATION" --retention-time 30 -o none
fi
LAW_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$LAW" --query primarySharedKey -o tsv)
if ! az containerapp env show -n "$CAE" -g "$RG" -o none 2>/dev/null; then
  az containerapp env create -n "$CAE" -g "$RG" -l "$LOCATION" \
    --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY" -o none
fi
unset LAW_KEY

say "Done"
az resource list -g "$RG" --query "[].{name:name, type:type}" -o table
