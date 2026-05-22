# Environment variables

All preview containers receive a set of `PREV_*` environment variables. They are available in PHP via `getenv()` and in deploy scripts as shell variables.

---

## General

| Variable             | Description                                                                                       | Example                          |
|----------------------|---------------------------------------------------------------------------------------------------|----------------------------------|
| `PREV_IS_PREVIEW`    | Flag indicating the app is running inside a preview environment.                                  | `"true"`                         |
| `PREV_PROJECT_NAME`  | Project slug as configured in Preview Manager.                                                    | `"my-drupal-site"`               |
| `PREV_PREVIEW_NAME`  | Preview identifier. MR previews use `mr-{iid}`, branch previews use `branch-{name}`.              | `"mr-42"` or `"branch-develop"`  |
| `PREV_MR_IID`        | GitLab merge request IID. Empty for branch previews.                                              | `"42"`                           |
| `PREV_BRANCH`        | Git branch name that this preview was built from.                                                 | `"feature/new-module"`           |
| `PREV_COMMIT_SHA`    | Full git commit SHA of the deployed code.                                                         | `"a1b2c3d4e5f6..."`              |

---

## URLs & domains

| Variable               | Description                                                                                                                                     | Example                                                          |
|------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------|
| `PREV_URL`             | Full preview URL including protocol. Use for absolute links and drush URI.                                                                      | `"https://mr-42-my-site.druploy.dev"`                            |
| `PREV_DOMAIN`          | Preview domain without protocol. Used in `trusted_host_patterns`.                                                                               | `"mr-42-my-site.druploy.dev"`                                    |
| `PREV_DOMAIN_ALIASES`  | Comma-separated list of alias domains. Set when `domain_aliases` configured in `druploy.yml`. Each alias becomes `{alias}--{domain}`.           | `"admin--mr-42-my-site.druploy.dev,fr--mr-42-my-site.druploy.dev"` |

---

## Database

| Variable           | Description                                                              | Example                |
|--------------------|--------------------------------------------------------------------------|------------------------|
| `PREV_DB_HOST`     | Database container hostname. Points to the MySQL/MariaDB container.      | `"mr-42-my-site-db"`   |
| `PREV_DB_NAME`     | Database name.                                                           | `"drupal"`             |
| `PREV_DB_USER`     | Database username.                                                       | `"drupal"`             |
| `PREV_DB_PASSWORD` | Database password.                                                       | `"drupal"`             |

!!! note
    All previews use the same credentials (`drupal`/`drupal`). The database runs in an isolated container per preview, so there is no conflict.

---

## File paths

| Variable                       | Description                                                       | Example                              |
|--------------------------------|-------------------------------------------------------------------|--------------------------------------|
| `PREV_FILE_PUBLIC_PATH`        | Drupal public files directory, relative to docroot.               | `"sites/default/files"`              |
| `PREV_FILE_PRIVATE_PATH`       | Drupal private files directory, relative to docroot.              | `"sites/default/files/private"`      |
| `PREV_FILE_TEMP_PATH`          | Temporary files directory (absolute path).                        | `"/tmp"`                             |
| `PREV_FILE_TRANSLATIONS_PATH`  | Interface translation files directory, relative to docroot.       | `"sites/default/files/translations"` |

---

## Optional services

These variables are only set when the corresponding service is enabled in `druploy.yml`.

| Variable                 | Description                                                                                                                  | Example                  |
|--------------------------|------------------------------------------------------------------------------------------------------------------------------|--------------------------|
| `PREV_REDIS_HOST`        | Redis/Valkey container hostname. The `settings.druploy.php` auto-configures the Redis cache backend when set.                | `"mr-42-my-site-redis"`  |
| `PREV_SOLR_HOST`         | Apache Solr container hostname.                                                                                              | `"mr-42-my-site-solr"`   |
| `PREV_SOLR_CORE`         | Solr core name.                                                                                                              | `"drupal"`               |
| `PREV_LITESPEED_CACHE`   | Flag indicating LiteSpeed Cache is enabled in the web server.                                                                | `"1"`                    |

---

## Proxy

| Variable            | Description                                                                  | Example                |
|---------------------|------------------------------------------------------------------------------|------------------------|
| `PREV_HTTP_PROXY`   | HTTP proxy URL. Set when the preview VM uses a proxy for outbound traffic.   | `"http://proxy:3128"`  |
| `PREV_HTTPS_PROXY`  | HTTPS proxy URL. Same value as `PREV_HTTP_PROXY`.                            | `"http://proxy:3128"`  |

These are also passed to `git clone` and other build-time operations.

---

## Custom variables

You can define custom environment variables in `druploy.yml` under the `env:` key. These are injected as-is into the PHP container, **without** the `PREV_` prefix.

```yaml
# druploy.yml
env:
  APP_ENV: preview
  STAGE_FILE_PROXY_ORIGIN: "https://example.com"
  MY_API_KEY: "abc123"
```

Access them in PHP or scripts the same way:

```php
$env = getenv('APP_ENV');                        // "preview"
$origin = getenv('STAGE_FILE_PROXY_ORIGIN');
```

---

## Usage examples

### In `settings.druploy.php` (PHP)

```php
// Database connection.
$databases['default']['default'] = [
  'database' => getenv('PREV_DB_NAME'),
  'username' => getenv('PREV_DB_USER'),
  'password' => getenv('PREV_DB_PASSWORD'),
  'host'     => getenv('PREV_DB_HOST'),
  'port'     => '3306',
  'driver'   => 'mysql',
];

// Trusted host.
$settings['trusted_host_patterns'][] =
  '^' . preg_quote(getenv('PREV_DOMAIN')) . '$';

// Conditional: detect preview environment.
if (getenv('PREV_IS_PREVIEW')) {
  $config['system.performance']['css']['preprocess'] = FALSE;
  $config['system.performance']['js']['preprocess'] = FALSE;
}
```

### In deploy scripts (Bash)

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "Deploying $PREV_PROJECT_NAME preview $PREV_PREVIEW_NAME"
echo "Branch: $PREV_BRANCH (commit: $PREV_COMMIT_SHA)"
echo "URL: $PREV_URL"

vendor/bin/drush deploy

# Warm the cache.
curl -s "$PREV_URL" > /dev/null
```

### Checking for optional services

```php
// Redis — only configure if the service is running.
if (getenv('PREV_REDIS_HOST')) {
  $settings['redis.connection']['host'] = getenv('PREV_REDIS_HOST');
  // ... redis config
}

// Solr.
if (getenv('PREV_SOLR_HOST')) {
  $config['search_api.server.solr']['backend_config']['connector_config']['host']
    = getenv('PREV_SOLR_HOST');
}
```
