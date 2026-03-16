#!/usr/bin/env bash
#
# Build a lightweight Hetzner Cloud snapshot with Docker (no pre-pulled images).
# Docker images are pulled on-demand during preview deployment.
#
# - Labels snapshots with purpose=preview-base-image for identification
# - Enables delete protection on the new (active) snapshot
# - Removes delete protection and deletes old preview-base-image snapshots
# - If the build fails, old snapshots are preserved
#
# Usage: ./build-snapshot.sh [HETZNER_API_TOKEN]
#
# Requirements: hcloud CLI installed and authenticated, or pass token as arg.
#
set -euo pipefail

SNAPSHOT_LABEL="purpose=preview-base-image"

TOKEN="${1:-${HETZNER_TOKEN:-}}"
if [ -z "$TOKEN" ]; then
    echo "Usage: $0 <hetzner-api-token>"
    echo "Or set HETZNER_TOKEN env var"
    exit 1
fi

export HCLOUD_TOKEN="$TOKEN"

LOCATION="fsn1"
SERVER_TYPE="cx23"
SERVER_NAME="snapshot-builder-$(date +%s)"
IMAGE="ubuntu-24.04"

# Collect existing preview-base-image snapshot IDs (only ours, not unrelated snapshots)
OLD_SNAPSHOT_IDS=$(hcloud image list --type snapshot --selector "$SNAPSHOT_LABEL" -o noheader -o columns=id 2>/dev/null | tr '\n' ' ')
if [ -n "$OLD_SNAPSHOT_IDS" ]; then
    echo "==> Found existing preview-base-image snapshots: $OLD_SNAPSHOT_IDS"
else
    echo "==> No existing preview-base-image snapshots found"
fi

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

# Install basic tools
apt-get install -y pv git rsync python3-pip
pip3 install awscli --break-system-packages

# Enable Docker
systemctl enable docker
systemctl start docker

# Configure insecure registry for private image pulls
echo '{"insecure-registries": ["91.99.157.66:5000"]}' > /etc/docker/daemon.json
systemctl restart docker

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
CREATE_OUTPUT=$(hcloud server create-image --type snapshot --description "$SNAPSHOT_DESC" --label "$SNAPSHOT_LABEL" "$SERVER_NAME" 2>&1)
echo "$CREATE_OUTPUT"

# Extract new snapshot ID
NEW_SNAPSHOT_ID=$(echo "$CREATE_OUTPUT" | grep -oP 'ID:\s*\K[0-9]+' | head -1)

if [ -z "$NEW_SNAPSHOT_ID" ]; then
    echo "==> ERROR: Could not determine new snapshot ID. Old snapshots preserved."
    echo "==> Cleaning up temporary VM..."
    hcloud server delete "$SERVER_NAME"
    exit 1
fi

echo "==> New snapshot created: ID=$NEW_SNAPSHOT_ID ($SNAPSHOT_DESC)"

# Protect the new snapshot against accidental deletion
echo "==> Enabling delete protection on new snapshot $NEW_SNAPSHOT_ID"
hcloud image enable-protection "$NEW_SNAPSHOT_ID" delete

# Clean up old preview-base-image snapshots
if [ -n "$OLD_SNAPSHOT_IDS" ]; then
    for OLD_ID in $OLD_SNAPSHOT_IDS; do
        echo "==> Removing protection and deleting old snapshot: $OLD_ID"
        hcloud image disable-protection "$OLD_ID" delete 2>/dev/null || true
        hcloud image delete "$OLD_ID" || echo "    Warning: could not delete snapshot $OLD_ID"
    done
fi

echo "==> Cleaning up temporary VM..."
hcloud server delete "$SERVER_NAME"

echo ""
echo "==> Done! New snapshot ID: $NEW_SNAPSHOT_ID"
echo "    Update HETZNER_SNAPSHOT_ID in your .env with this value."
