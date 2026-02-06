#!/bin/bash
set -e

#############################################################################
# REBUILD PREVIEW (LIGHT REBUILD - DB + FILES ONLY)
#############################################################################
# This script is called from the preview-manager when a user clicks "Rebuild".
# It performs a lightweight rebuild that:
# - Keeps existing code and DDEV configuration
# - Only replaces database with base backup
# - Only replaces files with base backup
# - Runs drush deploy to apply updates
#
# This is much faster than a full rebuild and is ideal for:
# - Testing with fresh data
# - Resetting the preview to a known state
#
# USAGE:
#   ./rebuild-preview.sh /var/www/previews/drupal-test-2/mr-3
#############################################################################

# Argument: preview directory (e.g., /var/www/previews/drupal-test-2/mr-3)
PREVIEW_DIR="${1}"

if [ -z "$PREVIEW_DIR" ]; then
  echo "❌ ERROR: Preview directory not provided"
  echo "Usage: $0 <preview-directory>"
  echo "Example: $0 /var/www/previews/drupal-test-2/mr-3"
  exit 1
fi

if [ ! -d "$PREVIEW_DIR" ]; then
  echo "❌ ERROR: Preview directory does not exist: $PREVIEW_DIR"
  exit 1
fi

# Read .preview-info file
PREVIEW_INFO_FILE="${PREVIEW_DIR}/.preview-info"

if [ ! -f "$PREVIEW_INFO_FILE" ]; then
  echo "❌ ERROR: .preview-info file not found: $PREVIEW_INFO_FILE"
  echo "This preview may have been created before the rebuild feature was implemented."
  exit 1
fi

echo "📋 Reading preview info from: $PREVIEW_INFO_FILE"

# Parse .preview-info file
while IFS='=' read -r key value; do
  case "$key" in
    PROJECT)
      PROJECT_NAME="$value"
      ;;
  esac
done < "$PREVIEW_INFO_FILE"

# Validate project name
if [ -z "$PROJECT_NAME" ]; then
  echo "❌ ERROR: Missing PROJECT in .preview-info"
  exit 1
fi

# Derive other variables from the preview directory path
PREVIEW_NAME="$(basename "$PREVIEW_DIR")"
PROJECT_DIR_NAME="$(basename "$(dirname "$PREVIEW_DIR")")"

# Set environment variables
DEPLOY_DIR="$PREVIEW_DIR"
DDEV_PROJECT_NAME="${PROJECT_DIR_NAME}-${PREVIEW_NAME}"
CI_PROJECT_NAME="$PROJECT_DIR_NAME"

echo "✅ Environment configured:"
echo "   PROJECT: $PROJECT_NAME"
echo "   DEPLOY_DIR: $DEPLOY_DIR"
echo "   DDEV_PROJECT_NAME: $DDEV_PROJECT_NAME"
echo ""

#############################################################################
# VERIFY BASE FILES EXIST
#############################################################################

DB_BACKUP="/backups/${CI_PROJECT_NAME}-base.sql.gz"
FILES_BACKUP="/backups/${CI_PROJECT_NAME}-files.tar.gz"

echo "🔍 Verifying base files..."

MISSING_FILES=()

if [ ! -f "$DB_BACKUP" ]; then
  MISSING_FILES+=("database")
fi

if [ ! -f "$FILES_BACKUP" ]; then
  MISSING_FILES+=("files")
fi

if [ ${#MISSING_FILES[@]} -ne 0 ]; then
  echo "❌ ERROR: Missing base backup files:"
  echo ""
  for item in "${MISSING_FILES[@]}"; do
    if [ "$item" = "database" ]; then
      echo "  ✗ Database: $DB_BACKUP"
    else
      echo "  ✗ Files:    $FILES_BACKUP"
    fi
  done
  echo ""
  echo "You need to push them first using:"
  echo "  ddev push-to-preview-server"
  echo ""
  exit 1
fi

echo "✅ Base files verified"

#############################################################################
# VERIFY PREVIEW DIRECTORY EXISTS
#############################################################################

echo "✅ Preview directory exists: $DEPLOY_DIR"

# Change to preview directory
cd "$DEPLOY_DIR" || exit 1

#############################################################################
# ENSURE DDEV IS RUNNING
#############################################################################

echo "🔄 Checking DDEV status..."
if ! ddev status > /dev/null 2>&1; then
  echo "🔄 Starting DDEV..."
  ddev start
else
  echo "✅ DDEV is already running"
fi

#############################################################################
# IMPORT DATABASE
#############################################################################

echo "💾 Importing database from base backup..."
echo "   Source: $DB_BACKUP"
ddev drush sql:drop -y
gunzip -c "$DB_BACKUP" | ddev exec 'bash -c "$(drush sql:connect)"'
echo "✅ Database imported"

#############################################################################
# REPLACE FILES
#############################################################################

echo "📁 Replacing files directory with base backup..."
echo "   Source: $FILES_BACKUP"

# Remove existing files
rm -rf "${DEPLOY_DIR}/web/sites/default/files"

# Create directory
mkdir -p "${DEPLOY_DIR}/web/sites/default/files"

# Extract backup
tar -xzf "$FILES_BACKUP" -C "${DEPLOY_DIR}/web/sites/default/files"

echo "✅ Files replaced"

#############################################################################
# RUN DRUSH DEPLOY
#############################################################################

echo "⚙️  Running drush deploy..."
ddev drush deploy
echo "✅ Drush deploy completed"

#############################################################################
# FINAL MESSAGE
#############################################################################

echo ""
echo "✅ Light rebuild completed successfully!"
echo "   Preview: $DEPLOY_DIR"
echo "   Database and files have been reset to base backup"
echo ""
