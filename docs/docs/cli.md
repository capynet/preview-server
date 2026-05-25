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

Arguments shown in `[brackets]` are optional. When `PROJECT/PREVIEW` is optional, the CLI auto-detects it from the current git remote and branch — see [Context-aware](#context-aware).

| Command | Description |
|---|---|
| `druploy login [--no-browser]` | Authenticate via browser (device flow). `--no-browser` prints the URL instead of opening it. |
| `druploy setup [--override]` | Scaffold a Drupal project for previews: creates `druploy.yml`, `web/sites/default/settings.druploy.php`, and `scripts/druploy/`. Run from the project root. `--override` overwrites existing files. |
| `druploy logout` | Log out and clear saved credentials. |
| `druploy whoami` | Show the current authenticated user (name, email, role). |
| `druploy list [PROJECT] [--no-status]` | List previews. With no `PROJECT`, opens an interactive selector. `--no-status` skips the Docker status check (faster). |
| `druploy ssh [container] [PROJECT/PREVIEW]` | Open an interactive shell in a container on the preview VM. `container` is `php` (default, lands in `/var/www/html`) or `db`. SSH key is registered on first use. |
| `druploy gen-drush-aliases [PROJECT/PREVIEW]` | Generate `drush/sites/druploy.site.yml` with site aliases pointing to the preview. After running, use native drush: `drush @druploy.default status`, `drush @druploy.default cr`, etc. |
| `druploy update` | Update the preview with the latest code from the current branch — syncs code, runs `composer install` and `update` deploy scripts. Does **not** re-import the database or files. |
| `druploy rebuild [PROJECT/PREVIEW]` | Rebuild the preview from scratch — new VM, fresh deploy with DB and files import. |
| `druploy push db [FILE] [-y\|--yes]` | Upload a base database used to seed every new preview. With no `FILE`, generates a dump from local DDEV (excluding `cache_*` tables) and uploads it. Pass a `.sql.gz` path to upload directly. `-y`/`--yes` skips confirmation. |
| `druploy push files [FILE] [--no-image-styles] [--strip-heavy-files SIZE] [-y\|--yes]` | Upload the base files archive. With no `FILE`, packages the local Drupal files dir into `.tar.gz`. Pass a `.tar.gz` path to upload directly. `--no-image-styles` excludes `styles/` (Drupal regenerates them). `--strip-heavy-files SIZE` excludes files larger than `SIZE` (e.g. `10mb`). `-y`/`--yes` skips confirmation. |
| `druploy self-update` | Update the CLI to the latest version in place. |

### `ssh` examples

```bash
druploy ssh                       # auto-detect, php container
druploy ssh db                    # auto-detect, db container
druploy ssh my-site/mr-1597       # explicit preview, php container
druploy ssh db my-site/mr-1597    # explicit preview, db container
```

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
