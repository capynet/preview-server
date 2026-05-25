# CLI

The `druploy` CLI lets you manage previews from your terminal — list, ssh, run drush, push base DB/files, rebuild from a branch, and more.

## Context-aware

The CLI is **context-aware**: when you run it from inside a project's git working tree, it figures out the project and preview from your current branch — no need to pass them as arguments.

For example, from a checked-out feature branch with an open MR:

```bash
druploy update     # updates the preview for this MR
druploy ssh        # SSH into the preview's PHP container
druploy drush cr   # runs `drush cr` on this MR's preview
```

The same commands work from a branch preview (e.g. `develop`) — `druploy update`, `druploy ssh`, `druploy drush` all resolve to the `branch-develop` preview automatically.

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

| Command           | Usage                                  | Description                                                                                                                                                                                                                                                                                  |
|-------------------|----------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `login`           | `druploy login`                        | Authenticate with Preview Manager via browser (device flow). Use `--no-browser` to print the URL instead of opening it.                                                                                                                                                                      |
| `logout`          | `druploy logout`                       | Log out and clear saved credentials.                                                                                                                                                                                                                                                         |
| `whoami`          | `druploy whoami`                       | Show the current authenticated user (name, email, role).                                                                                                                                                                                                                                     |
| `list`            | `druploy list [PROJECT]`               | List previews. Interactive project selector if none given. Use `--no-status` to skip the Docker status check (faster).                                                                                                                                                                       |
| `ssh`             | `druploy ssh [container]`              | Connect directly to the preview's PHP container where Drupal runs. Auto-detects from your git branch. Use `druploy ssh db` for the database container. SSH key is registered automatically on first use.                                                                                     |
| `drush`           | `druploy drush [args...]`              | Run a `drush` command on the preview via direct SSH. Auto-detects project and preview from the current git branch. Results are cached — subsequent calls skip resolution. All arguments are passed through to drush.                                                                         |
| `update`          | `druploy update`                       | Update the preview with the latest code from the current branch. Syncs code, runs `composer install` and `update` deploy scripts without re-importing the database.                                                                                                                          |
| `rebuild`         | `druploy rebuild`                      | Rebuild the preview from scratch — new VM, fresh deploy with DB and files import. Auto-detects from the current git branch.                                                                                                                                                                  |
| `push db`         | `druploy push db [file.sql.gz]`        | Upload a base database to the server. Auto-detects project from git remote.                                                                                                                                                                                                                  |
| `push files`      | `druploy push files [file.tar.gz]`     | Upload base files to the server. Auto-detects project from git remote. `--no-image-styles` excludes `styles/` (Drupal regenerates them on demand). `--strip-heavy-files SIZE` excludes files larger than `SIZE` (e.g. `2mb`).                                                                |
| `setup`           | `druploy setup`                        | Scaffold a Drupal project for preview environments. Run from the project root.                                                                                                                                                                                                               |
| `self-update`     | `druploy self-update`                  | Update the CLI to the latest version.                                                                                                                                                                                                                                                        |

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
