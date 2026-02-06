#!/bin/bash
set -e

echo "=================================================="
echo "03 - Configuring DDEV"
echo "=================================================="

cd "$PREVIEW_DIR"

# Check if .ddev/config.yaml exists
if [ ! -f ".ddev/config.yaml" ]; then
    echo "No .ddev/config.yaml found"
    echo "Initializing DDEV for Drupal..."
    
    # Initialize DDEV for Drupal
    ddev config \
        --project-type=drupal \
        --project-name="$PROJECT_NAME" \
        --docroot="${DOCROOT:-web}" \
        --php-version="${PHP_VERSION:-8.2}" \
        --database="${DB_VERSION:-mysql:8.0}"
    
    echo "✓ DDEV configured"
else
    echo "✓ DDEV config already exists"
fi

# Configure additional URLs if needed
# ddev config --additional-fqdns="$PREVIEW_URL"

echo ""
echo "DDEV configuration completed"
echo ""
