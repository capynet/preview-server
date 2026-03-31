#!/usr/bin/env bash
# Unified log viewer for the preview system.
#
# Usage:
#   ./preview-logs.sh server                              # preview-manager service logs (last 100)
#   ./preview-logs.sh server -f                           # follow server logs
#   ./preview-logs.sh worker                              # preview-worker service logs (last 100)
#   ./preview-logs.sh worker -f                           # follow worker logs
#   ./preview-logs.sh deploy 623                          # deployment logs from DB
#   ./preview-logs.sh deploy 623 --status                 # deployment status only (no logs)
#   ./preview-logs.sh vm soudal/branch-foo                # agent logs on the VM
#   ./preview-logs.sh vm soudal/branch-foo -f             # follow agent logs on VM
#   ./preview-logs.sh vm soudal/branch-foo docker         # docker logs on the VM (all containers)
#   ./preview-logs.sh vm soudal/branch-foo containers     # list containers on VM
#   ./preview-logs.sh vm soudal/branch-foo info            # GET /info from agent
#   ./preview-logs.sh db "SELECT ..."                     # run SQL query on PostgreSQL
#   ./preview-logs.sh preview soudal/branch-foo           # preview state from DB (JSON)

set -euo pipefail

MAIN_SERVER="91.99.157.66"
SSH_KEY="/home/preview-manager/.ssh/preview-vm"
DB_CONTAINER="preview-postgres"
DB_USER="preview_manager"
SCRIPTS_DIR="$(cd "$(dirname "$0")/server/preview-manager/scripts" && pwd)"

ssh_main() {
    ssh -o LogLevel=ERROR -o ConnectTimeout=5 "root@${MAIN_SERVER}" "$@"
}

ssh_vm() {
    local vm_ip="$1"
    shift
    # Base64-encode the command to avoid quoting hell in double SSH hop
    local encoded
    encoded=$(echo "$*" | base64 -w0)
    ssh_main "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR preview@${vm_ip} \"echo ${encoded} | base64 -d | bash\""
}

db_query() {
    ssh_main "docker exec ${DB_CONTAINER} psql -U ${DB_USER} -t -A $*"
}

get_vm_ip() {
    local input="$1"
    local project="${input%%/*}"
    local preview="${input#*/}"
    local ip
    ip=$(ssh_main "docker exec ${DB_CONTAINER} psql -U ${DB_USER} -t -A -c \"
        SELECT p.vm_ip FROM previews p
        JOIN projects proj ON p.project_id = proj.id
        WHERE proj.slug = '${project}' AND p.preview_name = '${preview}'
    \"" | tr -d '[:space:]')
    if [[ -z "$ip" ]]; then
        echo "Error: preview '${input}' not found or has no VM" >&2
        exit 1
    fi
    echo "$ip"
}

usage() {
    cat <<'EOF'
Usage: preview-logs.sh <command> [args...]

Commands:
  server [-f] [N]                     Server logs (preview-manager). -f to follow, N=lines (default 100)
  worker [-f] [N]                     Worker logs (preview-worker). -f to follow, N=lines (default 100)
  deploy <ID> [--status]              Deployment logs from DB. --status for summary only
  vm <project/preview> [subcommand]   VM logs. Subcommands:
      (none)                            Agent stdout (journalctl druploy-agent)
      -f                                Follow agent logs
      docker [container]                Docker container logs (all or specific)
      containers                        List running containers
      info                              GET /info from agent (stack, aliases, etc.)
      status                            GET /deploy/status from agent
      cmd <command...>                  Run arbitrary command on VM
  db "<SQL>"                          Run SQL query on PostgreSQL
  preview <project/preview>           Preview state from DB (all columns)

Examples:
  preview-logs.sh server -f
  preview-logs.sh deploy 623
  preview-logs.sh vm soudal/branch-foo info
  preview-logs.sh vm soudal/branch-foo docker php
  preview-logs.sh preview soudal/branch-foo
  preview-logs.sh db "SELECT id, status FROM deployments ORDER BY id DESC LIMIT 5"
EOF
    exit 1
}

cmd_server() {
    local follow=""
    local lines=100
    for arg in "$@"; do
        case "$arg" in
            -f|--follow) follow="-f" ;;
            [0-9]*) lines="$arg" ;;
        esac
    done
    if [[ -n "$follow" ]]; then
        ssh_main "journalctl -u preview-manager -f --no-pager -o cat"
    else
        ssh_main "journalctl -u preview-manager -n ${lines} --no-pager -o cat"
    fi
}

cmd_worker() {
    local follow=""
    local lines=100
    for arg in "$@"; do
        case "$arg" in
            -f|--follow) follow="-f" ;;
            [0-9]*) lines="$arg" ;;
        esac
    done
    if [[ -n "$follow" ]]; then
        ssh_main "journalctl -u preview-worker -f --no-pager -o cat"
    else
        ssh_main "journalctl -u preview-worker -n ${lines} --no-pager -o cat"
    fi
}

cmd_deploy() {
    if [[ $# -lt 1 ]]; then
        echo "Usage: preview-logs.sh deploy <ID> [--status]"
        exit 1
    fi
    local deploy_id="$1"
    shift
    local extra_args=""
    for arg in "$@"; do
        extra_args="$extra_args $arg"
    done
    cat "${SCRIPTS_DIR}/get-deploy-logs.py" | \
        ssh_main "cd /home/preview-manager/www/previews/server/preview-manager && source venv/bin/activate && python3 - --id ${deploy_id} ${extra_args}"
}

cmd_vm() {
    if [[ $# -lt 1 ]]; then
        echo "Usage: preview-logs.sh vm <project/preview> [subcommand]"
        exit 1
    fi
    local input="$1"
    shift
    local vm_ip
    vm_ip=$(get_vm_ip "$input")
    echo "→ ${input} @ ${vm_ip}" >&2

    local subcmd="${1:-logs}"
    shift 2>/dev/null || true

    case "$subcmd" in
        -f|--follow)
            ssh_vm "$vm_ip" "journalctl -u druploy-agent -f --no-pager -o cat 2>/dev/null || tail -f /var/log/druploy-agent.log 2>/dev/null || echo 'No agent logs found'"
            ;;
        logs)
            ssh_vm "$vm_ip" "journalctl -u druploy-agent -n 200 --no-pager -o cat 2>/dev/null || cat /var/log/druploy-agent.log 2>/dev/null || echo 'No agent logs found'"
            ;;
        docker)
            local container="${1:-}"
            if [[ -n "$container" ]]; then
                # Find the full container name matching the service
                ssh_vm "$vm_ip" "docker logs --tail 100 \$(docker ps -a --format '{{.Names}}' | grep -i '${container}' | head -1) 2>&1"
            else
                # Show logs from all preview containers
                ssh_vm "$vm_ip" "for c in \$(docker ps --format '{{.Names}}'); do echo '=== '\$c' ==='; docker logs --tail 30 \$c 2>&1 | tail -15; echo; done"
            fi
            ;;
        containers)
            ssh_vm "$vm_ip" "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'"
            ;;
        info)
            ssh_vm "$vm_ip" "curl -s http://localhost:8022/info 2>/dev/null" | python3 -m json.tool 2>/dev/null || echo "Agent /info not available"
            ;;
        status)
            ssh_vm "$vm_ip" "curl -s http://localhost:8022/deploy/status 2>/dev/null" | python3 -m json.tool 2>/dev/null || echo "Agent /deploy/status not available"
            ;;
        cmd)
            ssh_vm "$vm_ip" "$*"
            ;;
        *)
            echo "Unknown VM subcommand: $subcmd"
            echo "Available: logs, -f, docker [container], containers, info, status, cmd <command>"
            exit 1
            ;;
    esac
}

cmd_db() {
    if [[ $# -lt 1 ]]; then
        echo "Usage: preview-logs.sh db \"SELECT ...\""
        exit 1
    fi
    ssh_main "docker exec ${DB_CONTAINER} psql -U ${DB_USER} -c \"$1\""
}

cmd_preview() {
    if [[ $# -lt 1 ]]; then
        echo "Usage: preview-logs.sh preview <project/preview>"
        exit 1
    fi
    local input="$1"
    local project="${input%%/*}"
    local preview="${input#*/}"
    ssh_main "docker exec ${DB_CONTAINER} psql -U ${DB_USER} -t -A -c \"
        SELECT row_to_json(t) FROM (
            SELECT p.*, proj.slug AS project_slug
            FROM previews p
            JOIN projects proj ON p.project_id = proj.id
            WHERE proj.slug = '${project}' AND p.preview_name = '${preview}'
        ) t
    \"" | python3 -m json.tool 2>/dev/null || echo "Preview not found"
}

# --- Main ---
if [[ $# -lt 1 ]]; then
    usage
fi

command="$1"
shift

case "$command" in
    server)     cmd_server "$@" ;;
    worker)     cmd_worker "$@" ;;
    deploy)     cmd_deploy "$@" ;;
    vm)         cmd_vm "$@" ;;
    db)         cmd_db "$@" ;;
    preview)    cmd_preview "$@" ;;
    -h|--help)  usage ;;
    *)          echo "Unknown command: $command"; usage ;;
esac
