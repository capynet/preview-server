#!/bin/bash
set -e

echo "=================================================="
echo "05 - Importing Database"
echo "=================================================="

cd "$PREVIEW_DIR"

# Only import DB for NEW deployments
if [ "$PREVIEW_MODE" = "new" ]; then
    echo "New preview - importing database"
    
    if [ -n "$DB_SOURCE" ] && [ -f "$DB_SOURCE" ]; then
        echo "Importing database from: $DB_SOURCE"
        
        # Import database
        ddev import-db --src="$DB_SOURCE"
        
        echo "✓ Database imported successfully"
    else
        echo "No DB_SOURCE specified or file not found, skipping database import"
    fi
else
    echo "Existing preview - skipping database import"
    echo "Database will be preserved from previous deployment"
fi

echo ""
