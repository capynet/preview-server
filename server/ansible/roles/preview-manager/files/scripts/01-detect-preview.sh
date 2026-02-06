#!/bin/bash
set -e

echo "=================================================="
echo "01 - Detecting Preview"
echo "=================================================="

echo "Preview Name: $PROJECT_NAME"
echo "Preview Directory: $PREVIEW_DIR"
echo "Preview URL: $PREVIEW_URL"
echo "MR ID: $CI_MERGE_REQUEST_IID"
echo "Branch: $CI_COMMIT_REF_NAME"
echo "Commit: $CI_COMMIT_SHA"

# Check if preview already exists
if [ -d "$PREVIEW_DIR/.ddev" ]; then
    echo ""
    echo "✓ Existing preview detected"
    echo "  This is an UPDATE deployment"
    export PREVIEW_MODE="update"
else
    echo ""
    echo "✓ New preview"
    echo "  This is a NEW deployment"
    export PREVIEW_MODE="new"
fi

echo ""
