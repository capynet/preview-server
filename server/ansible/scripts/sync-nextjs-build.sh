#!/bin/bash
# Script to sync Next.js standalone build to server

set -e

DASHBOARD_BUILD_DIR="/home/capy/www/previews/dashboard/.next/standalone"
REMOTE_HOST="$1"
REMOTE_DEST="/var/www/dashboard"

if [ -z "$REMOTE_HOST" ]; then
    echo "Usage: $0 <remote_host>"
    exit 1
fi

echo "Syncing Next.js standalone build to $REMOTE_HOST..."

# Sync .next folder from standalone build
echo "Copying .next directory..."
rsync -av --delete \
    "${DASHBOARD_BUILD_DIR}/.next/" \
    "root@${REMOTE_HOST}:${REMOTE_DEST}/.next/"

echo "✓ Next.js build synced successfully"
