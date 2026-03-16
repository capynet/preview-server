#!/bin/bash
set -euo pipefail

# preview-ssh-proxy: restricted SSH proxy for preview VM access
# Installed at /usr/local/bin/preview-ssh-proxy
# Used as command= restriction in authorized_keys
#
# Format: VM_IP CONTAINER WORKDIR [COMMAND...]
#   - No command: opens interactive bash
#   - With command: executes it and exits

KEY="/home/preview-manager/.ssh/preview-vm"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

if [ -z "${SSH_ORIGINAL_COMMAND:-}" ]; then
    echo "Interactive shell not allowed. Use: preview ssh"
    exit 1
fi

# Parse: VM_IP CONTAINER WORKDIR [COMMAND...]
read -r VM_IP CONTAINER WORKDIR REST <<< "$SSH_ORIGINAL_COMMAND"

# Validate IP format
if ! [[ "$VM_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid VM IP"
    exit 1
fi

# Validate container name (alphanumeric + hyphens only)
if ! [[ "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
    echo "Invalid container name"
    exit 1
fi

# Build docker exec command
if [ -n "${REST:-}" ]; then
    # Non-interactive: run command and exit
    DOCKER_CMD="docker exec -w $WORKDIR $CONTAINER $REST"
else
    # Interactive: open bash shell
    DOCKER_CMD="docker exec -it"
    if [ -n "${WORKDIR:-}" ] && [ "$WORKDIR" != "-" ]; then
        DOCKER_CMD="$DOCKER_CMD -w $WORKDIR"
    fi
    DOCKER_CMD="$DOCKER_CMD $CONTAINER bash"
fi

exec ssh -t -i "$KEY" $SSH_OPTS "root@$VM_IP" "$DOCKER_CMD"
