package cmd

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

var overrideFlag bool

var setupProjectCmd = &cobra.Command{
	Use:   "project",
	Short: "Scaffold a Drupal project for preview environments",
	Long: `Creates the necessary files for preview compatibility:

  1. Creates web/sites/default/settings.preview.php for custom overrides
  2. Creates preview.yml template in the project root
  3. Creates deploy script templates in scripts/preview/

The preview include snippet in settings.php and the internal settings
file (settings.preview.internal.php) are managed automatically by
the deployer — you don't need to touch them.

Run this command from the root of your Drupal project.
Use --override to overwrite existing files with the latest templates.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return runSetupProject()
	},
}

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

	// 1. Create settings.preview.php
	previewSettingsPath := filepath.Join(settingsDir, "settings.preview.php")
	wrote, err := writeFile(previewSettingsPath, settingsPreviewContent())
	if err != nil {
		return fmt.Errorf("failed to create settings.preview.php: %w", err)
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

	// 2. Create preview.yml
	wrote, err = writeFile("preview.yml", previewYmlContent())
	if err != nil {
		return fmt.Errorf("failed to create preview.yml: %w", err)
	}
	switch wrote {
	case "created":
		created = append(created, "preview.yml")
		fmt.Printf("  ✓ preview.yml — created\n")
	case "overwritten":
		overwritten = append(overwritten, "preview.yml")
		fmt.Printf("  ✓ preview.yml — overwritten\n")
	default:
		skipped = append(skipped, "preview.yml")
		fmt.Printf("  · preview.yml — already exists\n")
	}

	// 3. Create deploy scripts
	for _, phase := range []string{"new", "update"} {
		scriptDir := filepath.Join("scripts", "preview", phase)
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
		scriptDir := filepath.Join("scripts", "preview", phase)
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
	fmt.Println("  1. Edit preview.yml to match your project's needs")
	fmt.Println("  2. Customize the deploy scripts in scripts/preview/")
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
 * (settings.preview.internal.php) which sets up the database connection,
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
	return `# Preview Manager configuration
# This file defines how preview environments are created for this project.
# See: https://app.preview-mr.com/docs/configuration

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
# These are available in settings.preview.php via getenv().
# env:
#   APP_ENV: preview
#   MY_CUSTOM_VAR: some-value

# Expose service ports via authenticated subdomain routing.
# Each entry maps a service name (as defined above) to its port.
# The service becomes accessible at {service}--{preview-domain}.mr.preview-mr.com
# Protected by the same authentication as the preview itself.
# expose:
#   solr: 8983

# Domain aliases — additional subdomains that route to this preview.
# Each prefix becomes {prefix}--{preview-domain}.mr.preview-mr.com
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

# Deploy scripts — executed inside the PHP container after setup.
# Paths are relative to the project root.
# If not defined or set to false, no deploy script runs for that phase.
#
# "new" runs when a preview is created for the first time (after DB + files import).
# "update" runs when new commits are pushed to the MR.
#
deploy:
  new: scripts/preview/new/deploy.sh
  update: scripts/preview/update/deploy.sh

# Post-deploy scripts — executed after a successful deploy.
# These run after the preview is fully active and reachable.
# Useful for cache warming, notifications, or other non-critical tasks.
# A failure here does NOT mark the deploy as failed.
#
# post_deploy:
#   new: scripts/preview/new/post-deploy.sh
#   update: scripts/preview/update/post-deploy.sh
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

func init() {
	setupProjectCmd.Flags().BoolVar(&overrideFlag, "override", false, "Overwrite existing files with the latest templates")
	setupCmd.AddCommand(setupProjectCmd)
}
