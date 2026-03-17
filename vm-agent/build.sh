#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

REGISTRY="${REGISTRY:-91.99.157.66:5000}"
OUTPUT_DIR="../cli/dist"  # same dir as CLI binaries (copied by Ansible)

echo "Building preview-agent (linux/amd64)..."
mkdir -p "$OUTPUT_DIR"
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o "${OUTPUT_DIR}/preview-agent" .

echo "Done! Binary: ${OUTPUT_DIR}/preview-agent"
echo "Deploy with Ansible --tags cli"
