#!/usr/bin/env bash
#
# Build a Hetzner Cloud snapshot with Docker + pre-pulled images.
#
# Usage: ./build-snapshot.sh [HETZNER_API_TOKEN]
#
# Requirements: hcloud CLI installed and authenticated, or pass token as arg.
#
set -euo pipefail

TOKEN="${1:-${HETZNER_TOKEN:-}}"
if [ -z "$TOKEN" ]; then
    echo "Usage: $0 <hetzner-api-token>"
    echo "Or set HETZNER_TOKEN env var"
    exit 1
fi

export HCLOUD_TOKEN="$TOKEN"

LOCATION="fsn1"
SERVER_TYPE="cx22"
SERVER_NAME="snapshot-builder-$(date +%s)"
IMAGE="ubuntu-24.04"

echo "==> Creating temporary VM: $SERVER_NAME"
hcloud server create \
    --name "$SERVER_NAME" \
    --type "$SERVER_TYPE" \
    --image "$IMAGE" \
    --location "$LOCATION" \
    --ssh-key preview-manager

SERVER_IP=$(hcloud server ip "$SERVER_NAME")
echo "==> VM created: $SERVER_IP"

# Wait for SSH
echo "==> Waiting for SSH..."
for i in $(seq 1 30); do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@"$SERVER_IP" true 2>/dev/null; then
        break
    fi
    sleep 2
done

echo "==> Installing Docker + tools"
ssh -o StrictHostKeyChecking=no root@"$SERVER_IP" bash <<'REMOTE'
set -euo pipefail

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Docker Compose plugin
apt-get install -y docker-compose-plugin

# Install AWS CLI (for S3 downloads)
apt-get install -y awscli

# Install basic tools
apt-get install -y pv git rsync

# Enable Docker
systemctl enable docker
systemctl start docker

# Pre-pull common images
echo "==> Pre-pulling images..."
docker pull preview-drupal:php8.3 || true
docker pull preview-drupal:php8.2 || true
docker pull preview-drupal:php8.1 || true
docker pull mysql:8.0
docker pull mysql:8.4 || true
docker pull mariadb:10.6 || true
docker pull mariadb:11.4 || true
docker pull redis:7-alpine
docker pull valkey/valkey:8-alpine
docker pull solr:9
docker pull alpine:3.20

# Clean up apt cache
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "==> Setup complete"
REMOTE

echo "==> Shutting down VM for snapshot..."
hcloud server shutdown "$SERVER_NAME"

# Wait for shutdown
for i in $(seq 1 20); do
    STATUS=$(hcloud server describe "$SERVER_NAME" -o format='{{.Status}}')
    if [ "$STATUS" = "off" ]; then
        break
    fi
    sleep 2
done

echo "==> Creating snapshot..."
SNAPSHOT_DESC="preview-vm-$(date +%Y%m%d-%H%M)"
hcloud server create-image --type snapshot --description "$SNAPSHOT_DESC" "$SERVER_NAME"

echo "==> Snapshot created: $SNAPSHOT_DESC"
echo "==> Cleaning up temporary VM..."
hcloud server delete "$SERVER_NAME"

echo ""
echo "==> Done! Update HETZNER_SNAPSHOT_ID in your .env with the snapshot ID."
echo "    List snapshots: hcloud image list --type snapshot"
