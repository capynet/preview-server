#!/usr/bin/env bash
# Build the Druploy docs site with MkDocs Material.
# Output lands directly in ../server/preview-manager/landing/docs/
# so that Ansible (`roles/caddy`) syncs it to the server with the rest of landing/.
#
# This script tries a project-local venv first. If `python3 -m venv` is not
# available (Debian/Ubuntu often need `python3-venv` to be installed),
# it falls back to a user-site install via pip + --break-system-packages.

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"

resolve_mkdocs() {
  if command -v mkdocs >/dev/null 2>&1; then
    MKDOCS_BIN="$(command -v mkdocs)"
  elif [ -x "$HOME/.local/bin/mkdocs" ]; then
    MKDOCS_BIN="$HOME/.local/bin/mkdocs"
  else
    echo "mkdocs not found on PATH after install — check ~/.local/bin/ is in PATH." >&2
    exit 1
  fi
}

if python3 -c "import ensurepip" >/dev/null 2>&1; then
  if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv..."
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install -q --upgrade pip
  "$VENV_DIR/bin/pip" install -q -r requirements.txt
  MKDOCS_BIN="$VENV_DIR/bin/mkdocs"
else
  echo "python3-venv not available; installing into user site (--user)..."
  pip install --user --break-system-packages -q --upgrade pip
  pip install --user --break-system-packages -q -r requirements.txt
  resolve_mkdocs
fi

echo "Building docs..."
"$MKDOCS_BIN" build --clean

echo "Done. Output: ../server/preview-manager/landing/docs/"
