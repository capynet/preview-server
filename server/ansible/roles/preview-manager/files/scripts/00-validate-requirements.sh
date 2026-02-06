#!/bin/bash
set -e

echo "=================================================="
echo "00 - Validating Requirements"
echo "=================================================="

# Check DDEV is installed
if ! command -v ddev &> /dev/null; then
    echo "ERROR: DDEV is not installed"
    exit 1
fi

echo "✓ DDEV installed: $(ddev version --json-output | jq -r '.raw')"

# Check required environment variables
required_vars=(
    "PROJECT_NAME"
    "PREVIEW_DIR"
    "PREVIEW_URL"
    "CI_COMMIT_SHA"
    "CI_MERGE_REQUEST_IID"
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "ERROR: Required variable $var is not set"
        exit 1
    fi
    echo "✓ $var=${!var}"
done

echo ""
echo "All requirements validated successfully"
echo ""
