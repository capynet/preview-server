# Changelog

All notable changes to the Preview CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
