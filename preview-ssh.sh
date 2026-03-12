#!/usr/bin/env bash
# Quick SSH into a preview VM
# Usage: ./preview-ssh.sh PROJECT/PREVIEW_NAME [command...]
#
# Examples:
#   ./preview-ssh.sh drupal-test/mr-123              # interactive shell on VM
#   ./preview-ssh.sh drupal-test/mr-123 docker ps    # run command on VM
#   ./preview-ssh.sh --list                          # list all previews with VMs

set -euo pipefail

MAIN_SERVER="91.99.157.66"
SSH_KEY="/home/preview-manager/.ssh/preview-vm"
DB_CONTAINER="preview-postgres"
DB_USER="preview_manager"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 PROJECT/PREVIEW_NAME [command...]"
    echo "       $0 --list"
    echo ""
    echo "Examples:"
    echo "  $0 drupal-test/mr-123              # interactive shell"
    echo "  $0 drupal-test/mr-123 docker ps    # run a command"
    echo "  $0 --list                          # list previews with VMs"
    exit 1
fi

ssh_main() {
    ssh -o LogLevel=ERROR "root@${MAIN_SERVER}" "$@"
}

# List mode
if [[ "$1" == "--list" || "$1" == "-l" ]]; then
    echo "Previews with active VMs:"
    echo ""
    printf "  %-40s %-16s %s\n" "PREVIEW" "VM IP" "STATUS"
    printf "  %-40s %-16s %s\n" "-------" "-----" "------"
    ssh_main "docker exec ${DB_CONTAINER} psql -U ${DB_USER} -t -A -F '|' -c \"
        SELECT proj.slug, p.preview_name, p.vm_ip, p.status
        FROM previews p
        JOIN projects proj ON p.project_id = proj.id
        WHERE p.vm_ip IS NOT NULL
        ORDER BY proj.slug, p.preview_name
    \"" | while IFS='|' read -r project name ip status; do
        [[ -z "$project" ]] && continue
        printf "  %-40s %-16s %s\n" "${project}/${name}" "$ip" "$status"
    done
    exit 0
fi

# Parse project/preview
INPUT="$1"
shift

if [[ "$INPUT" != */* ]]; then
    echo "Error: format must be PROJECT/PREVIEW_NAME (e.g. drupal-test/mr-123)"
    exit 1
fi

PROJECT="${INPUT%%/*}"
PREVIEW="${INPUT#*/}"

# Get VM IP from database via main server
VM_IP=$(ssh_main "docker exec ${DB_CONTAINER} psql -U ${DB_USER} -t -A -c \"
    SELECT p.vm_ip FROM previews p
    JOIN projects proj ON p.project_id = proj.id
    WHERE proj.slug = '${PROJECT}' AND p.preview_name = '${PREVIEW}'
\"" | tr -d '[:space:]')

if [[ -z "$VM_IP" ]]; then
    echo "Error: preview '${PROJECT}/${PREVIEW}' not found or has no VM"
    echo ""
    echo "Use '$0 --list' to see available previews"
    exit 1
fi

echo "→ ${PROJECT}/${PREVIEW} @ ${VM_IP}"

# Connect to VM through main server
# The SSH key lives on the main server, so we SSH there first and then hop to the VM
if [[ $# -gt 0 ]]; then
    ssh_main "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@${VM_IP} $*"
else
    ssh -t "root@${MAIN_SERVER}" "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -t root@${VM_IP}"
fi
