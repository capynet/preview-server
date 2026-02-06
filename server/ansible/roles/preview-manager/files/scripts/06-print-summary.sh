#!/bin/bash
set -e

echo "=================================================="
echo "06 - Deployment Summary"
echo "=================================================="

cd "$PREVIEW_DIR"

echo ""
echo "✅ Preview Environment Ready!"
echo ""
echo "📋 Details:"
echo "  Project: $PROJECT_NAME"
echo "  MR ID: $CI_MERGE_REQUEST_IID"
echo "  Branch: $CI_COMMIT_REF_NAME"
echo "  Commit: $CI_COMMIT_SHORT_SHA"
echo ""
echo "🔗 URLs:"
echo "  Preview URL: $PREVIEW_URL"

# Get DDEV URLs
if ddev describe &> /dev/null; then
    echo ""
    echo "  DDEV URLs:"
    ddev describe | grep -A 10 "URLs:"
fi

echo ""
echo "📁 Path: $PREVIEW_DIR"
echo ""
echo "🔧 Useful commands:"
echo "  cd $PREVIEW_DIR"
echo "  ddev ssh"
echo "  ddev drush status"
echo "  ddev logs"
echo ""
echo "=================================================="
echo ""
