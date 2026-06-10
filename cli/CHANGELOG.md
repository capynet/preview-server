# Changelog

All notable changes to the Preview CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-06-10

### Added

- **`druploy preview push db`**: replace a single preview's database with your local one. Dumps via ddev (cache tables structure-only, same as `project push db`), streams it over SSH into `drush sql:cli` (dropping the old DB first) and rebuilds caches with `drush cr`. The project's base DB is not touched. `-y` to skip confirmation.

## [2.1.4] - 2026-06-10

### Added

- **`preview push files`**: the payload summary (unfiltered size, filtered payload, savings) is now also shown before the confirmation prompt on a real push, so you know what you're about to send.

## [2.1.2] - 2026-06-10

### Changed

- **`preview push files --dry-run`**: compact summary (unfiltered size, filtered payload, savings) instead of rsync's full stats block. The real sync no longer prints the stats block either — the progress bar already shows totals.

## [2.1.1] - 2026-06-10

### Changed

- **`preview push files --replace`**: now wipes the preview's files dir completely before sending (the dir ends up containing exactly what you send), instead of rsync `--delete` mirroring.
- **`preview push files --dry-run`**: now computes the payload locally (against an empty dir) — no connection to the preview needed, and the report reflects the full payload regardless of what's already on the preview.

## [2.1.0] - 2026-06-10

### Added

- **`druploy preview push files`**: rsync the local Drupal files dir directly to a single preview, without touching the project's base files. Same exclusions as `project push files` (`css/js/php` always, `--no-image-styles`, `--strip-heavy-files`), plus:
  - `--replace` — full mirror: deletes files on the preview that don't exist locally (excluded dirs and size-stripped files are never deleted).
  - `--dry-run` — size report ("Total transferred file size") without sending anything, to tune the exclusion flags.

## [2.0.0] - 2026-06-10

### Changed

- **Noun-verb command structure (gcloud style)**: preview operations now live under the `preview` noun — `druploy preview ssh`, `druploy preview update`, `druploy preview rebuild`. `list` stays top-level as the everyday read-only command.

### Removed

- **Top-level `ssh`, `update`, `rebuild` and the deprecated `push`**: running them now prints an error pointing to the new command (`druploy preview <verb>` / `druploy project push`).

## [1.10.0] - 2026-06-10

### Added

- **Command groups in help**: `druploy --help` now groups commands by where they act — Local (your machine / working copy), Project (shared project resources), Preview (remote preview VM), and CLI.
- **`druploy project push db|files`**: New canonical location for pushing base DB/files, making explicit that they are project-level resources shared by every preview.
- **Target announcement**: Commands that act remotely now print the resolved target before doing anything, e.g. `→ preview: drupal-test/mr-123 (auto-detected from branch "feature/foo")` or `→ project: drupal-test (auto-detected from git remote)`.
- **Rebuild confirmation**: `druploy rebuild` now asks for confirmation (it destroys the current VM and redeploys from scratch). Use `-y`/`--yes` to skip in scripts/CI.

### Deprecated

- **`druploy push`**: Still works but is hidden from help and prints a deprecation warning. Use `druploy project push` instead.

## [1.8.1] - 2026-03-04

### Fixed

- **Rebuild with correct source**: v1.8.0 binaries were built from stale cache. This version includes the actual changes.

## [1.8.0] - 2026-03-04

### Changed

- **Separate internal settings from user settings**: `setup project` now only generates `settings.preview.php` as an empty file for user overrides. The deployer handles everything else automatically:
  - `settings.preview.internal.php` — Written by the deployer on every deploy. Contains DB connection, file paths, trusted host patterns, and hash salt.
  - The include snippet in `settings.php` — Injected by the deployer if missing, or upgraded from old format automatically.
- `setup project` no longer modifies `settings.php` — the deployer manages it.
- Existing projects with old-style includes are automatically upgraded on next deploy.

## [1.7.3] - 2026-03-04

### Added

- **Percona support**: `setup project` now lists `percona:8.0` and `percona:8.4` as database options in the generated `preview.yml`

## [1.7.2] - 2026-03-02

### Improved

- **`setup project` graceful fallback**: When `settings.php` is not writable (e.g. owned by root), the CLI now shows the snippet to add manually instead of failing with an error

## [1.7.1] - 2026-03-01

### Fixed

- **Temp file on disk**: Upload buffer now uses the current directory instead of `/tmp`, which on many Linux distros is a RAM-backed tmpfs. Prevents "no space left on device" errors on large uploads.

## [1.7.0] - 2026-03-01

### Added

- **pigz support**: `push db` and `push files` automatically use `pigz` (parallel gzip) when available, significantly faster on multi-core systems. Falls back to `gzip` if not installed.
- **Source size display**: `push files` now shows the uncompressed source size before packaging (e.g. "Source: docroot/sites/default/files (1.2 GB)")
- **pigz install hint**: When packaging >500 MB without pigz, shows a hint to install it

### Improved

- **Compression level**: Explicit `-6` compression level (good balance between speed and ratio)
- **Buffering progress**: `push db` and `push files` now show a live spinner with bytes processed during packaging, instead of appearing frozen

## [1.6.1] - 2026-03-01

### Fixed

- **`push files` auto-detect docroot**: Instead of hardcoding `web/sites/default/files`, the CLI now uses `ddev drush status` to detect the actual files directory. Projects using `docroot/` or other non-standard webroot paths now work correctly.

## [1.6.0] - 2026-02-28

### Added

- **Auto-detect preview in `drush`**: `preview drush cr` now works without specifying a preview — the project is detected from the git remote and the preview is matched by the current branch
- **Flexible preview names in `drush`**: Accepts any preview name format (e.g. `project/branch-develop`), not just `project/mr-ID`

## [1.5.1] - 2026-02-27

### Fixed

- **`push db` corruption**: When DDEV was not running, `ddev drush sql-dump` startup messages were mixed into the SQL dump, producing a corrupt file. Now ensures DDEV is running before piping the dump.

## [1.5.0] - 2026-02-26

### Added

- **Auto-detect preview in `pull`**: `preview pull db` and `preview pull files` now work without arguments — the project is detected from the git remote and the preview is matched by the current branch
- **Flexible preview names**: `pull` now accepts any preview name format (e.g. `project/branch-develop`), not just `project/mr-ID`

## [1.4.0] - 2026-02-22

### Added

- **Chunked uploads**: Files larger than 50MB are automatically split into chunks, enabling uploads of any size (no limit)
- **Progress bar**: Upload progress is now displayed in real-time with percentage and transfer speed
- **Retry per chunk**: Each chunk retries up to 3 times with exponential backoff on failure

## [1.3.1] - 2026-02-22

### Added

- **`--yes`/`-y` flag**: Skip confirmation prompts on `preview push db` and `preview push files`

## [1.3.0] - 2026-02-22

### Improved

- **Auth errors**: CLI now shows clear instructions when not authenticated or when the token is expired/revoked, guiding users to run `preview login`

### Changed

- **Push files**: Server now extracts uploaded files immediately and shares them across previews via OverlayFS (no tar.gz stored on disk)

## [1.0.5] - 2026-02-17

### Added

- **Self-update**: `preview self-update` command to update the CLI in place
- **Login guard**: `preview login` now warns if already logged in and shows current user info

### Changed

- **Install location**: CLI now installs to `~/.local/bin/` instead of `/usr/local/bin/` (no sudo required)
- **Version check**: update notification is fully non-blocking (uses cached data from previous run)
- **Version format**: switched to semantic versioning (1.x.x)

## [1.0.0] - 2026-02-17

### Added

- **Authentication**: `login`, `logout`, `setup` commands with device flow support
- **Preview management**: `list`, `start`, `stop`, `restart`, `rebuild` commands
- **Drush integration**: `uli` (user login) and arbitrary `drush` command execution
- **Downloads**: `db` (database dump) and `files` (tar.gz archive) commands
- **Push**: `push` command to trigger preview deployments
- **Project setup**: `setup project` command for per-project configuration
- **Version check**: automatic update notification with 24h cache (non-blocking)
- **Cross-platform**: binaries for linux/amd64, linux/arm64, darwin/amd64, darwin/arm64
- **Install script**: `curl -fsSL https://api.preview-mr.com/api/cli/install.sh | sh`
