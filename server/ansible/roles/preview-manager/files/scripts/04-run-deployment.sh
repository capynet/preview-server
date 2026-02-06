#!/bin/bash
set -e

echo "=================================================="
echo "04 - Running Deployment"
echo "=================================================="

cd "$PREVIEW_DIR"

# Start DDEV
echo "Starting DDEV..."
ddev start

echo "✓ DDEV started successfully"

# Run composer install if composer.json exists
if [ -f "composer.json" ]; then
    echo "Running composer install..."
    ddev composer install --no-interaction
    echo "✓ Composer dependencies installed"
fi

# Clear Drupal cache if drush is available
if ddev drush version &> /dev/null; then
    echo "Clearing Drupal cache..."
    ddev drush cache:rebuild
    echo "✓ Cache cleared"
fi

# Run database updates if needed
if ddev drush version &> /dev/null; then
    echo "Running database updates..."
    ddev drush updatedb -y || echo "No database updates needed"
    echo "✓ Database updates completed"
fi

# Import configuration if config exists
if [ -d "${DOCROOT:-web}/sites/default/files/config" ] || [ -d "config/sync" ]; then
    echo "Importing configuration..."
    ddev drush config:import -y || echo "No configuration to import"
    echo "✓ Configuration imported"
fi

echo ""
echo "Deployment completed successfully"
echo ""
