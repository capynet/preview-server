# CLI

The `druploy` CLI lets you manage previews from your terminal — list, ssh, generate drush aliases, push base DB/files, rebuild from a branch, and more.

## Context-aware

The CLI is **context-aware**: when you run it from inside a project's git working tree, it figures out the project and preview from your current branch — no need to pass them as arguments.

For example, from a checked-out feature branch with an open MR:

```bash
druploy preview update      # updates the preview for this MR
druploy preview ssh         # SSH into the preview's PHP container
druploy gen-drush-aliases   # generates drush aliases for this MR's preview
```

The same commands work from a branch preview (e.g. `develop`) — they all resolve to the `branch-develop` preview automatically.

For drush, the workflow is: run `druploy gen-drush-aliases` once, then use native drush from your project — `drush @druploy.default status`, `drush @druploy.default cr`, etc.

If you're outside a project directory, pass the project/preview explicitly: `druploy list my-project`, `druploy preview ssh my-project/mr-42`, etc.

---

## Installation

One-line install. Supports Linux and macOS (`amd64` and `arm64`):

```bash
curl -fsSL https://api.druploy.dev/api/cli/install.sh | sh
```

The CLI installs into `~/.local/bin/` — no `sudo` required.

---

## Quick start

1. Authenticate (opens a browser for device-flow login):

    ```bash
    druploy login
    ```

2. List your previews:

    ```bash
    druploy list
    ```

3. From inside a project directory, jump into the preview's PHP container:

    ```bash
    druploy preview ssh
    ```

---

## Commands

Commands are grouped by **where they act**: on your local machine, on shared project resources, or on a remote preview VM. The same grouping appears in `druploy --help`.

When `PROJECT/PREVIEW` is not provided, the CLI auto-detects it from the current git remote and branch — see [Context-aware](#context-aware). Commands that act remotely always print the resolved target before doing anything:

```
→ preview: my-site/mr-1597 (auto-detected from branch "feature/foo")
```

### Local commands (act on your machine / working copy)

| Command | Description |
|---|---|
| `druploy setup` | Scaffold a Drupal project for previews: creates `druploy.yml`, `web/sites/default/settings.druploy.php`, and `scripts/druploy/`. Run from the project root.<br>**Usage:** `druploy setup [--override]`<br>**Options:**<br>• `--override` — overwrite existing files with the latest templates. _Example:_ `druploy setup --override` |
| `druploy gen-drush-aliases` | Generate `drush/sites/druploy.site.yml` with site aliases pointing to the preview. After running, use native drush: `drush @druploy.default status`, `drush @druploy.default cr`, etc.<br>**Usage:** `druploy gen-drush-aliases [PROJECT/PREVIEW]`<br>**Options:**<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy gen-drush-aliases my-site/branch-develop` |

### Project commands (act on shared project resources)

The base DB and files are **project-level resources**: every new preview of the project is seeded from them.

| Command | Description |
|---|---|
| `druploy list` | List the previews of a project. Auto-detects the project from the git remote, or opens an interactive selector.<br>**Usage:** `druploy list [PROJECT] [--no-status]`<br>**Options:**<br>• `PROJECT` — project slug to list previews for. _Example:_ `druploy list my-drupal-site`<br>• `--no-status` — skip the Docker status check (faster). _Example:_ `druploy list --no-status` |
| `druploy project push db` | Upload a base database used to seed every new preview. By default, generates a dump from local DDEV (excluding `cache_*` tables) and uploads it.<br>**Usage:** `druploy project push db [FILE] [-y\|--yes]`<br>**Options:**<br>• `FILE` — path to an existing `.sql.gz` to upload directly, skipping the dump step. _Example:_ `druploy project push db ./base.sql.gz`<br>• `-y`, `--yes` — skip confirmation prompts. _Example:_ `druploy project push db -y` |
| `druploy project push files` | Upload the base files archive. By default, packages the local Drupal files dir into `.tar.gz` and uploads it.<br>**Usage:** `druploy project push files [FILE] [--no-image-styles] [--strip-heavy-files SIZE] [-y\|--yes]`<br>**Options:**<br>• `FILE` — path to an existing `.tar.gz` to upload directly, skipping packaging. _Example:_ `druploy project push files ./files.tar.gz`<br>• `--no-image-styles` — exclude `styles/` (Drupal regenerates them on demand). _Example:_ `druploy project push files --no-image-styles`<br>• `--strip-heavy-files SIZE` — exclude files larger than `SIZE`. _Example:_ `druploy project push files --strip-heavy-files 10mb`<br>• `-y`, `--yes` — skip confirmation prompts. _Example:_ `druploy project push files -y` |

### Preview commands (act on a remote preview VM)

| Command | Description |
|---|---|
| `druploy preview ssh` | Open an interactive shell in a container on the preview VM. SSH key is registered on first use.<br>**Usage:** `druploy preview ssh [container] [PROJECT/PREVIEW]`<br>**Options:**<br>• `container` — `php` (default, lands in `/var/www/html`) or `db`. _Example:_ `druploy preview ssh db`<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy preview ssh db my-site/mr-1597` |
| `druploy preview update` | Update the preview with the latest code from the current branch — syncs code, runs `composer install` and `update` deploy scripts. Does **not** re-import the database or files.<br>**Usage:** `druploy preview update` |
| `druploy preview rebuild` | Rebuild the preview from scratch — new VM, fresh deploy with DB and files import. Asks for confirmation before destroying the current VM.<br>**Usage:** `druploy preview rebuild [PROJECT/PREVIEW] [-y\|--yes]`<br>**Options:**<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy preview rebuild my-site/mr-1597`<br>• `-y`, `--yes` — skip the confirmation prompt. _Example:_ `druploy preview rebuild -y` |
| `druploy preview push db` | Dump the local database (via ddev, cache tables structure-only) and import it into **this preview only**, replacing its current database. Rebuilds caches afterwards (`drush cr`). The project's base DB is not touched.<br>**Usage:** `druploy preview push db [PROJECT/PREVIEW] [-y\|--yes]`<br>**Options:**<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy preview push db my-site/mr-1597`<br>• `-y`, `--yes` — skip the confirmation prompt. |
| `druploy preview push files` | Rsync the local Drupal files dir to **this preview only** (the project's base files are not touched). Additive by default — nothing is deleted on the preview. Same exclusions as `project push files`. Shows a payload summary before asking for confirmation.<br>**Usage:** `druploy preview push files [PROJECT/PREVIEW] [--dry-run] [--replace] [--no-image-styles] [--strip-heavy-files SIZE] [-y\|--yes]`<br>**Options:**<br>• `--dry-run` — compact local report (no connection, nothing sent): unfiltered dir size, payload after filters, and how much the filters save. _Example:_ `druploy preview push files --dry-run --strip-heavy-files 5mb`<br>• `--replace` — wipe the preview's files dir completely before sending, so it ends up containing exactly what you send.<br>• `--no-image-styles` — exclude `styles/`.<br>• `--strip-heavy-files SIZE` — exclude files larger than `SIZE`.<br>• `-y`, `--yes` — skip confirmation prompts. |

!!! warning "Moved in v2.0"
    The old top-level forms (`druploy ssh`, `druploy update`, `druploy rebuild`, `druploy push`) were removed in v2.0. Running them prints an error pointing to the new location.

### CLI commands

| Command | Description |
|---|---|
| `druploy login` | Authenticate via browser (device flow).<br>**Usage:** `druploy login [--no-browser]`<br>**Options:**<br>• `--no-browser` — print the authorization URL instead of opening a browser. _Example:_ `druploy login --no-browser` |
| `druploy logout` | Log out and clear saved credentials. |
| `druploy whoami` | Show the current authenticated user (name, email, role). |
| `druploy self-update` | Update the CLI to the latest version in place. |

---

## Changelog

### v2.2 — 2026-06-10

**Added**

- `druploy preview push db`: replace a single preview's database with your local one (dump via ddev, import over SSH, `drush cr` afterwards). The project's base DB is untouched.

### v2.1 — 2026-06-10

**Added**

- `druploy preview push files`: rsync the local Drupal files dir directly to a single preview (the project's base files are untouched). Supports the same exclusion flags as `project push files`, plus `--replace` (wipes the preview's files dir before sending) and `--dry-run` (local payload report to tune exclusions before sending).

### v2.0 — 2026-06-10

**Breaking**

- Noun-verb command structure (gcloud style): preview operations now live under the `preview` noun — `druploy preview ssh`, `druploy preview update`, `druploy preview rebuild`.
- Removed top-level `druploy ssh`, `druploy update`, `druploy rebuild` and the deprecated `druploy push`. Running them prints an error pointing to the new command.

### v1.10 — 2026-06-10

**Added**

- Command groups in help: `druploy --help` now groups commands by where they act — Local, Project, Preview, and CLI.
- `druploy project push db|files`: new canonical location for pushing base DB/files, making explicit that they are project-level resources shared by every preview.
- Target announcement: remote commands print the resolved target (e.g. `→ preview: my-site/mr-123 (auto-detected from branch "feature/foo")`) before doing anything.
- Rebuild confirmation: rebuild asks for confirmation before destroying the current VM. Use `-y`/`--yes` to skip in scripts/CI.

**Deprecated**

- `druploy push` — replaced by `druploy project push` (removed in v2.0).

### v1.9 — 2026-03-16

**Added**

- `druploy ssh`: Direct SSH into preview containers (PHP or DB). Auto-detects project/preview from git branch. Registers SSH key on first use.
- `druploy ssh db`: Access the database container directly.
- Cached resolution: project/preview detection is cached — subsequent commands skip API calls for instant execution.
- Automatic SSH key registration: first `ssh`/`drush` command prompts to register your local SSH key.
- Copyable SSH command shown in the preview detail page for quick access.

**Changed**

- `druploy drush`: Now runs via direct SSH instead of the API. No timeouts, full output, supports all drush commands including interactive ones.
- Smart retry: if a command fails, the CLI refreshes the cached preview info and retries automatically.
- Preview readiness check: `ssh`/`drush` commands show clear messages when a preview is creating, failed, or being deleted.

### v1.0.5 — 2026-02-17

**Added**

- Self-update: `druploy self-update` command to update the CLI in place.
- Login guard: `druploy login` now warns if already logged in and shows current user info.

**Changed**

- Install location: CLI now installs to `~/.local/bin/` instead of `/usr/local/bin/` (no sudo required).
- Version check: update notification is fully non-blocking (uses cached data from previous run).
- Version format: switched to semantic versioning (`1.x.x`).

### v1.0.0 — 2026-02-17

**Added**

- Authentication: `login`, `logout`, `setup` commands with device flow support.
- Preview management: `list`, `start`, `stop`, `restart`, `rebuild` commands.
- Drush integration: `uli` (user login) and arbitrary drush command execution.
- Downloads: `db` (database dump) and `files` (tar.gz archive) commands.
- Push: `push` command to trigger preview deployments.
- Project setup: `setup` command for per-project configuration.
- Version check: automatic update notification with 24 h cache.
- Cross-platform: binaries for `linux/amd64`, `linux/arm64`, `darwin/amd64`, `darwin/arm64`.
