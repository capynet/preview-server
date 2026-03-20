#!/bin/bash
set -euo pipefail

# preview-ssh-proxy: restricted SSH proxy for preview VM access
# Installed at /usr/local/bin/preview-ssh-proxy
# Used as command= restriction in authorized_keys
#
# Format 1 (CLI): VM_IP CONTAINER WORKDIR [COMMAND...]
#   - No command: opens interactive bash
#   - With command: executes it and exits
#
# Format 2 (drush aliases): PREV_ROUTE env var set via SSH SetEnv.
#   Contains VM_IP:CONTAINER. SSH_ORIGINAL_COMMAND has the drush command.

KEY="/home/preview-manager/.ssh/preview-vm"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

if [ -z "${SSH_ORIGINAL_COMMAND:-}" ]; then
    echo "Interactive shell not allowed. Use: preview ssh"
    exit 1
fi

# Read the first token to determine the format
read -r FIRST_TOKEN REST_TOKENS <<< "$SSH_ORIGINAL_COMMAND"

if [[ "$FIRST_TOKEN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    # Format 1: VM_IP CONTAINER WORKDIR [COMMAND...]
    VM_IP="$FIRST_TOKEN"
    read -r CONTAINER WORKDIR REST <<< "$REST_TOKENS"
elif [ -n "${PREV_ROUTE:-}" ]; then
    # Format 2: routing via PREV_ROUTE env var (sent via SSH SetEnv)
    VM_IP="${PREV_ROUTE%%:*}"
    CONTAINER="${PREV_ROUTE#*:}"
    WORKDIR="/var/www/html"
    REST="$SSH_ORIGINAL_COMMAND"
else
    echo "Error: cannot determine VM/container routing."
    echo "Use 'preview ssh' or ensure PREV_ROUTE is set in the drush alias."
    exit 1
fi

# Validate IP format
if ! [[ "$VM_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid VM IP: $VM_IP"
    exit 1
fi

# Validate container name (alphanumeric + hyphens only)
if ! [[ "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
    echo "Invalid container name: $CONTAINER"
    exit 1
fi

# Build docker exec command
if [ -n "${REST:-}" ]; then
    # Non-interactive: run command and exit
    DOCKER_CMD="docker exec -w $WORKDIR $CONTAINER bash -c '${REST//\'/\'\\\'\'}'"
else
    # Interactive: open bash shell
    DOCKER_CMD="docker exec -it"
    if [ -n "${WORKDIR:-}" ] && [ "$WORKDIR" != "-" ]; then
        DOCKER_CMD="$DOCKER_CMD -w $WORKDIR"
    fi
    DOCKER_CMD="$DOCKER_CMD $CONTAINER bash"
fi

exec ssh -t -i "$KEY" $SSH_OPTS "root@$VM_IP" "$DOCKER_CMD"
