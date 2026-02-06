#!/bin/bash
set -e

echo "=================================================="
echo "02 - Syncing Repository"
echo "=================================================="

# Ensure preview directory exists
mkdir -p "$PREVIEW_DIR"

# If CI_PROJECT_DIR is provided (from GitLab runner), copy from there
if [ -n "$CI_PROJECT_DIR" ] && [ -d "$CI_PROJECT_DIR" ]; then
    echo "Copying from GitLab runner workspace: $CI_PROJECT_DIR"
    
    # Use rsync to copy files
    rsync -av \
        --exclude='.git' \
        --exclude='node_modules' \
        --exclude='vendor' \
        --exclude='.ddev/.dbimageBuild' \
        --exclude='.ddev/.webimageBuild' \
        "$CI_PROJECT_DIR/" "$PREVIEW_DIR/"
    
    echo "✓ Repository synced from runner"
else
    # Clone from git repository
    echo "Cloning from repository: $CI_REPOSITORY_URL"
    
    if [ -d "$PREVIEW_DIR/.git" ]; then
        # Repository already exists, fetch and checkout
        cd "$PREVIEW_DIR"
        git fetch origin
        git checkout "$CI_COMMIT_SHA"
        echo "✓ Repository updated to commit $CI_COMMIT_SHA"
    else
        # Clone fresh
        git clone "$CI_REPOSITORY_URL" "$PREVIEW_DIR"
        cd "$PREVIEW_DIR"
        git checkout "$CI_COMMIT_SHA"
        echo "✓ Repository cloned and checked out to $CI_COMMIT_SHA"
    fi
fi

echo ""
echo "Repository sync completed"
echo ""
