package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

const settingsPreviewInternalPHP = `<?php

/**
 * @file
 * Internal preview environment settings — managed by Druploy Agent.
 *
 * DO NOT EDIT. This file is overwritten on every deploy.
 * Use settings.druploy.php for custom overrides.
 */

// Database connection.
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

// Trusted host patterns — allow the preview domain and any aliases.
$settings['trusted_host_patterns'][] = '^' . preg_quote(getenv('PREV_DOMAIN')) . '$';
if (getenv('PREV_DOMAIN_ALIASES')) {
  foreach (explode(',', getenv('PREV_DOMAIN_ALIASES')) as $_alias) {
    $settings['trusted_host_patterns'][] = '^' . preg_quote(trim($_alias)) . '$';
  }
}

// Reverse proxy — Drupal is behind Caddy + Python middleware.
$settings['reverse_proxy'] = TRUE;
$settings['reverse_proxy_addresses'] = ['172.16.0.0/12', '10.0.0.0/8', '127.0.0.1'];

// File system paths.
$settings['file_public_path'] = getenv('PREV_FILE_PUBLIC_PATH');
$settings['file_private_path'] = getenv('PREV_FILE_PRIVATE_PATH');
$settings['file_temp_path'] = getenv('PREV_FILE_TEMP_PATH');
$config['locale.settings']['translation']['path'] = getenv('PREV_FILE_TRANSLATIONS_PATH');

// Config sync directory — auto-detect common locations.
foreach (['../config/sync', '../config', 'sites/default/config/sync'] as $_candidate) {
  if (is_dir(DRUPAL_ROOT . '/' . $_candidate)) {
    $settings['config_sync_directory'] = $_candidate;
    break;
  }
}

// Hash salt — override if not already set upstream.
if (empty($settings['hash_salt'])) {
  $settings['hash_salt'] = getenv('PREV_PROJECT_NAME') . '-preview';
}

// Redis / Valkey cache backend — two-phase activation.
$_redis_host = getenv('PREV_REDIS_HOST');
if ($_redis_host) {
  $settings['redis.connection']['interface'] = 'PhpRedis';
  $settings['redis.connection']['host'] = $_redis_host;
  $settings['redis.connection']['port'] = 6379;
  if (file_exists('/tmp/.preview_redis_ready')) {
    if (isset($databases['default']['default'])) {
      try {
        $_db = $databases['default']['default'];
        $_pdo = new \PDO(
          "mysql:host={$_db['host']};port={$_db['port']};dbname={$_db['database']}",
          $_db['username'],
          $_db['password']
        );
        $_result = $_pdo->query("SELECT 1 FROM key_value WHERE collection = 'system.schema' AND name = 'redis' LIMIT 1");
        if ($_result && $_result->fetch()) {
          $settings['cache']['default'] = 'cache.backend.redis';
          $_redis_services = DRUPAL_ROOT . '/modules/contrib/redis/example.services.yml';
          if (file_exists($_redis_services)) {
            $settings['container_yamls'][] = $_redis_services;
          }
        }
      } catch (\Exception $e) {
        // DB not ready yet — skip Redis cache backend.
      }
    }
  }
}
`

const settingsIncludeSnippet = `
// Preview environment settings.
if (getenv('PREV_IS_PREVIEW')) {
  include __DIR__ . '/settings.druploy.internal.php';
  if (file_exists(__DIR__ . '/settings.druploy.php')) {
    include __DIR__ . '/settings.druploy.php';
  }
}
`

// WriteSettings generates settings.druploy.internal.php, ensures settings.php
// includes it, and writes drush site aliases.
func WriteSettings(repoPath string, job *DeployJob, cfg *PreviewConfig) error {
	docroot := cfg.Docroot
	settingsDir := filepath.Join(repoPath, docroot, "sites", "default")
	if err := os.MkdirAll(settingsDir, 0755); err != nil {
		return fmt.Errorf("failed to create settings dir: %w", err)
	}

	// 1. Write settings.druploy.internal.php
	internalPath := filepath.Join(settingsDir, "settings.druploy.internal.php")
	if err := os.WriteFile(internalPath, []byte(settingsPreviewInternalPHP), 0644); err != nil {
		return fmt.Errorf("failed to write settings.druploy.internal.php: %w", err)
	}

	// 2. Ensure settings.php includes the preview snippet
	settingsPhpPath := filepath.Join(settingsDir, "settings.php")
	if data, err := os.ReadFile(settingsPhpPath); err == nil {
		content := string(data)
		if !strings.Contains(content, "settings.druploy.internal.php") {
			content = strings.TrimRight(content, "\n") + "\n" + settingsIncludeSnippet
			if err := os.WriteFile(settingsPhpPath, []byte(content), 0644); err != nil {
				return fmt.Errorf("failed to update settings.php: %w", err)
			}
		}
	} else {
		// settings.php doesn't exist — create it
		content := "<?php\n" + settingsIncludeSnippet
		if err := os.WriteFile(settingsPhpPath, []byte(content), 0644); err != nil {
			return fmt.Errorf("failed to create settings.php: %w", err)
		}
	}

	// 3. Write drush site aliases for domain aliases
	if len(cfg.DomainAliases) > 0 {
		drushSitesDir := filepath.Join(repoPath, "drush", "sites")
		if err := os.MkdirAll(drushSitesDir, 0755); err != nil {
			return fmt.Errorf("failed to create drush/sites dir: %w", err)
		}

		aliases := make(map[string]map[string]string)
		// Default alias pointing to the base preview domain
		aliases["default"] = map[string]string{
			"uri": fmt.Sprintf("https://%s", job.Domain),
		}
		for _, prefix := range cfg.DomainAliases {
			aliasDomain := fmt.Sprintf("%s--%s", prefix, job.Domain)
			aliases[prefix] = map[string]string{
				"uri": fmt.Sprintf("https://%s", aliasDomain),
			}
		}

		aliasData, err := yaml.Marshal(aliases)
		if err != nil {
			return fmt.Errorf("failed to marshal drush aliases: %w", err)
		}

		aliasContent := "# Managed by Druploy Agent — overwritten on every deploy.\n" + string(aliasData)
		aliasPath := filepath.Join(drushSitesDir, "preview.site.yml")
		if err := os.WriteFile(aliasPath, []byte(aliasContent), 0644); err != nil {
			return fmt.Errorf("failed to write drush aliases: %w", err)
		}
	}

	// 4. Write .env for docker-compose (TERMINAL_SECRET)
	envContent := fmt.Sprintf("TERMINAL_SECRET=%s\n", job.TerminalSecret)
	envPath := filepath.Join(repoPath, ".env")
	if err := os.WriteFile(envPath, []byte(envContent), 0644); err != nil {
		return fmt.Errorf("failed to write .env: %w", err)
	}

	return nil
}
