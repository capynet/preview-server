#!/usr/bin/env bash
# Build CLI binaries for all platforms.
# Usage:
#   ./build.sh          # auto-bump patch: 1.3.1 → 1.3.2
#   ./build.sh 2.0.0    # set explicit version

set -euo pipefail
cd "$(dirname "$0")"

CURRENT=$(cat VERSION)

if [ $# -ge 1 ]; then
    VERSION="$1"
else
    # Auto-bump patch
    IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"
    PATCH=$((PATCH + 1))
    VERSION="${MAJOR}.${MINOR}.${PATCH}"
fi

echo "$VERSION" > VERSION
echo "$VERSION" > dist/VERSION

echo "Building CLI v${VERSION} (was ${CURRENT})"

PLATFORMS=(
    "linux/amd64"
    "linux/arm64"
    "darwin/amd64"
    "darwin/arm64"
)

for PLATFORM in "${PLATFORMS[@]}"; do
    OS="${PLATFORM%/*}"
    ARCH="${PLATFORM#*/}"
    OUTPUT="dist/preview-${OS}-${ARCH}"
    echo "  → ${OS}/${ARCH}"
    GOOS=$OS GOARCH=$ARCH go build -o "$OUTPUT" .
done

echo ""
echo "Done! CLI v${VERSION} ready in dist/"
echo "Deploy with: cd ../server/ansible && ~/.local/bin/ansible-playbook -i inventory/hosts.yml playbooks/deploy-preview-manager.yml --tags cli"
