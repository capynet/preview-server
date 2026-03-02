# Changelog

All notable changes to the Preview CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
