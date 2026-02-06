#!/bin/bash
set -e

: "${PREVIEW_SERVER:?Variable PREVIEW_SERVER is not set}"
: "${PREVIEW_USER:?Variable PREVIEW_USER is not set}"
ARTIFACT_PATH="/tmp/artifact-${CI_PROJECT_NAME}-${CI_MERGE_REQUEST_IID}.tar.gz"

tar czf "$ARTIFACT_PATH" .

scp "$ARTIFACT_PATH" "${PREVIEW_USER}@${PREVIEW_SERVER}:${ARTIFACT_PATH}"

ssh "${PREVIEW_USER}@${PREVIEW_SERVER}" "bash -s" <<EOF
  export CI_PROJECT_NAME="${CI_PROJECT_NAME}"
  export CI_MERGE_REQUEST_IID="${CI_MERGE_REQUEST_IID}"
  export CI_COMMIT_REF_NAME="${CI_COMMIT_REF_NAME}"
  export CI_COMMIT_SHA="${CI_COMMIT_SHA}"
  /var/www/preview-manager/scripts/receive-artifact.sh ${ARTIFACT_PATH}
EOF
