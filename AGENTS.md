# AGENTS.md — preview-server (Druploy Backend)

## Project Overview

**Druploy** is a Drupal preview environment management system. It automatically spins up isolated, ephemeral Drupal preview environments (one per GitLab merge request or branch) on Hetzner Cloud VMs. Each preview runs its own VM with Docker Compose, orchestrated by a Go "VM Agent".

This repo is a **multi-component monorepo**:
- `server/preview-manager/` — Python FastAPI backend (the main coordinator/orchestrator)
- `cli/` — Go CLI (`druploy`) distributed to end users
- `vm-agent/` — Go agent that runs on each preview VM and executes deploys/terminals
- `vm-terminal-server/` — Legacy Go terminal server (functionality merged into vm-agent; kept for reference)
- `server/ansible/` — Ansible IaC for the coordinator server
- `runner/` — Ansible for a GitLab runner server
- `docs/` — MkDocs documentation source

The frontend (`preview-ui`, Next.js) lives in a separate repo.

Production: `https://api.druploy.dev` (backend), `https://druploy.dev` (frontend).

## Tech Stack

**Backend (server/preview-manager):**
- Python 3 (async), FastAPI 0.115.5, Uvicorn 0.32.1
- PostgreSQL 17 via `asyncpg` (raw SQL, no ORM for queries; SQLAlchemy only for Alembic migrations)
- Valkey (Redis-compatible) via `redis[asyncio]` + `arq` 0.26.1 for background workers
- Alembic 1.14.0 for migrations
- Pydantic 2.10.3 + pydantic-settings 2.6.1
- `hcloud` (Hetzner Cloud), `boto3` (S3 object storage), `httpx`, `asyncssh`, `psutil`
- Auth: `python-jose`, `itsdangerous`, `bcrypt` (session cookies + Bearer API tokens + OAuth GitLab/Google)
- Email: `resend`

**CLI:** Go 1.21, `spf13/cobra`. Binary `druploy` for linux/darwin amd64/arm64.

**VM Agent:** Go 1.26.1, `gorilla/websocket`, `creack/pty`, `gopkg.in/yaml.v3`. HTTP server on port 8022.

**Infrastructure:** Ansible, Caddy (reverse proxy), Docker, PostgreSQL, Valkey, systemd.

## Directory Structure

```
preview-server/
├── server/
│   ├── ansible/                # Ansible IaC (playbooks + roles for coordinator)
│   └── preview-manager/        # THE FastAPI backend (main component)
│       ├── main.py             # FastAPI app entry point + lifespan + uvicorn
│       ├── seed.py             # Seeds superadmin + default org
│       ├── alembic/            # 22 migrations (001–022)
│       ├── config/settings.py  # Pydantic Settings (all env config)
│       ├── app/
│       │   ├── api.py          # Router aggregator
│       │   ├── models.py       # Pydantic response models
│       │   ├── database.py     # asyncpg pool + all CRUD (orgs/projects/previews/deployments)
│       │   ├── state.py        # PreviewStateManager (thin DB wrapper)
│       │   ├── deployment.py   # PreviewDeployer — orchestrates VM deploys (~1495 lines)
│       │   ├── cloud.py        # HetznerCloudManager — VM/volume lifecycle
│       │   ├── caddy_api.py    # CaddyRouteManager — dynamic Caddy route management
│       │   ├── wake_preview.py # WakePreviewMiddleware — proxy/wake preview domains
│       │   ├── websockets.py   # All WS endpoints + broadcast managers (~1231 lines)
│       │   ├── workers.py      # arq WorkerSettings — background tasks + cron jobs
│       │   ├── valkey.py       # Valkey connection, deploy locks, log buffers, pub/sub
│       │   ├── remote.py       # RemoteExecutor — SSH command execution on VMs
│       │   ├── storage.py      # ObjectStorageManager (S3)
│       │   ├── storage_backend.py  # StorageBackend abstract interface
│       │   ├── storage_box.py  # Hetzner Storage Box (SFTP) backend
│       │   ├── docker_compose.py   # docker-compose.yml generation + druploy.yml parsing
│       │   ├── cron_jobs.py    # Cron job validation/loading
│       │   ├── preview_rules.py    # Auto-preview skip rules
│       │   ├── gitlab_token.py # GitLab token auto-rotation
│       │   ├── gitlab_comment.py   # Post/update MR comments on GitLab
│       │   ├── auth/
│       │   │   ├── models.py       # OrgRole enum, User/Organization models
│       │   │   ├── dependencies.py # get_current_user, get_org_context, require_*_role
│       │   │   ├── database.py     # User/session/oauth/token/ssh-key CRUD
│       │   │   ├── oauth.py        # OAuth providers (GitLab, Google)
│       │   │   └── email.py        # Email sending (invitations, magic links)
│       │   ├── routes/
│       │   │   ├── auth.py     # /api/auth/*
│       │   │   ├── orgs.py     # /api/orgs/*
│       │   │   ├── previews.py # /api/orgs/{org}/projects/{project}/previews/*
│       │   │   ├── gitlab.py   # /api/gitlab/* + /api/orgs/{org}/gitlab/*
│       │   │   ├── webhooks.py # /api/webhooks/{org}/gitlab
│       │   │   ├── base_files.py   # base-files endpoints
│       │   │   ├── cli.py      # /api/cli/* + /api/internal/agent/download
│       │   │   └── config.py   # settings endpoints + /api/health
│       │   └── tasks/          # Cron task implementations
│       │       ├── auto_erase.py
│       │       ├── docker_events.py
│       │       ├── orphan_vms.py
│       │       ├── purge_soft_deleted.py
│       │       ├── rotate_gitlab_tokens.py
│       │       └── warm_pool.py
│       ├── docker/             # Caddy + Drupal Dockerfiles/compose
│       ├── infra/              # docker-compose for Postgres + Valkey (local dev)
│       ├── scripts/            # ops scripts
│       └── landing/            # landing page (docs built by mkdocs)
├── cli/                        # Go CLI
├── vm-agent/                   # Go VM agent
├── vm-terminal-server/         # Legacy Go terminal server
├── runner/                     # Ansible for GitLab runner
├── docs/                       # MkDocs documentation
├── preview-ssh.sh              # Helper: SSH into a preview VM
└── preview-logs.sh             # Helper: unified log viewer
```

## Build / Run / Dev Commands

### Backend (Python) — workdir: `server/preview-manager/`

```bash
# Install dependencies
pip install -r requirements.txt

# Local infra (Postgres + Valkey)
docker compose -f infra/docker-compose.yml up -d

# Run migrations
alembic upgrade head

# Run API (dev) — runs migrations + seed + uvicorn with 2 workers
python main.py

# Run arq worker (separate process)
arq WorkerSettings

# Seed superadmin (automatic on startup if SEED_ADMIN_EMAIL set and users table empty)
python seed.py
```

### CLI (Go) — workdir: `cli/`

```bash
make build          # single binary
make all            # all platforms
./build.sh [version]  # auto-bumps patch
```

### VM Agent (Go) — workdir: `vm-agent/`

```bash
./build.sh    # → bin/vm-agent (linux/amd64)
```

### Local toolchain / requirements (control machine)

To work with and **deploy** this project from a control machine you need:

| Tool | Version | Why |
|------|---------|-----|
| **Python** | 3.12+ (with `pip`) | `preview-manager` backend (`requirements.txt`, ~24 deps) |
| **Go** | **1.26.1+** | Ansible builds the Go binaries **locally** during deploy. Highest requirement wins: `vm-agent` needs 1.26.1, `vm-terminal-server` 1.22, `cli` 1.21. Installing 1.26.1 covers all three. |
| **Node.js** | LTS | `preview-ui` (Next.js frontend) |
| **Ansible** | `ansible-core` 2.18+ | Deploys (see below) |
| **rsync + ssh + git** | any recent | Ansible `synchronize` uses rsync; agent build stamps a version from `git rev-parse` |

**Ansible collections** (not bundled with `ansible-core` — install with `ansible-galaxy collection install ...`):
`community.docker`, `ansible.posix`, `community.general`, `community.crypto`.

**Ansible vault:** secrets live in `inventory/group_vars/all/vault.yml` (encrypted).
`ansible.cfg` reads the password from `~/.vault_pass` (password: `preview-mr`).
Create it once: `printf 'preview-mr' > ~/.vault_pass && chmod 600 ~/.vault_pass`.

> Gotcha: even `--tags code` runs the **local Go build** of `vm-agent` (task
> `Build VM agent binary (local)`, `delegate_to: localhost`). Without Go on the
> control machine that task fails and the play stops before restarting services.

### Ansible deployment — workdir: `server/ansible/`

**Ansible is the preferred (canonical) way to deploy.** Do not hand-roll deploys
(manual `rsync` + `systemctl restart`, etc.) — they skip steps the playbook owns
(`.env` templating from vault, venv/pip, ownership, service files, docker infra)
and leave prod drifted from a clean run. Always go through the playbook.

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy-preview-manager.yml
```

- Code-only change? Scope it with `--tags code` (still re-templates `.env`, so the
  Ansible vault password — `preview-mr` — is required).
- Preview what a run would change first with `--check` (dry-run) before applying.
- The vault holds production secrets; pass it via `--ask-vault-pass` or a vault
  password file.

Playbooks: `setup-preview-server.yml`, `deploy-landing.yml`, `deploy-preview-ui.yml`, `harden-server.yml`, `setup-caddy.yml`, etc.

### Testing

**There is no automated test suite.** No test files exist. Testing is manual via `preview-logs.sh` and `preview-ssh.sh` helper scripts. `scripts/send_test_email.py` is a manual email test.

### Linting / Typecheck

No configured linter or typechecker. Python is dynamically typed (Pydantic for validation).

## Configuration

All config centralized in `config/settings.py` via Pydantic `BaseSettings` (env vars / `.env`). Key variables:

| Var | Default | Purpose |
|-----|---------|---------|
| `API_HOST` / `API_PORT` | 0.0.0.0 / 8000 | Uvicorn bind |
| `DATABASE_URL` | postgresql://preview_manager:...@localhost:5432/preview_manager | PostgreSQL DSN |
| `VALKEY_URL` | redis://localhost:6379 | Valkey/Redis DSN |
| `SECRET_KEY` | change-me-in-production | HMAC, sessions, terminal tokens |
| `BASE_DOMAIN` / `PREVIEW_DOMAIN` / `API_URL` / `FRONTEND_URL` | druploy.dev | Domain config |
| `GITLAB_URL` | https://gitlab.com | GitLab instance |
| `GITLAB_WEBHOOK_SECRET` | | Validates incoming webhooks |
| `GITLAB_OAUTH_CLIENT_ID/SECRET` | | GitLab user-login OAuth |
| `GOOGLE_OAUTH_CLIENT_ID/SECRET` | | Google OAuth |
| `RESEND_API_KEY` / `INVITATION_FROM_EMAIL` | | Transactional email |
| `HETZNER_API_TOKEN` | | Hetzner Cloud |
| `HETZNER_LOCATION` / `HETZNER_SERVER_TYPE` / `HETZNER_SNAPSHOT_ID` | fsn1 / cx23 / 0 | VM provisioning |
| `HETZNER_SSH_PRIVATE_KEY_PATH` / `HETZNER_SSH_PUBLIC_KEY` | | SSH to VMs |
| `WARM_POOL_SIZE` | 1 | Pre-created VMs |
| `HETZNER_S3_*` | | Object storage for base files |
| `STORAGE_BACKEND` | s3 | "s3" or "storagebox" |
| `SOFT_DELETE_RETENTION_DAYS` | 30 | Resurrection window |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_NAME` | | First-run seeding |
| `DOCKER_NETWORK` / `DRUPAL_BASE_IMAGE` / `DOCKER_REGISTRY` | | Docker defaults |
| `COMPOSER_PROXY_URL` | | tinyproxy for private registries |
| `UVICORN_WORKERS` | 2 | |

`.env.example` is minimal. Full config documented in `docs/` (environment-variables.md). Ansible vault (password `preview-mr`) holds production secrets.

## Architecture

**Multi-tenant, organization-scoped.** Hierarchy: Users → Organizations → Projects → Previews → Deployments. Roles: `owner > admin > member > viewer` (org-level and project-level; effective role = max of both). Superadmin bypasses all checks.

### Request Flow

1. **Caddy** terminates TLS for `api.druploy.dev` → proxies to FastAPI (port 8000). Caddy also serves `*.druploy.dev` preview domains via `WakePreviewMiddleware`.
2. **FastAPI** authenticates via session cookie (`pm_session`) or Bearer API token. Auth dependencies resolve org/project context and enforce RBAC.
3. **Route handlers** (`app/routes/`) call `app/database.py` (asyncpg) for CRUD and enqueue background work to **arq** (Valkey-backed queue).
4. **arq workers** (`app/workers.py`, `preview-worker` service) execute deploy/delete tasks and cron jobs.
5. **Deploys** (`app/deployment.py` `PreviewDeployer`): provision/assign a Hetzner VM (from warm pool or created), POST a `DeployJob` to the VM agent's `/deploy` endpoint. The agent runs phases (git clone, docker pull, import db, deploy script, post-deploy) and streams logs back over WebSocket (`/ws/internal/agent`) → Valkey pub/sub → frontend WebSocket clients.
6. **Preview access:** user visits `https://{url_hash}.druploy.dev`. Caddy wildcard route hits `WakePreviewMiddleware`, which authenticates, looks up the preview, and either reverse-proxies to the VM or shows a "waking up"/"building" splash. Soft-deleted previews show a "resurrect" splash.

### State

- **PostgreSQL**: source of truth for all entities.
- **Valkey**: arq job queue, deploy locks, deploy log buffering, pub/sub events, disk-usage cache, single-writer election locks.
- **Preview filesystem**: lives on the VM at `/var/www/preview`.

### Storage

Base DB/files (shared across previews of a project) stored in Hetzner Object Storage (S3) or Storage Box (SFTP), abstracted by `StorageBackend`.

## Maintenance Mode (control-plane drain)

Lets the control-plane (`druploy` API + `druploy-worker`) be redeployed **without breaking active preview deploys, without losing webhooks, and with the UI put read-only** — so writes can't race an in-flight deploy and corrupt state.

**Single source of truth:** a Valkey hash `maintenance:state` (`active`, `level`, `reason`, `by`, `started_at`). `level` ∈ `{drain, full}`. Helpers in `app/valkey.py`: `set_maintenance` / `get_maintenance` / `is_maintenance_active` / `list_active_deploy_locks` (SCAN over `deploy_lock:*`, never KEYS). `get_maintenance`/`is_maintenance_active` **fail open to inactive** so a Valkey blip never locks the system.

**How each layer reacts while active:**
- **Worker** (`app/workers.py`): `task_deploy_preview` and `task_delete_preview` **park** a *new* job — re-enqueue it with `_defer_by=30s`, `_expires=6h`, and return. In-flight deploys (they already hold `deploy_lock:<project>/<preview>`) and recovery retries (`job_try>=2`) run through. Parked jobs resume automatically when the flag clears. Mutating cron jobs skip via `_skip_for_maintenance(...)`; the reaper and webhook-inbox dispatcher keep running.
- **API** (`app/maintenance_middleware.py`, `MaintenanceMiddleware`): rejects mutating methods (POST/PUT/PATCH/DELETE) to `/api/**` with **503** while active. Exempt prefixes: `/api/webhooks` (durable receiver must keep accepting), `/api/admin/maintenance` (the toggle), `/api/auth` (admin must be able to log in). **Scoped to `/api/` only** — preview-app traffic on hash subdomains is never blocked. This is the real barrier; the UI disable is only UX.
- **UI** (frontend repo): a `MaintenanceProvider` polls `/api/health` (which reports `maintenance` + `active_deploys`) and also listens to a `druploy:maintenance` window event pushed over the previews websocket. `level=drain` → sticky banner + management surface read-only; `level=full` → blocking overlay (except `/admin`, so a superadmin can still toggle it off).

**Admin control:** `GET/POST /api/admin/maintenance` (`app/routes/config.py`). Auth via `require_admin`: a **superadmin** session/bearer **OR** the machine secret header `X-Admin-Token` matching `settings.admin_api_token` (this is how Ansible toggles it headlessly, and how the `/admin` UI `MaintenanceControl` toggles it as a superadmin). `/api/health` is extended to report `{maintenance: {active, level, active_deploys}}` for the drain poll.

**Ansible drain** (`server/ansible/`): `playbooks/tasks/maintenance_enable.yml` sets the flag then polls `GET /api/admin/maintenance` until `active_deploys == 0` (default 60×15s = 15 min) — turning the worker restart into a **drain** rather than a SIGKILL of a long deploy (worker `TimeoutStopSec=900` vs `job_timeout=36000`=10h). `maintenance_clear.yml` lifts it. Both are wired into `deploy-preview-manager.yml` (`pre_tasks` drain, `post_tasks` clear), **guarded by `admin_api_token | length > 0`** so deploys behave normally until the token is set. Standalone: `playbooks/maintenance-drain.yml` / `maintenance-clear.yml`. Token: `vault_admin_api_token` in the Ansible vault → `admin_api_token` (hosts.yml) → `ADMIN_API_TOKEN` (env.j2) → `settings.admin_api_token`. On the *first* deploy that ships this feature the enable call may 404 (old API lacks the endpoint); the task tolerates it (`failed_when: false`) and skips the drain.

**Webhook durability (the "no limbo" half):** the GitLab webhook receiver (`app/routes/webhooks.py` `gitlab_webhook`) is **thin** — it validates the token, persists the raw body to the `webhook_inbox` table (Postgres) and returns 200. All routing (org/project resolution, `druploy.yml` fetch, CI gating, deploy/delete decisions) runs later in the worker via `dispatch_webhook` → enqueued as `task_process_webhook`. Idempotent on GitLab's `X-Gitlab-Event-UUID` (partial unique index). A per-minute cron `task_dispatch_webhook_inbox` re-enqueues rows left `pending`/stuck `processing` (safety net if the direct enqueue failed). `task_purge_soft_deleted` also purges processed inbox rows after 7 days. So a webhook is only lost if Postgres is lost — and during maintenance webhooks are still accepted; the resulting deploy simply parks. Migration: `alembic/versions/023_webhook_inbox.py` (timestamps are `TEXT` ISO-8601, consistent with the rest of the schema — not `TIMESTAMPTZ`).

**Rollout note:** DB migrations run in `main()` before the uvicorn fork, so during a drain window an old worker coexists with the new schema — keep migrations **expand/contract** (additive first, drops in a later release).

## API Endpoints

All routes aggregated in `app/api.py`. Auth via session cookie or `Authorization: Bearer <token>`.

### Auth (`/api/auth`) — `app/routes/auth.py`
- `GET /api/auth/login/{provider}` — OAuth redirect (gitlab/google)
- `GET /api/auth/callback/{provider}` — OAuth callback
- `GET /api/auth/verify-preview` — Caddy forward_auth for preview URLs
- `POST /api/auth/logout`
- `GET /api/auth/me` — current user + orgs
- `GET /api/auth/projects/resolve?slug=` — resolve project slug (CLI)
- `POST /api/auth/cli/request` — CLI device flow
- `POST /api/auth/cli/approve` — approve CLI request
- `GET /api/auth/cli/poll/{code}` — CLI polls for approval
- `GET /api/auth/invitations/validate?token=`
- `POST /api/auth/invitations/accept`
- `POST /api/auth/magic/request` — send magic link
- `GET /api/auth/magic/verify?token=`
- `POST/GET/DELETE /api/auth/ssh-keys[/{key_id}]`
- `GET/POST /api/auth/admin/users` — superadmin user management
- `POST /api/auth/admin/users/{user_id}/revoke`
- `DELETE /api/auth/admin/users/{user_id}` — anonymize user
- `GET/POST/DELETE /api/auth/tokens[/{token_id}]` — API tokens

### GitLab (`/api/gitlab` + `/api/orgs/{org}/gitlab`) — `app/routes/gitlab.py`
- `GET /api/gitlab/auth/login` — GitLab OAuth login
- `GET /api/gitlab/auth/callback`
- `GET /api/orgs/{org}/gitlab/status`
- `POST /api/orgs/{org}/gitlab/connect` — connect via PAT
- `POST /api/orgs/{org}/gitlab/disconnect`
- `GET /api/orgs/{org}/gitlab/projects/enabled`
- `GET /api/orgs/{org}/gitlab/projects` — list all (cached 1h)
- `POST /api/orgs/{org}/gitlab/projects/{project_id}/enable`
- `GET /api/orgs/{org}/gitlab/projects/{project_id}/branches`
- `GET /api/orgs/{org}/gitlab/projects/by-slug/{project_slug}/branches`
- `GET /api/orgs/{org}/gitlab/projects/by-slug/{project_slug}/merge-requests`

### Webhooks (`/api/webhooks`) — `app/routes/webhooks.py`
- `POST /api/webhooks/{org_slug}/gitlab` — GitLab webhook (push, merge_request, pipeline). Validates `X-Gitlab-Token`.

### Organizations (`/api/orgs`) — `app/routes/orgs.py`
- `GET/POST /api/orgs` — list/create orgs
- `GET/PATCH/DELETE /api/orgs/{org}`
- `GET/POST /api/orgs/{org}/members`
- `PATCH/DELETE /api/orgs/{org}/members/{user_id}`
- `GET/POST/DELETE /api/orgs/{org}/email-domains[/{domain_id}]`
- `GET/POST /api/orgs/{org}/invitations[/{invitation_id}]`
- `DELETE /api/orgs/{org}/projects/{slug}`
- `GET /api/orgs/{org}/projects/{project}/my-role`
- `GET/POST /api/orgs/{org}/projects/{slug}/members`
- `PATCH/DELETE /api/orgs/{org}/projects/{slug}/members/{member_id}`

### Previews — `app/routes/previews.py`
Org-scoped (`/api/orgs/{org}/projects/{project}/previews`):
- `POST /previews/mr` — create from MR
- `POST /previews/branch` — create from branch
- `GET /previews` — list
- `GET /previews/{preview_name}` — detail
- `PATCH /previews/{preview_name}` — update (auto_update, pinned, env_vars, cron_jobs)
- `DELETE /previews/{preview_name}` — delete
- `GET /previews/{preview_name}/preview-config` — proxy to VM agent `/info`
- `POST /previews/{preview_name}/ssh-keys` — inject SSH key
- `POST /previews/{preview_name}/{stop|start|restart|rebuild|rerun-post-deploy|drush-uli}`
- `POST /previews/{preview_name}/drush` — run arbitrary drush command
- `POST /previews/{preview_name}/terminal-token` — generate HMAC terminal token
- `GET /previews/{preview_name}/deployments[/{deployment_id}[/live-logs]]`
- `GET /previews/{preview_name}/db/download` — gzipped SQL dump
- `GET /previews/{preview_name}/files/download` — tar.gz of files dir
- `GET /previews/{preview_name}/stats` — CPU/RAM/disk from VM

Global:
- `GET /api/previews` — all previews visible to user

### Base Files (`/api/orgs/{org}/projects/{slug}/base-files`) — `app/routes/base_files.py`
- `GET ` — status
- `GET /db` — download base db
- `GET /files` — download base files
- `POST /{kind}/upload/presign` — presigned S3 URL
- `POST /{kind}/upload/complete` — finalize
- `PUT /{kind}/upload/proxy` — proxy upload (non-S3 backends)

### Config/Settings — `app/routes/config.py`
- `GET/PUT /api/orgs/{org}/settings/{auto-erase|composer-proxy|require-ci}`
- `GET/PUT /api/orgs/{org}/projects/{project}/{env-vars|cron-jobs|preview-rules|settings/require-ci|settings/public-paths}`
- `GET /api/system/disk-usage` — superadmin
- `GET /api/health`
- `GET /` — root

### CLI Distribution — `app/routes/cli.py`
- `GET /api/cli/version`
- `GET /api/cli/install.sh`
- `GET /api/cli/download/{os}/{arch}`
- `GET /api/internal/agent/download` — VM agent binary

## WebSocket Endpoints

All in `app/websockets.py`, authenticated via `?token=` (API token) or `pm_session` cookie:

| Path | Purpose |
|------|---------|
| `/ws/previews` | Real-time per-user-filtered preview list (two-phase push) |
| `/ws/system-resources` | Real-time CPU/RAM/disk/load (2s interval) + docker stats |
| `/ws/deployments/{deployment_id}/logs` | Real-time deployment log streaming via Valkey pub/sub |
| `/ws/previews/{org}/{project}/{preview}/action?action=...` | Execute preview actions with streaming logs |
| `/ws/internal/agent` | Internal: VM agent → server log relay (HMAC token auth) |

**VM Agent endpoints** (on each VM, port 8022):
- `POST /deploy`, `POST /deploy/cancel`, `GET /deploy/status`, `GET /deploy/logs/{deployment_id}`
- `GET /ws` (terminal WebSocket, HMAC-token auth)
- `GET /containers`, `POST /ssh-keys`, `GET /info`, `GET /health`

## Database Schema

PostgreSQL via asyncpg (raw SQL, no ORM). 22 Alembic migrations (`alembic/versions/001`–`022`). Key tables:

- **users** — id, email (unique), name, avatar_url, password_hash, is_superadmin, system_role, created_at, updated_at, deleted_at (soft delete)
- **oauth_accounts** — user_id, provider, provider_user_id, provider_username
- **sessions** — id, user_id, created_at, expires_at
- **api_tokens** — user_id, organization_id, name, token_hash, token_prefix, last_used_at
- **cli_auth_requests** — code, status, user_id, token
- **organizations** — slug (unique), name, avatar_url, gitlab_url, gitlab_access_token, gitlab_token_expires_at, auto_erase_enabled/days, composer_proxy_enabled, color, require_ci_success
- **org_members** — user_id, organization_id, role
- **org_email_domains** — organization_id, domain, default_role (auto-join by email)
- **org_invitations** — organization_id, email, role, token, project_id, invited_by, status, expires_at
- **projects** — organization_id, slug (unique per org), name, gitlab_project_id/path/web_url/default_branch, env_vars, cron_jobs, public_paths, require_ci_success, skip_source/target_branches, composer_proxy
- **project_members** — user_id, project_id, role, added_by
- **previews** — project_id, preview_name (unique per project), url_hash, mr_id, mr_title, target_branch, branch, commit_sha, status, url, path, vm_id, vm_ip, volume_id, env_vars, cron_jobs, auto_update, pinned, last_accessed_at, deleted_at, post_deploy_status, stack_info, domain_aliases, expose_config, gitlab_note_id, ci_status
- **deployments** — preview_id, status, log_output, error, triggered_by, phases (JSON), started_at, completed_at, duration
- **cloud_resources** — project_id, preview_name, resource_type, resource_id, resource_name, spec, price_hourly/monthly, created_at, destroyed_at (cost tracking)
- **magic_link_tokens** — token, user_id, expires_at, consumed_at
- **ssh_keys** — user_id, name, public_key, fingerprint

URL hashes: `sha256(org-project-preview)[:8]` (`compute_url_hash`).

## Code Conventions

- **Pattern:** Layered — routes (thin HTTP handlers) → `app/database.py` & `app/auth/database.py` (raw SQL via asyncpg) → `app/state.py` / `app/deployment.py` (business logic). No repository pattern; module-level async functions returning plain dicts.
- **SQL:** Hand-written parameterized queries (`$1`, `$2`...). Results converted via `_row_to_dict`. `_now()` returns ISO UTC strings.
- **Auth/RBAC:** FastAPI dependencies (`get_current_user` → `get_org_context` / `get_project_context` → `require_org_role` / `require_project_role` / `require_superadmin`). Role hierarchy numeric (`ORG_ROLE_HIERARCHY`).
- **Error handling:** `HTTPException` in routes; background tasks catch and log. Deploy tasks implement cancellation via Valkey flags.
- **Logging:** stdlib `logging`, INFO level, to stdout (journalctl in prod). `[TIMING]` logs for performance.
- **Naming:** snake_case Python; files by domain. Global singletons (`cloud_manager`, `caddy_manager`, `storage_manager`, `preview_list_manager`).
- **Config:** single `Settings` instance in `config/settings.py`; per-org settings in DB.
- **Background work:** all deploys/deletes run in arq workers, never in request cycle (enqueued via `request.app.state.arq`).
- **Cross-process coordination:** Valkey pub/sub + shared keys so multiple uvicorn + arq workers stay consistent.

## Deployment

- **systemd services:** `preview-manager.service` (API) and `preview-worker` (arq). Both `Restart=always`.
- **Ansible** (`server/ansible/`) provisions the coordinator: roles for postgresql, caddy, docker, hardening, preview-manager, preview-ui, r2, registry, storage. Secrets in ansible-vault (password `preview-mr`).
- **Caddy** (`docker/docker-compose.caddy.yml`): edge reverse proxy, TLS, routes `api.druploy.dev` → FastAPI, wildcard `*.druploy.dev` → WakePreviewMiddleware. Routes dynamically patched via Caddy Admin API (`app/caddy_api.py`).
- **Postgres + Valkey:** Docker containers (`infra/docker-compose.yml`), localhost only.
- **Production server:** `91.99.157.66`.
- **VM Agent:** deployed to each preview VM; self-updates via `druploy-agent-update` (downloads from `/api/internal/agent/download`).

## Gotchas / Special Notes

1. **Product name is "Druploy"** but repo/module path is `github.com/preview-manager/cli`. Code mixes "preview-manager" (legacy) and "Druploy" (current). Always use "Druploy" in new code.
2. **`settings.previews_base_path`** is referenced in `app/state.py` and `app/deployment.py` but NOT defined in `config/settings.py` — must be set via env or it fails at runtime.
3. **SSH execution uses subprocess `ssh`**, not `asyncssh`. `StrictHostKeyChecking=no` (no host verification — known security TODO).
4. **VM agent `/deploy*` endpoints lack auth** (only `/ws` validates HMAC token). Anyone reaching :8022 can trigger deploys.
5. **GitLab token sent over plain HTTP** to VM agent in clone URLs. Mitigated by intra-datacenter traffic only.
6. **Auto-docs disabled:** `docs_url`, `redoc_url`, `openapi_url` all set to None on the FastAPI app.
7. **Soft-delete + resurrection:** previews retained for `SOFT_DELETE_RETENTION_DAYS` (default 30). Visiting their URL shows "resurrect" splash. Daily cron `task_purge_soft_deleted` hard-deletes after window.
8. **Warm pool:** VMs pre-created (`WARM_POOL_SIZE`) for instant assignment. `task_replenish_warm_pool` runs every 5 min.
9. **Deploy cancellation:** rebuilding cancels in-flight deploy via Valkey flags + VM agent `/deploy/cancel`. Stale locks force-released after timeout.
10. **Worker recovery:** on startup, `recover_interrupted_deployments` marks deployments left "running" as failed. arq retries cancelled jobs but code deliberately does NOT re-raise `CancelledError` to avoid zombie re-enqueues.
11. **CI gating:** previews can wait for GitLab CI success (`require_ci_success` at org/project level, or `ci.required_jobs` in `druploy.yml` in repo).
12. **Caddy route patching:** coordinator dynamically rewrites wildcard Caddy route via Admin API (`localhost:2019`) for per-preview subroutes. Static assets bypass Python.
13. **`scripts/deploy-steps/` is DEPRECATED** — deploy execution moved entirely to VM agent.
14. **`vm-terminal-server` is legacy** — functionality merged into `vm-agent/main.go`.
15. **README.md is a large TODO/notes file** (Spanish) — may contain sensitive info, don't make repo public without auditing.
16. **CLI auto-updates** via `/api/cli/version` polling and `druploy self-update`.
