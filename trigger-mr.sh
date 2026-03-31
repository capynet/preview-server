#!/bin/bash
# Trigger a deploy for a GitLab MR via the webhook endpoint.
# Usage: ./trigger-mr.sh <mr_iid> [branch]
#
# Examples:
#   ./trigger-mr.sh 1584
#   ./trigger-mr.sh 1584 SDLMNT-4-Drupal-11-Upgrade

set -euo pipefail

MR_IID="${1:?Usage: $0 <mr_iid> [branch]}"

SERVER="root@91.99.157.66"
ENV_FILE="/home/preview-manager/www/previews/server/preview-manager/.env"
GITLAB_API="https://gitlab.dropsolid.com/api/v4"
PROJECT_ID=461
ORG_SLUG="dropsolid"

# Read secrets from server
WEBHOOK_SECRET=$(ssh "$SERVER" "grep GITLAB_WEBHOOK_SECRET $ENV_FILE | cut -d= -f2")
GITLAB_TOKEN=$(ssh "$SERVER" "grep SEED_ORG_GITLAB_TOKEN $ENV_FILE | cut -d= -f2")

# Get MR info from GitLab API
MR_JSON=$(ssh "$SERVER" "curl -sf --header 'PRIVATE-TOKEN: $GITLAB_TOKEN' '$GITLAB_API/projects/$PROJECT_ID/merge_requests/$MR_IID'")
SHA=$(echo "$MR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'])")
TITLE=$(echo "$MR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])")
SOURCE_BRANCH="${2:-$(echo "$MR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['source_branch'])")}"
TARGET_BRANCH=$(echo "$MR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['target_branch'])")

echo "MR: !$MR_IID — $TITLE"
echo "Branch: $SOURCE_BRANCH → $TARGET_BRANCH"
echo "Commit: $SHA"
echo ""

# Trigger webhook
RESULT=$(ssh "$SERVER" "curl -sf -X POST http://localhost:8000/api/webhooks/$ORG_SLUG/gitlab \
  -H 'Content-Type: application/json' \
  -H 'X-Gitlab-Token: $WEBHOOK_SECRET' \
  -H 'X-Gitlab-Event: Merge Request Hook' \
  -d '{
    \"object_kind\": \"merge_request\",
    \"project\": {\"id\": $PROJECT_ID},
    \"object_attributes\": {
      \"iid\": $MR_IID,
      \"action\": \"open\",
      \"state\": \"opened\",
      \"source_branch\": \"$SOURCE_BRANCH\",
      \"target_branch\": \"$TARGET_BRANCH\",
      \"title\": \"$TITLE\",
      \"last_commit\": {\"id\": \"$SHA\"}
    }
  }'")

echo "Result: $RESULT"
echo ""
echo "Track: https://druploy.dev/projects/project/soudal/mr-$MR_IID"
