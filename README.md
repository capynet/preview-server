# Druploy Preview Server

Production server: 91.99.157.66

For local development, having the GitLab, GitHub, and Cloudflare CLIs installed is recommended.

Ansible vault password: **preview-mr**
To view:
```
ansible-vault view inventory/group_vars/all/vault.yml
```
To edit:
```
ansible-vault edit inventory/group_vars/all/vault.yml
```

## Server [server](server)

This directory contains the server application itself:
```
/www/previews/server/preview-manager
```

### Previews

Each preview runs on its own VM (Hetzner Cloud). The code is cloned and executed directly on the VM by the VM Agent.

### Resources (base db and files)

Stored in Hetzner Object Storage (S3).

## UI [ui](ui)

This is the UI of preview manager ([server](server)).

---

## TODO before production

- ~~Necesito un modo mantenimiento que me permita desplegar sin romper deploys activos, que prevenga que los webhooks se pierdan en el limbo y que desactive la UI parcial o completamente hasta que todo acabe.~~ **DONE** — see [Maintenance Mode](#maintenance-mode).
- Use case for marketing: A great use case to exemplify preview usage on branches is when there are two different designs or functionalities for the same feature and a decision needs to be made on which one is better. You can have a preview of each at the same time.
- Review the documentation and add docs on how to configure GitLab.
- Is it possible to be assigned to organizations owned by others? For example, can a user be part of Druploy and Dropsolid while also being owner of their own org?
- Post-deploy still shows the "Deploying" tag even after the main deploy has finished.

## TODO


- https://wterm.dev/
- El codigo fue creciendo con el tiemo y tal vez se podrian refactorizar algunos para achicarlos ya que consumen muchos token de claude cuando hayq ue leer algo.
- We can create a banner in the UI warning that the token has expired and/or could not be renewed, only shown to users with the appropriate permissions to resolve it. Ideally with a link to the area where the new token needs to be set.
- Tighten security. Currently only have fail2ban but need daily backups that allow quick service restoration and confirm that Ansible can rebuild the server from scratch if needed.
- The email system is ready. Only tested adding a user to a group or org when the user doesn't exist yet and when they do. Need to test the rest. Not urgent but needed.
- Need to audit all URLs and endpoints. Make sure a user without privileges cannot access or perform GET or POST actions without the necessary permissions.
- When doing `druploy push db`, there should be a flag to sanitize the DB. In fact, it should sanitize by default.
- Should the admin user be reset? That could be an option in druploy.yml or the UI.
- The Python code has grown over time and large files make it harder for Claude to analyze and make changes as they consume many tokens. If so, it would be great to do a refactor and split files into smaller ones, ideally by functionality: organization, user, account, emails, etc.
- Sometimes you want to send a preview to Druploy even if the required_jobs rule doesn't pass. It would be good to be able to disable at the preview level (or at the project level at least specify which branches or MRs are bypassed so the first preview can be created).
- Previews should show the VM creation date, last update, and time remaining until deletion if not pinned.
- Ability to configure a project to use larger VMs. Needed for projects that are resource-heavy.
- Would like to see if a cron job is currently running.
- When an MR is merged or closed, the comment with Druploy info could be removed.
- Need to improve the comments posted on MRs. Sometimes a new one is created by accident. Need to add an extra check: at the hash level (the one used in the preview URL). Search for its presence. Maybe it's already done. The point is to avoid having more than one comment on an MR.
- Allow auto-erase to be configured in hours instead of days.
- Fix the ability to log in with a GitLab account.
- If an MR contains no changes at the GitLab level, nothing is done. It just informs that there are no changes. In that type of MR, we should do the same as with draft MRs: nothing.
- Drupal has a scaffold that could be configured automatically as an initial project configuration.
- There are logs inside the container that would be good to expose via `preview logs` (all), `preview logs cron`, etc., or even `preview logs pull` to download them locally and process them.
- Env vars cannot be disabled. Should be able to toggle active/inactive in case a specific preview doesn't need one because testing requires that value to be absent.
- When an MR is closed, it would be nice to delete the Druploy comment.
- Replace PAT with OAuth for GitLab connection?
- When opening SSH to a preview (drush ssh), would like a console with colors.
- The CLI should be served from a static location. If downloads scale up, it will crash the server.
- GitLab has a new granular token type. Need to support it.
- Need some form of feedback in the UI. A popup that allows quickly attaching a video or image and a comment. So users can report what needs to be improved or fixed.
- Need the CLI to have auto error reporting. Before using it, we'll need to add a message for the user to confirm allowing us to send anonymized information to improve the tool. Once approved, if a command fails or crashes, useful information should be sent so we can fix the error.
- Could each tenant or organization configure their own domains? The preview URL would still work. The custom domain would be layered on top. Note: can only allow subdomains of their registered domain. At most, allow subdomains of an alternative site but never base domains so they don't use it as a website.
- The backend already has the endpoints (`/api/config/cloud-resources` and `/api/config/cloud-costs`), but the frontend UI to display them is missing.
- Should there be a script that scans for orphaned previews?
- Need a check to prevent the server from crashing if disk space runs out.
- When going to production, need to disable Next.js dev tools and `productionBrowserSourceMaps`.
- Need statistics on how many previews are created per project, how many rebuilds and updates. Any info that allows usage statistics.
- Need to run the entire solution on an alternative server for developing new features. Or at least be able to run it entirely locally.
- Need MRs to abort the current build when a new hook arrives.
- Need a button to abort the current build. Useful for example if you need to add an env var. Instead of waiting for it to finish, you can abort and relaunch.
- 2FA needed, especially for the superadmin, but really for anyone since DBs are sensitive.
- Need a free tier and a paid tier. The free tier allows creating only one preview per project with no other limits. (Expand on this idea.)
- Extract all sensitive configuration (keys, tokens, etc.) to a secure and configurable location so they can be updated in the future without much hassle.
- Need some sanitization for uploaded DBs, or does the developer handle that?
- I think this already exists: Ability to specify a custom deployment process per branch! (Super useful when creating something new that requires specific configuration). Probably needs to be specified from the branch config since you can't be committing changes just for testing.
- Once everything is stable, remove the `set -x` debug.
- Multisite support. Already implemented but some projects are edge cases. Need real multisite support.
- Use case to cover: if someone has a custom platform like Dropsolid's DXP, if they want to provide link, uli, and other options, the CLI suffices, right? (in terms of integration).
- Idea: route logs to somewhere we can access visually. Maybe not titanic tools but visualizers that allow viewing, and a CLI command for log streaming and a download link.
- Need Drupal to store its logs alongside Apache, PHP, etc. in a centralized, easy-to-review location. ELK or something simpler?
- Need simple RAM, CPU, and disk stats for an easy overview.
- Need mailpit but also a UI config to disable it per preview (there are cases where emails need to be confirmed).
- Support Drupal decoupled (additional Node.js container) — to compete with Upsym in that niche.
- For this to work, freelance users should have this for free, or at least the option to self-host, or a 0€ tier.
- Preview autocompletion should happen automatically instead of needing to run the command manually.
- Is it possible to support Apache or OpenLiteSpeed interchangeably?
- As an alternative to Varnish, there's LiteSpeed Cache with some Drupal integration. Not sure if there's a module; if not, one needs to be made.
- In the "New Preview from Branch" modal, want branches listed from newest to oldest.
- Use GitHub Actions to compile the CLI and UI.
- Should provide MCP support to connect to previews from local.
- Need to support specifying an arbitrary DB or files dir in `preview push db/files`, and accept `.sql`, `.sql.gz`, `files.tgz`, or `tar.gz`.
- When the DB cache is created, it happens during the first preview creation. Wonder if it's possible to move that to a background process so it doesn't block preview generation. If not possible, at least give more info: "Creating DB cache for use in subsequent previews." And if progress can be reported, even better.
- Need AI integration where it makes sense: "enable the projects with most activity", "if you see previews that are never visited after being created in a particular project, better not create previews and inform the user that previews aren't being created due to lack of use." Allow setting an expectation for auto-erase timing, e.g. "in this project we usually review previews on a specific day, so it doesn't make sense to create them automatically. Better create them when an MR to master named 'Release XXX' is created."
- Want to mark some branch or MR previews as favorites. When the preview is deleted, the favorite disappears silently.
- Parent and child visualization? For example, all previews targeting master hang from master, those for d11 hang from d11, etc.
- Storage Box is half-implemented. It's a bit slower but much cheaper than R2. Claude has memory: for now the output comes out badly encoded and the preview server gets saturated processing the output.
- The preview agent could capture anything happening on the VM, e.g. CPU, RAM, and disk usage shown on the preview detail page.
- "Use weekly statistics of most-used Docker images to automatically regenerate the VM snapshot with the images most likely to be used, reducing pull time during deploys."
- "Pre-load Docker images in the VM snapshot to eliminate pull in most deploys, keeping the 'Pulling Docker images' phase as fallback for images not included in the snapshot."
- Need telemetry in preview commands to know if they fail and have output to fix them. Obviously, we need to ask users for permission to receive anonymous statistics before doing so.
- When creating a preview and there's no pool ready, a VM will be created for that preview, and if deleted within a few seconds, the VM will be orphaned. Seems like the vm_id isn't saved in the DB yet when the preview is deleted, but it should be saved as soon as the VM is assigned.
- Do deploy scripts have access to a visual TUI toolkit if provided externally? I mean some console graphical toolkit that can simply be used because it's available on the VM or Docker (not sure where it actually runs).
- Going to change build numbering. Currently shared across all VMs of all orgs. Need it to be per org, user, or project.
- Would like to upload environment variables from the UI, e.g. upload a JSON file instead of copying and pasting. Makes sense?

## Maintenance Mode

Lets you redeploy the control-plane (`druploy` API + `druploy-worker`) **without breaking active preview deploys, without losing webhooks, and with the UI read-only**. It works by *draining*: park new deploys, let in-flight ones finish, restart, resume.

### One-time setup

The drain is driven by a machine token. Add it to the vault (already done in this repo, but for a fresh environment):

```bash
cd server/ansible
ansible-vault edit inventory/group_vars/all/vault.yml    # add: vault_admin_api_token: "<long-random-secret>"
```

It flows through: `vault_admin_api_token` → `admin_api_token` (hosts.yml) → `ADMIN_API_TOKEN` (env) → API. **Until it is set, deploys skip the drain** (guarded by a length check), so nothing breaks. The token only reaches the server's `.env` after a deploy that regenerates it from `env.j2`.

### Normal deploys (automatic)

Once the token is set, the normal deploy drains automatically — no extra steps:

```bash
ansible-playbook playbooks/deploy-preview-manager.yml
```

It runs: **enable maintenance → poll until `active_deploys == 0` (up to 15 min) → restart worker/API → clear maintenance**. Parked deploys resume on their own. To skip the drain for a one-off: `-e drain=false`. To wait longer for a slow deploy: `-e maintenance_drain_retries=120` (×15s).

### Manual control

```bash
# Enter maintenance and drain (does NOT deploy) — e.g. before a manual op
ansible-playbook playbooks/maintenance-drain.yml
ansible-playbook playbooks/maintenance-drain.yml -e maintenance_level=full   # also block the UI

# Lift maintenance (resume parked deploys, re-enable UI)
ansible-playbook playbooks/maintenance-clear.yml
```

Or from the **`/admin`** page in the dashboard (superadmin): the *Maintenance mode* card toggles it (reason + `drain`/`full`).

Or via the API directly:

```bash
curl -X POST https://api.druploy.dev/api/admin/maintenance \
  -H "X-Admin-Token: <admin_api_token>" -H "Content-Type: application/json" \
  -d '{"active": true, "level": "drain", "reason": "hotfix"}'

curl https://api.druploy.dev/api/health | jq .maintenance   # {active, level, active_deploys}
```

### Behavior while active

- **Deploys/deletes**: new ones are *parked* (re-enqueued every 30s, durable up to 6h) and resume when maintenance clears; in-flight deploys finish normally.
- **Webhooks**: still accepted and stored durably in the `webhook_inbox` table (never lost); the resulting deploy just parks. A per-minute cron re-drives anything stuck.
- **UI**: `drain` → banner + management read-only; `full` → full-screen block (except `/admin`, so you can still turn it off). The API also returns **503** for mutating `/api/**` calls.
- **`level`**: use `drain` for a normal deploy; use `full` only if a migration is destructive and you need the UI fully frozen.

### Notes

- Keep Alembic migrations **expand/contract** (additive first): during the drain window the old worker runs against the new schema.
- On the very first deploy that ships this feature, the enable call may 404 (old API without the endpoint) — the playbook tolerates it and just skips the drain that once.

## GitLab Setup

To guide users, document how to connect GitLab to the preview app:

Go to https://gitlab.com/oauth/applications
Add an application:

**"User login"**
- Callback: https://api.druploy.dev/api/gitlab/auth/callback
- Scopes: read_user

Then create a new app to connect the API:
**"Previews API"**
- Callback: https://api.druploy.dev/api/gitlab/connect/callback
- Scopes: api

> **WARNING**: The GitLab OAuth application secrets were previously stored in plaintext in this README. They have been removed. Check the Ansible vault (`inventory/group_vars/all/vault.yml`) for current secrets.

---

## Feature: Import DB directly to a preview

### Problem
Currently there is only one base DB per project. Need to be able to have previews with different DBs (e.g. main with live DB, develop with sanitized DB).

### Solution
New `--target` flag in `preview push db` that imports the DB directly into an existing preview, instead of uploading it as a base file for the project.

### Flow
```
preview push db --target=branch-main
```

1. CLI does local dump (drush sql:dump, as it already does)
2. CLI uploads the gzip to `POST /api/previews/{project}/{preview_name}/db/import`
3. Backend receives the gzip and runs `gunzip | docker exec -i {container}-db mysql -u drupal -pdrupal drupal`

### Required changes

**CLI:**
- `--target=<preview_name>` flag in `push db`
  - If `--target` is passed, send to the import endpoint instead of the base files endpoint

**Backend:**
- New endpoint `POST /api/previews/{project}/{preview_name}/db/import`
  - Receives the gzip as body/multipart
  - Runs the import in the DB container of that preview
  - Optionally runs `drush cr` afterwards

### Advantages
- No extra config, no variants, no drush aliases
- Reuses the dump the CLI already knows how to make
- Each preview can have whatever DB you want without affecting others
- Backward compatible: `preview push db` without `--target` still works as before

---

## Docker image snapshot for DB

Question: Could you have a snapshot of a prepared Docker image to be reused in seconds? For example, the DB docker image is always the same on each rebuild until a new DB is loaded.

### Options for the DB case:

1. **Pre-loaded image with docker commit**
   After importing the base DB, do `docker commit` of the MySQL container as a custom image (e.g. `preview-db-soudal:latest`). New previews use that image and start with data already loaded. Problem: MySQL stores data in a volume, not in the container layer, so that would need adjusting.

2. **Custom image with baked-in data (more robust)**
   Create a Dockerfile that starts MySQL, imports the dump, and saves the datadir inside the image. Each time a new base DB is uploaded, the image is rebuilt. Previews start in seconds with data ready.

3. **Volume snapshots (faster, more complex)**
   With ZFS or btrfs you can do instantaneous snapshots of the data volume. Cloning a 500MB volume takes milliseconds. But requires the server to use those filesystems.

The most practical option would be #2: when someone uploads a new base DB via `preview push db`, the backend:
1. Spins up a temporary MySQL container
2. Imports the dump
3. Does `docker commit` -> `preview-db:{project}:latest`
4. New previews use that image instead of `mysql:8.0` + import

---

## Hardening: Coordinator <-> VM Agent channel

- [ ] **Firewall on VMs** (highest impact, lowest risk). Add a Hetzner Cloud firewall when creating the VM in `cloud.py` that only allows :8022 (agent) and :2222 (SSH container) from the coordinator's IP. Currently VMs have public IP with all ports open (iptables ACCEPT, ufw inactive).
- [ ] **Token on /deploy endpoint**. Currently only `/terminal` validates the HMAC (validateToken); `/deploy`, `/deploy/status`, `/deploy/logs`, `/info`, `/containers`, `/ssh-keys` have no auth. Anyone reaching :8022 can trigger deploys or read info.
- [ ] **GitLab token in plaintext over HTTP**. The `/deploy` payload includes `GitCloneURL = https://oauth2:{gitlab_token}@.../repo.git` and travels unencrypted over HTTP (`http://{vm_ip}:8022/deploy`). Partially mitigated because it's intra-datacenter traffic (Hetzner Falkenstein), but the token is exposed.
- [ ] **Encrypt coordinator -> agent channel**. Switch :8022 from plain HTTP to HTTPS (TLS, even self-signed with pinning) or tunnel it. This resolves the two issues above at the root.

## Hardening: CLI <-> Preview channel (lower priority)

- [ ] **No host verification in SSH**. The CLI uses `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` in ssh/push files/push db. Traffic is encrypted, but there's no TOFU: an active MITM could impersonate the preview. Evaluate pinning the VM host key (the coordinator already knows its fingerprint).

---

## Preview resurrection

If a preview is deleted (e.g. by auto-erase), the URL should trigger an initial build to reconstruct the image if the MR or branch still exists.
Ideally with a splash screen: "The preview you are trying to access does not currently exist. If you want to create it, confirm this message and in a few minutes it will be available. You don't need to close this tab, as soon as the build finishes the page will be displayed."
