#!/bin/bash
#
# Cleanup Preview Environment Script
#
# This script removes a DDEV preview environment from the preview server.
# It is designed to run in GitLab CI when a merge request is merged or closed.
#
# Required Environment Variables:
#   - PROJECT_NAME: Name of the DDEV project (e.g., mr-2)
#   - DEPLOY_DIR: Full path to deployment directory (e.g., /var/www/previews/mr-2)
#   - CI or GITLAB_CI: GitLab CI indicator
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Preview Environment Cleanup${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

# Verify we are running in GitLab CI
if [ -z "$GITLAB_CI" ] && [ -z "$CI" ]; then
  echo -e "${RED}❌ ERROR: This script can ONLY run in GitLab CI${NC}"
  echo ""
  echo "This is a safety measure to prevent accidental execution."
  exit 1
fi

echo -e "${CYAN}✓ Running in GitLab CI${NC}"

# Validate required environment variables
if [ -z "$PROJECT_NAME" ] || [ -z "$DEPLOY_DIR" ]; then
  echo -e "${RED}❌ ERROR: Missing required environment variables${NC}"
  echo "  Required: PROJECT_NAME, DEPLOY_DIR"
  exit 1
fi

echo -e "${CYAN}✓ Variables: PROJECT_NAME=${PROJECT_NAME}, DEPLOY_DIR=${DEPLOY_DIR}${NC}"
echo ""

# ============================================================================
# CLEANUP OPERATIONS
# ============================================================================

echo -e "${BLUE}Starting cleanup for: ${PROJECT_NAME}${NC}"
echo -e "${BLUE}Directory: ${DEPLOY_DIR_ABS}${NC}"
echo ""

# Check if DDEV project exists
echo -e "${CYAN}[1/3] Checking DDEV project status...${NC}"
if ddev list | grep -q "^${PROJECT_NAME}"; then
  echo -e "${YELLOW}      DDEV project found: ${PROJECT_NAME}${NC}"

  echo -e "${CYAN}[2/3] Stopping and deleting DDEV project...${NC}"
  cd "$DEPLOY_DIR_ABS" || {
    echo -e "${RED}      ❌ Failed to change to directory: $DEPLOY_DIR_ABS${NC}"
    echo -e "${YELLOW}      Attempting cleanup anyway...${NC}"
  }

  # Delete DDEV project
  # -O: Omit snapshot (faster)
  # -y: Skip confirmation
  if ddev delete "${PROJECT_NAME}" -O -y; then
    echo -e "${GREEN}      ✓ DDEV project deleted successfully${NC}"
  else
    echo -e "${YELLOW}      ⚠ DDEV delete command failed (project may not exist)${NC}"
    echo -e "${YELLOW}      Continuing with directory cleanup...${NC}"
  fi
else
  echo -e "${YELLOW}      DDEV project not found: ${PROJECT_NAME}${NC}"
  echo -e "${CYAN}      Skipping DDEV deletion...${NC}"
fi

echo ""

# Remove deployment directory
echo -e "${CYAN}[3/3] Removing deployment directory...${NC}"
if [ -d "$DEPLOY_DIR_ABS" ]; then
  echo -e "${YELLOW}      Deleting: ${DEPLOY_DIR_ABS}${NC}"

  # Final safety check before rm -rf
  if [[ "$DEPLOY_DIR_ABS" == "$ALLOWED_BASE/"* ]] && [ ${#DEPLOY_DIR_ABS} -gt ${#ALLOWED_BASE} ]; then
    rm -rf "$DEPLOY_DIR_ABS"
    echo -e "${GREEN}      ✓ Directory removed successfully${NC}"
  else
    echo -e "${RED}      ❌ Final safety check failed - directory not deleted${NC}"
    echo -e "${RED}      Path: $DEPLOY_DIR_ABS${NC}"
    exit 1
  fi
else
  echo -e "${YELLOW}      Directory does not exist: ${DEPLOY_DIR_ABS}${NC}"
  echo -e "${CYAN}      Already cleaned up${NC}"
fi

echo ""

# ============================================================================
# COMPLETION
# ============================================================================

echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✓ Cleanup completed successfully!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${BLUE}Cleaned up:${NC}"
echo -e "  ${GREEN}✓${NC} DDEV project: ${PROJECT_NAME}"
echo -e "  ${GREEN}✓${NC} Directory: ${DEPLOY_DIR_ABS}"
echo ""
echo -e "${CYAN}Preview environment has been removed from the server.${NC}"
echo ""
