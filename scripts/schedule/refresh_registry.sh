#!/bin/sh
# Scheduled column-registry refresh (BigQuery). Run by launchd
# (com.costguard.registry-refresh.plist) or by hand. Mode "stale": only
# tables whose profiles are older than customer.yaml column_registry
# max_age_days are re-profiled, so most runs read the catalog only (free API
# calls) and bill nothing; a full re-profile of TheLook bills ~668 MiB
# (dry-run measured 2026-10-18; $0.004 MODELED). Every run is recorded in the
# registry file (registry_status / the MCP tool shows it).
set -eu
cd "$(dirname "$0")/../.."
set -a
. ./.env
set +a
mkdir -p runs
exec .venv/bin/python scripts/build_column_registry.py \
    --bigquery-project "$GCP_PROJECT" --dataset cost_guard_thelook --mode stale \
    >> runs/registry_refresh.log 2>&1
