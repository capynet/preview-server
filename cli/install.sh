#!/bin/sh
set -e

BASE_URL="https://api.preview-mr.com/api/cli"

# Detect OS
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$OS" in
  linux)  OS="linux" ;;
  darwin) OS="darwin" ;;
  *)
    echo "Error: Unsupported operating system: $OS"
    exit 1
    ;;
esac

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64)  ARCH="amd64" ;;
  aarch64|arm64)  ARCH="arm64" ;;
  *)
    echo "Error: Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

DOWNLOAD_URL="${BASE_URL}/download/${OS}/${ARCH}"
INSTALL_DIR="${HOME}/.local/bin"
BINARY_NAME="preview"
TMP_FILE=$(mktemp)

echo "Downloading preview CLI for ${OS}/${ARCH}..."
if ! curl -fsSL "$DOWNLOAD_URL" -o "$TMP_FILE"; then
  echo "Error: Failed to download binary from $DOWNLOAD_URL"
  rm -f "$TMP_FILE"
  exit 1
fi

chmod +x "$TMP_FILE"

# Ensure install directory exists
mkdir -p "$INSTALL_DIR"
mv "$TMP_FILE" "${INSTALL_DIR}/${BINARY_NAME}"

echo ""
echo "preview CLI installed successfully!"
echo ""
${INSTALL_DIR}/${BINARY_NAME} --version
echo ""

# Check if ~/.local/bin is in PATH
case ":$PATH:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    echo "WARNING: ${INSTALL_DIR} is not in your PATH."
    echo "Add it by running:"
    echo ""
    echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    echo ""
    ;;
esac

# Install shell completions
SHELL_NAME=$(basename "$SHELL" 2>/dev/null || echo "")
case "$SHELL_NAME" in
  bash)
    COMP_DIR="${HOME}/.local/share/bash-completion/completions"
    mkdir -p "$COMP_DIR"
    "${INSTALL_DIR}/${BINARY_NAME}" completion bash > "${COMP_DIR}/preview" 2>/dev/null
    ;;
  zsh)
    COMP_DIR="${HOME}/.local/share/zsh/site-functions"
    mkdir -p "$COMP_DIR"
    "${INSTALL_DIR}/${BINARY_NAME}" completion zsh > "${COMP_DIR}/_preview" 2>/dev/null
    ;;
esac

# Only show login hint if not already authenticated
CONFIG_FILE="${HOME}/.preview-manager.json"
if ! grep -q '"token"' "$CONFIG_FILE" 2>/dev/null; then
  echo ""
  echo "Get started with: preview login"
fi
