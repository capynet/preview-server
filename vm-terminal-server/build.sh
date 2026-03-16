#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

REGISTRY="${REGISTRY:-91.99.157.66:5000}"
IMAGE="vm-terminal-server"

echo "Building ${IMAGE}..."
docker build -t ${IMAGE}:latest .

echo "Tagging for registry ${REGISTRY}..."
docker tag ${IMAGE}:latest ${REGISTRY}/${IMAGE}:latest

echo "Pushing to ${REGISTRY}..."
docker push ${REGISTRY}/${IMAGE}:latest

echo "Done! Image: ${REGISTRY}/${IMAGE}:latest"
