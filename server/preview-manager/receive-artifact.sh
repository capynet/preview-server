#!/bin/bash
set -e

ARTIFACT_PATH="$1"

: "${CI_PROJECT_NAME:?Variable CI_PROJECT_NAME is not set}"
: "${CI_MERGE_REQUEST_IID:?Variable CI_MERGE_REQUEST_IID is not set}"

if [ -z "$ARTIFACT_PATH" ]; then
  echo "Usage: receive-artifact.sh <artifact-path>"
  exit 1
fi

if [ ! -f "$ARTIFACT_PATH" ]; then
  echo "Artifact not found: $ARTIFACT_PATH"
  exit 1
fi

#############################################################################
# VARIABLES
#############################################################################
PROJECT_NAME="${CI_PROJECT_NAME}"
DEPLOY_DIR="/var/www/previews/${PROJECT_NAME}/mr-${CI_MERGE_REQUEST_IID}"
DDEV_PROJECT_NAME="${PROJECT_NAME}-mr-${CI_MERGE_REQUEST_IID}"
DOMAIN="mr-${CI_MERGE_REQUEST_IID}.${PROJECT_NAME}.preview-mr.com"

echo "Project: $PROJECT_NAME"
echo "MR: $CI_MERGE_REQUEST_IID"
echo "Deploy dir: $DEPLOY_DIR"
echo "Domain: $DOMAIN"

#############################################################################
# VERIFY BASE BACKUPS
#############################################################################
DB_BACKUP="/backups/${PROJECT_NAME}-base.sql.gz"
FILES_BACKUP="/backups/${PROJECT_NAME}-files.tar.gz"

echo "Verifying base backup files..."

MISSING_FILES=()
[ ! -f "$DB_BACKUP" ] && MISSING_FILES+=("Database: $DB_BACKUP")
[ ! -f "$FILES_BACKUP" ] && MISSING_FILES+=("Files: $FILES_BACKUP")

if [ ${#MISSING_FILES[@]} -ne 0 ]; then
  echo "ERROR: Missing base backup files:"
  for item in "${MISSING_FILES[@]}"; do
    echo "  - $item"
  done
  echo ""
  echo "Push them first with: ddev push-to-preview-server"
  exit 1
fi

echo "Base files verified"

#############################################################################
# EXTRACT ARTIFACT TO DEPLOY DIR
#############################################################################
if [ -d "${DEPLOY_DIR}" ]; then
  echo "Updating existing preview..."
else
  echo "First time deployment - creating new preview..."
fi

mkdir -p "${DEPLOY_DIR}"
tar -xzf "$ARTIFACT_PATH" -C "${DEPLOY_DIR}"
rm -f "$ARTIFACT_PATH"

cd "${DEPLOY_DIR}"

#############################################################################
# BASIC AUTH
#############################################################################
BASIC_AUTH_USER="preview"
BASIC_AUTH_PASS=$(openssl rand -base64 12 | tr -d "=+/" | cut -c1-12)

echo "Generating Basic Auth credentials..."
echo "  Username: ${BASIC_AUTH_USER}"
echo "  Password: ${BASIC_AUTH_PASS}"

#############################################################################
# CONFIGURE DDEV
#############################################################################
ddev config --project-name="${DDEV_PROJECT_NAME}" --docroot=web --project-type=drupal --additional-fqdns="${DOMAIN}"

# Set preview server environment variable
mkdir -p "${DEPLOY_DIR}/.ddev"
cat > "${DEPLOY_DIR}/.ddev/web-environment" << 'EOF'
IS_PREVIEW_SERVER=true
EOF

#############################################################################
# CONFIGURE BASIC AUTH (NGINX)
#############################################################################
mkdir -p "${DEPLOY_DIR}/.ddev/nginx"

BASIC_AUTH_HASH_NGINX=$(htpasswd -nb "${BASIC_AUTH_USER}" "${BASIC_AUTH_PASS}")
echo "${BASIC_AUTH_HASH_NGINX}" > "${DEPLOY_DIR}/.ddev/nginx/.htpasswd"

cat > "${DEPLOY_DIR}/.ddev/nginx/basicauth.conf" << 'EOF'
auth_basic "Preview Access";
auth_basic_user_file /mnt/ddev_config/nginx/.htpasswd;
EOF

#############################################################################
# SAVE PREVIEW INFO
#############################################################################
cat > "${DEPLOY_DIR}/.preview-info" << EOF
BRANCH=${CI_COMMIT_REF_NAME:-unknown}
COMMIT_SHA=${CI_COMMIT_SHA:-unknown}
MR_ID=${CI_MERGE_REQUEST_IID}
PROJECT=${PROJECT_NAME}
BASIC_AUTH_USER=${BASIC_AUTH_USER}
BASIC_AUTH_PASS=${BASIC_AUTH_PASS}
EOF

#############################################################################
# START DDEV
#############################################################################
ddev start

#############################################################################
# IMPORT DATABASE
#############################################################################
echo "Importing database..."
ddev drush sql:drop -y
gunzip -c "$DB_BACKUP" | ddev exec 'bash -c "$(drush sql:connect)"'

#############################################################################
# RUN PROJECT-SPECIFIC DEPLOY SCRIPT (if present)
#############################################################################
if [ -f "${DEPLOY_DIR}/.gitlab/ci/scripts/deploy.sh" ]; then
  echo "Running project deploy script: .gitlab/ci/scripts/deploy.sh"
  echo "--- deploy.sh contents ---"
  cat "${DEPLOY_DIR}/.gitlab/ci/scripts/deploy.sh"
  echo "--- deploy.sh output ---"
  bash "${DEPLOY_DIR}/.gitlab/ci/scripts/deploy.sh"
  echo "--- end deploy.sh ---"
fi

#############################################################################
# MARK DEPLOYMENT COMPLETE
#############################################################################
echo "$(date -Iseconds)" > "${DEPLOY_DIR}/.deployment-complete"

echo "Preview available at https://${DOMAIN}"
