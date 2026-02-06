#!/bin/bash
#
# Cleanup Closed/Merged MRs Script
#
# This script checks all preview environments and removes those whose
# merge requests have been merged or closed.
#
# It runs periodically via GitLab scheduled pipeline.
#
# Required Environment Variables:
#   - CI_PROJECT_ID: GitLab project ID
#   - CI_JOB_TOKEN: GitLab API token (auto-provided by GitLab CI)
#   - CI_API_V4_URL: GitLab API URL (auto-provided by GitLab CI)
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
echo -e "${BLUE}  Scheduled Cleanup of Closed/Merged MRs${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

# Verify we are running in GitLab CI
if [ -z "$GITLAB_CI" ] && [ -z "$CI" ]; then
  echo -e "${RED}❌ ERROR: This script can ONLY run in GitLab CI${NC}"
  exit 1
fi

echo -e "${CYAN}✓ Running in GitLab CI${NC}"

# Validate required environment variables
if [ -z "$CI_PROJECT_ID" ] || [ -z "$CI_JOB_TOKEN" ] || [ -z "$CI_API_V4_URL" ]; then
  echo -e "${RED}❌ ERROR: Missing required GitLab CI variables${NC}"
  echo "  Required: CI_PROJECT_ID, CI_JOB_TOKEN, CI_API_V4_URL"
  exit 1
fi

echo -e "${CYAN}✓ GitLab API: ${CI_API_V4_URL}${NC}"
echo -e "${CYAN}✓ Project ID: ${CI_PROJECT_ID}${NC}"
echo ""

# Base directory for previews
PREVIEWS_DIR="/var/www/previews"

# Check if previews directory exists
if [ ! -d "$PREVIEWS_DIR" ]; then
  echo -e "${YELLOW}⚠ Previews directory does not exist: ${PREVIEWS_DIR}${NC}"
  echo -e "${CYAN}Nothing to clean up.${NC}"
  exit 0
fi

# Find all mr-* directories (in project subdirectories)
MR_DIRS=$(find "$PREVIEWS_DIR" -mindepth 2 -maxdepth 2 -type d -name "mr-*" 2>/dev/null || true)

if [ -z "$MR_DIRS" ]; then
  echo -e "${CYAN}No preview environments found.${NC}"
  echo -e "${CYAN}Nothing to clean up.${NC}"
  exit 0
fi

echo -e "${BLUE}Found preview environments:${NC}"
echo "$MR_DIRS" | while read dir; do
  echo -e "  - $(basename $dir)"
done
echo ""

# Counters (using temp files because of subshell in while loop)
echo "0" > /tmp/cleanup_total
echo "0" > /tmp/cleanup_cleaned
echo "0" > /tmp/cleanup_active
echo "0" > /tmp/cleanup_errors

# Process each MR directory
echo -e "${BLUE}Checking MR status...${NC}"
echo ""

echo "$MR_DIRS" | while read MR_DIR; do
  TOTAL=$(cat /tmp/cleanup_total)
  CLEANED=$(cat /tmp/cleanup_cleaned)
  ACTIVE=$(cat /tmp/cleanup_active)
  ERRORS=$(cat /tmp/cleanup_errors)

  TOTAL=$((TOTAL + 1))
  echo "$TOTAL" > /tmp/cleanup_total

  # Extract MR number from directory name (mr-123 -> 123)
  DIR_NAME=$(basename "$MR_DIR")
  MR_IID="${DIR_NAME#mr-}"

  # Extract project name from parent directory
  PROJECT_DIR=$(dirname "$MR_DIR")
  PROJECT_NAME_SLUG=$(basename "$PROJECT_DIR")

  # Validate it's a number
  if ! [[ "$MR_IID" =~ ^[0-9]+$ ]]; then
    echo -e "${YELLOW}⚠ Skipping invalid directory: ${DIR_NAME}${NC}"
    continue
  fi

  echo -e "${CYAN}Checking ${PROJECT_NAME_SLUG}/MR !${MR_IID}...${NC}"

  # Query GitLab API for MR status
  API_URL="${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/merge_requests/${MR_IID}"

  HTTP_CODE=$(curl -s -o /tmp/mr_response_${MR_IID}.json -w "%{http_code}" \
    --header "JOB-TOKEN: ${CI_JOB_TOKEN}" \
    "$API_URL")

  if [ "$HTTP_CODE" = "404" ]; then
    echo -e "${YELLOW}  MR !${MR_IID} not found (may have been deleted)${NC}"
    echo -e "${YELLOW}  Cleaning up orphaned directory...${NC}"

    # Cleanup this orphaned preview
    DDEV_PROJECT_NAME="${PROJECT_NAME_SLUG}-mr-${MR_IID}"
    DEPLOY_DIR="$MR_DIR"

    # Delete DDEV project if it exists (don't need to cd to directory)
    if ddev list | grep -q "${DDEV_PROJECT_NAME}"; then
      ddev delete "${DDEV_PROJECT_NAME}" -O -y 2>/dev/null || echo "  Note: DDEV project may not exist"
    fi

    # Remove directory
    rm -rf "$DEPLOY_DIR"

    echo -e "${GREEN}  ✓ Cleaned up orphaned ${PROJECT_NAME_SLUG}/MR !${MR_IID}${NC}"
    CLEANED=$((CLEANED + 1))
    echo "$CLEANED" > /tmp/cleanup_cleaned
    echo ""
    continue
  fi

  if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}  ❌ API error (HTTP ${HTTP_CODE})${NC}"
    ERRORS=$((ERRORS + 1))
    echo "$ERRORS" > /tmp/cleanup_errors
    echo ""
    continue
  fi

  # Parse MR state from response
  MR_STATE=$(jq -r '.state' /tmp/mr_response_${MR_IID}.json)

  if [ "$MR_STATE" = "merged" ] || [ "$MR_STATE" = "closed" ]; then
    echo -e "${YELLOW}  MR !${MR_IID} is ${MR_STATE}${NC}"
    echo -e "${YELLOW}  Cleaning up preview environment...${NC}"

    # Cleanup this preview
    DDEV_PROJECT_NAME="${PROJECT_NAME_SLUG}-mr-${MR_IID}"
    DEPLOY_DIR="$MR_DIR"

    # Delete DDEV project if it exists (don't need to cd to directory)
    if ddev list | grep -q "${DDEV_PROJECT_NAME}"; then
      ddev delete "${DDEV_PROJECT_NAME}" -O -y 2>/dev/null || echo "  Note: DDEV project may not exist"
    fi

    # Remove directory
    rm -rf "$DEPLOY_DIR"

    echo -e "${GREEN}  ✓ Cleaned up ${PROJECT_NAME_SLUG}/MR !${MR_IID}${NC}"
    CLEANED=$((CLEANED + 1))
    echo "$CLEANED" > /tmp/cleanup_cleaned
  else
    echo -e "${GREEN}  ${PROJECT_NAME_SLUG}/MR !${MR_IID} is active (${MR_STATE})${NC}"
    ACTIVE=$((ACTIVE + 1))
    echo "$ACTIVE" > /tmp/cleanup_active
  fi

  # Cleanup temp file
  rm -f /tmp/mr_response_${MR_IID}.json

  echo ""
done

# Read final counter values
TOTAL=$(cat /tmp/cleanup_total 2>/dev/null || echo "0")
CLEANED=$(cat /tmp/cleanup_cleaned 2>/dev/null || echo "0")
ACTIVE=$(cat /tmp/cleanup_active 2>/dev/null || echo "0")
ERRORS=$(cat /tmp/cleanup_errors 2>/dev/null || echo "0")

# Cleanup temp files
rm -f /tmp/cleanup_total /tmp/cleanup_cleaned /tmp/cleanup_active /tmp/cleanup_errors

# Summary
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}Cleanup Summary${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Total previews checked: ${TOTAL}"
echo -e "  ${GREEN}Active MRs (kept):     ${ACTIVE}${NC}"
echo -e "  ${YELLOW}Cleaned up:            ${CLEANED}${NC}"
if [ "$ERRORS" -gt 0 ]; then
  echo -e "  ${RED}Errors:                ${ERRORS}${NC}"
fi
echo ""

if [ "$CLEANED" -gt 0 ]; then
  echo -e "${GREEN}✓ Cleanup completed - ${CLEANED} environment(s) removed${NC}"
else
  echo -e "${CYAN}All preview environments are active - nothing to clean up${NC}"
fi
echo ""
