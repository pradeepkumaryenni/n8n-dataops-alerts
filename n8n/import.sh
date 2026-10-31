#!/bin/sh
# One-shot bootstrap, run by the `n8n-import` compose service before n8n starts:
#   1. render credentials from env vars and import them (encrypted with N8N_ENCRYPTION_KEY)
#   2. import every workflow in /opt/dataops/workflows (same IDs every time, so re-runs overwrite)
#   3. publish (activate) the router, the digest and the error-handler workflows
#      (n8n 2.x only calls an error workflow that is published)
set -eu

WORKFLOW_DIR=/opt/dataops/workflows
CRED_FILE=$(mktemp /tmp/credentials.XXXXXX)
trap 'rm -f "$CRED_FILE"' EXIT

echo "[import] rendering credentials from environment"
node /opt/dataops/n8n/render-credentials.js > "$CRED_FILE"
n8n import:credentials --input="$CRED_FILE"

echo "[import] importing workflows from $WORKFLOW_DIR"
n8n import:workflow --separate --input="$WORKFLOW_DIR"

for id in ${N8N_PUBLISH_WORKFLOW_IDS:-dataopsAlertRtr1 dataopsDigest001 dataopsErrorWf01}; do
  echo "[import] publishing workflow $id"
  n8n publish:workflow --id="$id"
done

echo "[import] done"
