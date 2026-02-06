#!/bin/bash
set -e

#############################################################################
# DEPLOY PREVIEW FROM GITLAB CI
#############################################################################
# This script is called when a new MR is created in GitLab.
# It validates that it's running in GitLab CI and then delegates
# to the shared build script.
#############################################################################

#############################################################################
# SECURITY CHECK - This script should ONLY run in GitLab Runner
#############################################################################

if [ -z "$GITLAB_CI" ] && [ -z "$CI" ]; then
  echo "❌ ERROR: This script can ONLY run in GitLab CI"
  echo "   CI variables not detected (GITLAB_CI, CI)"
  exit 1
fi

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# All required variables are already set by GitLab CI
# Just call the shared build script
exec "${SCRIPT_DIR}/_build-preview.sh"
