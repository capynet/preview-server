#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Building druploy-agent (linux/amd64)..."
mkdir -p bin
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o bin/vm-agent .

echo "Done! Binary: bin/vm-agent"
echo "Deploy with: cd ../server/ansible && ansible-playbook -i inventory/hosts.yml playbooks/deploy-preview-manager.yml --tags code"
