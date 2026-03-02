package cmd

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

var overrideFlag bool

var setupProjectCmd = &cobra.Command{
	Use:   "project",
	Short: "Scaffold a Drupal project for preview environments",
	Long: `Creates the necessary files for preview compatibility:

  1. Adds a preview include snippet to web/sites/default/settings.php
  2. Creates web/sites/default/settings.preview.php with DB config
  3. Creates preview.yml template in the project root
  4. Creates deploy script templates in scripts/preview/

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

	// 1. Add include snippet to settings.php
	settingsPath := filepath.Join(settingsDir, "settings.php")
	result, err := appendPreviewInclude(settingsPath)
	if err != nil {
		fmt.Printf("  ⚠ %s — could not write (permission denied)\n", settingsPath)
		fmt.Println()
		fmt.Println("  Add the following snippet manually to the end of your settings.php:")
		fmt.Println()
		for _, line := range strings.Split(strings.TrimSpace(previewIncludeSnippet), "\n") {
			fmt.Printf("    %s\n", line)
		}
		fmt.Println()
		skipped = append(skipped, settingsPath)
	} else if result == "created" || result == "appended" {
		created = append(created, settingsPath)
		fmt.Printf("  ✓ %s — preview include added\n", settingsPath)
	} else {
		skipped = append(skipped, settingsPath)
		fmt.Printf("  · %s — already configured\n", settingsPath)
	}

	// 2. Create settings.preview.php
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

	// 3. Create preview.yml
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

	// 4. Create deploy scripts
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
	fmt.Println("  1. Review the generated files, especially settings.preview.php")
	fmt.Println("  2. Edit preview.yml to match your project's needs")
	fmt.Println("  3. Customize the deploy scripts in scripts/preview/")
	fmt.Println("  4. Commit everything to your repository")

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

const previewIncludeSnippet = `
// Preview environment settings.
if (getenv('PREV_IS_PREVIEW')) {
  include __DIR__ . '/settings.preview.php';
}
`

func appendPreviewInclude(settingsPath string) (string, error) {
	data, err := os.ReadFile(settingsPath)
	if os.IsNotExist(err) {
		// No settings.php — create one with just the include
		content := "<?php\n\n" + strings.TrimLeft(previewIncludeSnippet, "\n")
		if err := os.WriteFile(settingsPath, []byte(content), 0644); err != nil {
			return "", err
		}
		return "created", nil
	}
	if err != nil {
		return "", err
	}

	// Check if already configured
	if strings.Contains(string(data), "PREV_IS_PREVIEW") {
		return "exists", nil
	}

	// Append the include snippet
	f, err := os.OpenFile(settingsPath, os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		return "", err
	}
	defer f.Close()

	if _, err := f.WriteString(previewIncludeSnippet); err != nil {
		return "", err
	}
	return "appended", nil
}

func settingsPreviewContent() string {
	return `<?php

/**
 * @file
 * Preview environment settings.
 *
 * This file is automatically included when running in a preview environment
 * (when the PREV_IS_PREVIEW environment variable is set).
 *
 * All preview environment variables use the PREV_ prefix:
 *   PREV_IS_PREVIEW  - Always "true" in preview environments
 *   PREV_DB_HOST     - Database hostname
 *   PREV_DB_NAME     - Database name
 *   PREV_DB_USER     - Database username
 *   PREV_DB_PASSWORD - Database password
 *   PREV_PROJECT_NAME - Project slug
 *   PREV_MR_IID      - Merge request IID
 *   PREV_BRANCH      - Git branch name
 *   PREV_COMMIT_SHA  - Git commit SHA
 *   PREV_URL         - Full preview URL (https://...)
 *   PREV_DOMAIN      - Preview domain (without protocol)
 *   PREV_FILE_PUBLIC_PATH - Public files path
 *   PREV_FILE_PRIVATE_PATH - Private files path
 *   PREV_FILE_TEMP_PATH - Temp files path
 *   PREV_FILE_TRANSLATIONS_PATH - Translations path
 *   PREV_REDIS_HOST  - Redis hostname (only if Redis is enabled)
 *   PREV_SOLR_HOST   - Solr hostname (only if Solr is enabled)
 *   PREV_SOLR_CORE   - Solr core name (only if Solr is enabled)
 */

// Database connection.
// MySQL 8.0+ enables SSL by default with a self-signed certificate.
// Disable SSL verification to avoid "self-signed certificate in certificate
// chain" errors when Drush or Drupal connects to the database container.
$databases['default']['default'] = [
  'database' => getenv('PREV_DB_NAME'),
  'username' => getenv('PREV_DB_USER'),
  'password' => getenv('PREV_DB_PASSWORD'),
  'host' => getenv('PREV_DB_HOST'),
  'port' => '3306',
  'driver' => 'mysql',
  'prefix' => '',
  'collation' => 'utf8mb4_general_ci',
  'pdo' => [
    \PDO::MYSQL_ATTR_SSL_VERIFY_SERVER_CERT => FALSE,
  ],
];

// Trusted host patterns — allow the preview domain.
$settings['trusted_host_patterns'][] = '^' . preg_quote(getenv('PREV_DOMAIN')) . '$';

// File system paths.
$settings['file_public_path'] = getenv('PREV_FILE_PUBLIC_PATH');
$settings['file_private_path'] = getenv('PREV_FILE_PRIVATE_PATH');
$settings['file_temp_path'] = getenv('PREV_FILE_TEMP_PATH');
$config['locale.settings']['translation']['path'] = getenv('PREV_FILE_TRANSLATIONS_PATH');

// Hash salt — override if not already set upstream.
if (empty($settings['hash_salt'])) {
  $settings['hash_salt'] = getenv('PREV_PROJECT_NAME') . '-preview';
}
`
}

func previewYmlContent() string {
	return `# Preview Manager configuration
# This file defines how preview environments are created for this project.
# See: https://app.preview-mr.com/docs/configuration

# PHP version for the preview container.
# Supported: 8.1, 8.2, 8.3
php_version: "8.3"

# MySQL version. Use "8.0", "8.4", etc.
# For MariaDB, use the top-level "mariadb" key instead.
mysql_version: "8.0"

# Uncomment to use MariaDB instead of MySQL:
# mariadb: "10.6"

# Document root relative to the project root.
# Auto-detected if not set (looks for "web/" or "docroot/").
docroot: web

# Optional services. Disabled by default.
# When enabled, the corresponding PREV_*_HOST env vars are set automatically.
services:
  redis: false
  solr: false

# Custom environment variables injected into the PHP container.
# These are available in settings.preview.php via getenv().
# env:
#   APP_ENV: preview
#   MY_CUSTOM_VAR: some-value

# Deploy scripts — executed inside the PHP container after setup.
# Paths are relative to the project root.
# If not defined or set to false, no deploy script runs for that phase.
#
# "new" runs when a preview is created for the first time (after DB + files import).
# "update" runs when new commits are pushed to the MR.
#
# You can override per-MR by creating: scripts/preview/{phase}/mr-{id}-deploy.sh
deploy:
  new: scripts/preview/new/deploy.sh
  update: scripts/preview/update/deploy.sh
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

func init() {
	setupProjectCmd.Flags().BoolVar(&overrideFlag, "override", false, "Overwrite existing files with the latest templates")
	setupCmd.AddCommand(setupProjectCmd)
}
