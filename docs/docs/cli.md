# CLI

The `druploy` CLI lets you manage previews from your terminal — list, ssh, generate drush aliases, push base DB/files, rebuild from a branch, and more.

## Context-aware

The CLI is **context-aware**: when you run it from inside a project's git working tree, it figures out the project and preview from your current branch — no need to pass them as arguments.

For example, from a checked-out feature branch with an open MR:

```bash
druploy update              # updates the preview for this MR
druploy ssh                 # SSH into the preview's PHP container
druploy gen-drush-aliases   # generates drush aliases for this MR's preview
```

The same commands work from a branch preview (e.g. `develop`) — they all resolve to the `branch-develop` preview automatically.

For drush, the workflow is: run `druploy gen-drush-aliases` once, then use native drush from your project — `drush @druploy.default status`, `drush @druploy.default cr`, etc.

If you're outside a project directory, pass the project/preview explicitly: `druploy list my-project`, `druploy ssh my-project/mr-42`, etc.

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
    druploy ssh
    ```

---

## Commands

When `PROJECT/PREVIEW` is not provided, the CLI auto-detects it from the current git remote and branch — see [Context-aware](#context-aware).

| Command | Description |
|---|---|
| `druploy login` | Authenticate via browser (device flow).<br>**Usage:** `druploy login [--no-browser]`<br>**Options:**<br>• `--no-browser` — print the authorization URL instead of opening a browser. _Example:_ `druploy login --no-browser` |
| `druploy setup` | Scaffold a Drupal project for previews: creates `druploy.yml`, `web/sites/default/settings.druploy.php`, and `scripts/druploy/`. Run from the project root.<br>**Usage:** `druploy setup [--override]`<br>**Options:**<br>• `--override` — overwrite existing files with the latest templates. _Example:_ `druploy setup --override` |
| `druploy logout` | Log out and clear saved credentials. |
| `druploy whoami` | Show the current authenticated user (name, email, role). |
| `druploy list` | List previews. Opens an interactive project selector if no project is given.<br>**Usage:** `druploy list [PROJECT] [--no-status]`<br>**Options:**<br>• `PROJECT` — project slug to list previews for. _Example:_ `druploy list my-drupal-site`<br>• `--no-status` — skip the Docker status check (faster). _Example:_ `druploy list --no-status` |
| `druploy ssh` | Open an interactive shell in a container on the preview VM. SSH key is registered on first use.<br>**Usage:** `druploy ssh [container] [PROJECT/PREVIEW]`<br>**Options:**<br>• `container` — `php` (default, lands in `/var/www/html`) or `db`. _Example:_ `druploy ssh db`<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy ssh db my-site/mr-1597` |
| `druploy gen-drush-aliases` | Generate `drush/sites/druploy.site.yml` with site aliases pointing to the preview. After running, use native drush: `drush @druploy.default status`, `drush @druploy.default cr`, etc.<br>**Usage:** `druploy gen-drush-aliases [PROJECT/PREVIEW]`<br>**Options:**<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy gen-drush-aliases my-site/branch-develop` |
| `druploy update` | Update the preview with the latest code from the current branch — syncs code, runs `composer install` and `update` deploy scripts. Does **not** re-import the database or files.<br>**Usage:** `druploy update` |
| `druploy rebuild` | Rebuild the preview from scratch — new VM, fresh deploy with DB and files import.<br>**Usage:** `druploy rebuild [PROJECT/PREVIEW]`<br>**Options:**<br>• `PROJECT/PREVIEW` — explicit preview (otherwise auto-detected). _Example:_ `druploy rebuild my-site/mr-1597` |
| `druploy push db` | Upload a base database used to seed every new preview. By default, generates a dump from local DDEV (excluding `cache_*` tables) and uploads it.<br>**Usage:** `druploy push db [FILE] [-y\|--yes]`<br>**Options:**<br>• `FILE` — path to an existing `.sql.gz` to upload directly, skipping the dump step. _Example:_ `druploy push db ./base.sql.gz`<br>• `-y`, `--yes` — skip confirmation prompts. _Example:_ `druploy push db -y` |
| `druploy push files` | Upload the base files archive. By default, packages the local Drupal files dir into `.tar.gz` and uploads it.<br>**Usage:** `druploy push files [FILE] [--no-image-styles] [--strip-heavy-files SIZE] [-y\|--yes]`<br>**Options:**<br>• `FILE` — path to an existing `.tar.gz` to upload directly, skipping packaging. _Example:_ `druploy push files ./files.tar.gz`<br>• `--no-image-styles` — exclude `styles/` (Drupal regenerates them on demand). _Example:_ `druploy push files --no-image-styles`<br>• `--strip-heavy-files SIZE` — exclude files larger than `SIZE`. _Example:_ `druploy push files --strip-heavy-files 10mb`<br>• `-y`, `--yes` — skip confirmation prompts. _Example:_ `druploy push files -y` |
| `druploy self-update` | Update the CLI to the latest version in place. |

---

## Changelog

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
