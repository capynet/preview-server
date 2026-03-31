package cmd

import (
	"fmt"
	"os"
	"path/filepath"
)

var overrideFlag bool

func runSetupProject() error {
	// Verify we're in a Drupal project
	docroot := detectDocroot()
	if docroot == "" {
		return fmt.Errorf("could not find web/ or docroot/ directory — are you in a Drupal project root?")
	}

	settingsDir := filepath.Join(docroot, "sites", "default")
	if _, err := os.Stat(settingsDir); os.IsNotExist(err) {
		return fmt.Errorf("directory %s not found — are you in a Drupal project root?", settingsDir)
	}

	fmt.Println("Setting up preview environment files...")
	fmt.Println()

	var created, skipped, overwritten []string

	if overrideFlag {
		fmt.Println("  ⚠ Override mode: existing files will be overwritten")
		fmt.Println()
	}

	// 1. Create settings.druploy.php
	previewSettingsPath := filepath.Join(settingsDir, "settings.druploy.php")
	wrote, err := writeFile(previewSettingsPath, settingsPreviewContent())
	if err != nil {
		return fmt.Errorf("failed to create settings.druploy.php: %w", err)
	}
	switch wrote {
	case "created":
		created = append(created, previewSettingsPath)
		fmt.Printf("  ✓ %s — created\n", previewSettingsPath)
	case "overwritten":
		overwritten = append(overwritten, previewSettingsPath)
		fmt.Printf("  ✓ %s — overwritten\n", previewSettingsPath)
	default:
		skipped = append(skipped, previewSettingsPath)
		fmt.Printf("  · %s — already exists\n", previewSettingsPath)
	}

	// 2. Create druploy.yml
	wrote, err = writeFile("druploy.yml", previewYmlContent())
	if err != nil {
		return fmt.Errorf("failed to create druploy.yml: %w", err)
	}
	switch wrote {
	case "created":
		created = append(created, "druploy.yml")
		fmt.Printf("  ✓ druploy.yml — created\n")
	case "overwritten":
		overwritten = append(overwritten, "druploy.yml")
		fmt.Printf("  ✓ druploy.yml — overwritten\n")
	default:
		skipped = append(skipped, "druploy.yml")
		fmt.Printf("  · druploy.yml — already exists\n")
	}

	// 3. Create deploy scripts
	for _, phase := range []string{"new", "update"} {
		scriptDir := filepath.Join("scripts", "druploy", phase)
		scriptPath := filepath.Join(scriptDir, "deploy.sh")
		os.MkdirAll(scriptDir, 0755)
		wrote, err = writeFile(scriptPath, deployScriptContent(phase))
		if err != nil {
			return fmt.Errorf("failed to create %s: %w", scriptPath, err)
		}
		os.Chmod(scriptPath, 0755)
		switch wrote {
		case "created":
			created = append(created, scriptPath)
			fmt.Printf("  ✓ %s — created\n", scriptPath)
		case "overwritten":
			overwritten = append(overwritten, scriptPath)
			fmt.Printf("  ✓ %s — overwritten\n", scriptPath)
		default:
			skipped = append(skipped, scriptPath)
			fmt.Printf("  · %s — already exists\n", scriptPath)
		}
	}

	// 4. Create post-deploy scripts
	for _, phase := range []string{"new", "update"} {
		scriptDir := filepath.Join("scripts", "druploy", phase)
		scriptPath := filepath.Join(scriptDir, "post-deploy.sh")
		os.MkdirAll(scriptDir, 0755)
		wrote, err = writeFile(scriptPath, postDeployScriptContent(phase))
		if err != nil {
			return fmt.Errorf("failed to create %s: %w", scriptPath, err)
		}
		os.Chmod(scriptPath, 0755)
		switch wrote {
		case "created":
			created = append(created, scriptPath)
			fmt.Printf("  ✓ %s — created\n", scriptPath)
		case "overwritten":
			overwritten = append(overwritten, scriptPath)
			fmt.Printf("  ✓ %s — overwritten\n", scriptPath)
		default:
			skipped = append(skipped, scriptPath)
			fmt.Printf("  · %s — already exists\n", scriptPath)
		}
	}

	fmt.Println()
	if len(created) > 0 {
		fmt.Printf("Created %d file(s).\n", len(created))
	}
	if len(overwritten) > 0 {
		fmt.Printf("Overwritten %d file(s).\n", len(overwritten))
	}
	if len(skipped) > 0 {
		fmt.Printf("Skipped %d file(s) that already exist.\n", len(skipped))
	}

	fmt.Println()
	fmt.Println("Next steps:")
	fmt.Println("  1. Edit druploy.yml to match your project's needs")
	fmt.Println("  2. Customize the deploy scripts in scripts/druploy/")
	fmt.Println("  3. Commit everything to your repository")

	return nil
}

// writeFile writes content to path. Returns "created", "overwritten", or "skipped".
func writeFile(path string, content string) (string, error) {
	_, err := os.Stat(path)
	exists := err == nil

	if exists && !overrideFlag {
		return "skipped", nil
	}

	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		return "", err
	}

	if exists {
		return "overwritten", nil
	}
	return "created", nil
}

func detectDocroot() string {
	for _, candidate := range []string{"web", "docroot"} {
		info, err := os.Stat(candidate)
		if err == nil && info.IsDir() {
			return candidate
		}
	}
	return ""
}

func settingsPreviewContent() string {
	return `<?php

/**
 * @file
 * Preview environment overrides.
 *
 * This file is loaded after the internal preview configuration
 * (settings.druploy.internal.php) which sets up the database connection,
 * file paths, and trusted host patterns automatically.
 *
 * Use this file to add or override any Drupal settings specifically
 * for preview environments. For example:
 *
 *   $config['system.performance']['css']['preprocess'] = FALSE;
 *   $config['system.performance']['js']['preprocess'] = FALSE;
 *   $settings['my_custom_setting'] = 'preview-value';
 */

`
}

func previewYmlContent() string {
	return `# Druploy configuration
# This file defines how preview environments are created for this project.
# See: https://druploy.dev/docs/configuration

# PHP version for the preview container.
# Supported: 8.1, 8.2, 8.3, 8.4
php_version: "8.3"

# Database engine and version.
# Examples:
#   mysql:5.7   (≈ mariadb:10.3)
#   mysql:8.0   (≈ mariadb:10.6)
#   mysql:8.4   (≈ mariadb:11.4)
#   mariadb:10.6
#   mariadb:11.4
database: mysql:8.0

# Document root relative to the project root.
# Auto-detected if not set (looks for "web/" or "docroot/").
docroot: web

# Optional services. Set a version to enable, false to disable.
# When enabled, the corresponding PREV_*_HOST env vars are set automatically.
# Note: redis and valkey are mutually exclusive (valkey takes priority).
redis: false          # e.g. "7", "6"
valkey: false         # e.g. "8", "7" (Redis-compatible fork)
solr: false           # e.g. "9", "8"

# Solr configset — path relative to the project root containing schema.xml,
# solrconfig.xml, and language-specific files. If not set, Solr uses its
# default config. Generate one with: drush search-api-solr:get-server-config
# solr_configset: "etc/solr"

# Custom environment variables injected into the PHP container.
# These are available in settings.druploy.php via getenv().
# env:
#   APP_ENV: preview
#   MY_CUSTOM_VAR: some-value

# Domain aliases — additional subdomains that route to this preview.
# Each prefix becomes {prefix}--{preview-domain}.druploy.dev
# Useful for multi-site setups where the app expects different hostnames.
# The list is available as PREV_DOMAIN_ALIASES env var.
# domain_aliases:
#   - admin
#   - fr
#   - de

# LiteSpeed Cache — enable the built-in OLS cache module.
# Works like Varnish but integrated into the webserver. Requires the
# Drupal "lite_speed_cache" module to be installed in your project.
# litespeed_cache: true

# Drush URI — custom URI passed to drush via --uri flag.
# Used for drush uli (admin login link) and other drush commands.
# Can be a domain alias name (e.g. "admin") which expands to
# https://{alias}--{preview-domain}.druploy.dev, or a full URL.
# If not set or false, the default preview URL is used.
# drush_uri: admin

# Deploy scripts — executed inside the PHP container after setup.
# Paths are relative to the project root.
# If not defined or set to false, no deploy script runs for that phase.
#
# "new" runs when a preview is created for the first time (after DB + files import).
# "update" runs when new commits are pushed to the MR.
#
deploy:
  new: scripts/druploy/new/deploy.sh
  update: scripts/druploy/update/deploy.sh

# Post-deploy scripts — executed after a successful deploy.
# These run after the preview is fully active and reachable.
# Useful for cache warming, notifications, or other non-critical tasks.
# A failure here does NOT mark the deploy as failed.
#
# post_deploy:
#   new: scripts/druploy/new/post-deploy.sh
#   update: scripts/druploy/update/post-deploy.sh
`
}

func deployScriptContent(phase string) string {
	if phase == "new" {
		return `#!/usr/bin/env bash
set -euo pipefail

# Deploy script for NEW preview environments.
# Runs inside the PHP container after database and files have been imported.
#
# Available environment variables (PREV_ prefix):
#   PREV_IS_PREVIEW, PREV_PROJECT_NAME, PREV_MR_IID, PREV_BRANCH,
#   PREV_COMMIT_SHA, PREV_URL, PREV_DOMAIN, PREV_DB_HOST, etc.

DRUSH="vendor/bin/drush"

echo "Running new preview deploy script..."

$DRUSH deploy

echo "Deploy complete."
`
	}

	return `#!/usr/bin/env bash
set -euo pipefail

# Deploy script for UPDATED preview environments.
# Runs inside the PHP container after code has been synced (new commits pushed).
#
# Available environment variables (PREV_ prefix):
#   PREV_IS_PREVIEW, PREV_PROJECT_NAME, PREV_MR_IID, PREV_BRANCH,
#   PREV_COMMIT_SHA, PREV_URL, PREV_DOMAIN, PREV_DB_HOST, etc.

DRUSH="vendor/bin/drush"

echo "Running update preview deploy script..."

$DRUSH deploy

echo "Update complete."
`
}

func postDeployScriptContent(phase string) string {
	if phase == "new" {
		return `#!/usr/bin/env bash
set -euo pipefail

# Post-deploy script for NEW preview environments.
# Runs after the preview is fully deployed and reachable.
# Use this for non-critical tasks like cache warming or notifications.
#
# Available environment variables (PREV_ prefix):
#   PREV_IS_PREVIEW, PREV_PROJECT_NAME, PREV_MR_IID, PREV_BRANCH,
#   PREV_COMMIT_SHA, PREV_URL, PREV_DOMAIN, PREV_DB_HOST, etc.

echo "Running new preview post-deploy script..."

# Example: warm caches
# vendor/bin/drush cr
# curl -s "$PREV_URL" > /dev/null

echo "Post-deploy complete."
`
	}

	return `#!/usr/bin/env bash
set -euo pipefail

# Post-deploy script for UPDATED preview environments.
# Runs after the preview has been updated with new code and is reachable.
# Use this for non-critical tasks like cache warming or notifications.
#
# Available environment variables (PREV_ prefix):
#   PREV_IS_PREVIEW, PREV_PROJECT_NAME, PREV_MR_IID, PREV_BRANCH,
#   PREV_COMMIT_SHA, PREV_URL, PREV_DOMAIN, PREV_DB_HOST, etc.

echo "Running update preview post-deploy script..."

# Example: warm caches
# vendor/bin/drush cr
# curl -s "$PREV_URL" > /dev/null

echo "Post-deploy complete."
`
}

