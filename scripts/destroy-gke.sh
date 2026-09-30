#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=${PROJECT_ID:?Set PROJECT_ID to the GCP project to clean up}
ZONE=${ZONE:-us-east1-d}
CLUSTER=${CLUSTER:-inference-demo}

if [[ ${CONFIRM_DELETE:-} != "$CLUSTER" ]]; then
  echo "Set CONFIRM_DELETE=$CLUSTER to delete this cluster and its node pools. Review persistent disks separately." >&2
  exit 1
fi
gcloud container clusters delete "$CLUSTER" --project "$PROJECT_ID" --zone "$ZONE" --quiet