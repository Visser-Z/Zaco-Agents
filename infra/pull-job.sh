#!/usr/bin/env bash
# The 08:00 TechnoFresh pull as a Container Apps job (replaces Vercel Cron).
#
#   bash infra/pull-job.sh manual      create or keep it, run only when started by hand
#   bash infra/pull-job.sh schedule    run every day at 06:00 UTC (08:00 SAST)
#   bash infra/pull-job.sh run         start one run now and show its output
#
# It calls the API inside the environment (http://zacon-api) with the
# CRON_SECRET from Key Vault, the same request Vercel Cron makes.
set -euo pipefail
export MSYS_NO_PATHCONV=1

MODE=${1:?usage: pull-job.sh manual|schedule|run}
RG=rg-zacon
CAE=cae-zacon
KV=kv-zacon-za1
ID=id-zacon
JOB=zacon-pull
IMAGE=curlimages/curl:8.11.1

ID_RES=$(az identity show -n "$ID" -g "$RG" --query id -o tsv)
KV_URI=$(az keyvault show -n "$KV" -g "$RG" --query properties.vaultUri -o tsv)
CMD='curl -sS --fail-with-body -m 600 -H "Authorization: Bearer $CRON_SECRET" http://zacon-api/api/technofresh/cron; echo'

create() {
  az containerapp job create -n "$JOB" -g "$RG" --environment "$CAE" --image "$IMAGE" \
    --user-assigned "$ID_RES" --trigger-type "$1" ${2:+--cron-expression "$2"} \
    --replica-timeout 900 --replica-retry-limit 1 --parallelism 1 --replica-completion-count 1 \
    --cpu 0.25 --memory 0.5Gi \
    --secrets "cron-secret=keyvaultref:${KV_URI}secrets/cron-secret,identityref:$ID_RES" \
    --env-vars CRON_SECRET=secretref:cron-secret -o none
  set_command
}

# The CLI joins --args into one string, so the command is set through the API
# as a proper list: /bin/sh -c "<curl ...>".
set_command() {
  local id cmd body
  id=$(az containerapp job show -n "$JOB" -g "$RG" --query id -o tsv)
  cmd=$(printf '%s' "$CMD" | sed 's/\\/\\\\/g; s/"/\\"/g')
  body="{\"properties\":{\"template\":{\"containers\":[{\"name\":\"$JOB\",\"image\":\"$IMAGE\",
    \"command\":[\"/bin/sh\",\"-c\",\"$cmd\"],
    \"env\":[{\"name\":\"CRON_SECRET\",\"secretRef\":\"cron-secret\"}],
    \"resources\":{\"cpu\":0.25,\"memory\":\"0.5Gi\"}}]}}}"
  az rest --method patch --url "https://management.azure.com${id}?api-version=2024-03-01" --body "$body" -o none
}

exists() { az containerapp job show -n "$JOB" -g "$RG" -o none 2>/dev/null; }

case "$MODE" in
  manual)
    exists || create Manual
    echo "$JOB exists; runs only when started by hand."
    ;;
  schedule)
    if exists; then az containerapp job delete -n "$JOB" -g "$RG" --yes -o none; fi
    create Schedule "0 6 * * *"
    echo "$JOB now runs every day at 06:00 UTC (08:00 SAST)."
    ;;
  run)
    exec=$(az containerapp job start -n "$JOB" -g "$RG" --query name -o tsv)
    echo "Started $exec; waiting for it to finish..."
    for i in $(seq 1 90); do
      status=$(az containerapp job execution show -n "$JOB" -g "$RG" --job-execution-name "$exec" --query properties.status -o tsv)
      case "$status" in Succeeded|Failed|Stopped|Degraded) break;; esac
      sleep 10
    done
    echo "Status: $status"
    az containerapp job logs show -n "$JOB" -g "$RG" --execution "$exec" --container "$JOB" --format text 2>/dev/null | tail -20 || true
    ;;
  *) echo "usage: pull-job.sh manual|schedule|run"; exit 1;;
esac
