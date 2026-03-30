package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// DeployJob holds all info the server sends to start a deploy.
type DeployJob struct {
	DeploymentID int    `json:"deployment_id"`
	Phase        string `json:"phase"` // "new" or "update"
	ForceNew     bool   `json:"force_new"`

	OrgSlug     string `json:"org_slug"`
	ProjectSlug string `json:"project_slug"`
	PreviewName string `json:"preview_name"`
	Branch       string `json:"branch"`
	CommitSHA    string `json:"commit_sha"`
	TargetBranch string `json:"target_branch"`

	GitCloneURL string `json:"git_clone_url"`
	ProxyURL    string `json:"composer_proxy_url"`

	DockerRegistry  string `json:"docker_registry"`
	URLHash         string `json:"url_hash"`
	Domain          string `json:"domain"`
	PreviewURL      string `json:"preview_url"`
	TerminalSecret  string `json:"terminal_secret"`

	Storage StorageConfig `json:"storage"`

	EnvVars map[string]string `json:"env_vars"`

	CallbackURL   string `json:"callback_url"`
	CallbackToken string `json:"callback_token"`
}

// StorageConfig holds S3/R2 credentials for downloading base files.
type StorageConfig struct {
	Type       string `json:"type"` // "s3"
	Endpoint   string `json:"endpoint"`
	AccessKey  string `json:"access_key"`
	SecretKey  string `json:"secret_key"`
	Bucket     string `json:"bucket"`
	BaseDBKey  string `json:"base_db_key"`
	BaseFilesKey string `json:"base_files_key"`
}

// GenerateDockerCompose creates a docker-compose.yml dict from the config.
func GenerateDockerCompose(job *DeployJob, cfg *PreviewConfig) map[string]interface{} {
	prefix := job.URLHash
	domain := job.Domain
	url := job.PreviewURL

	// DB image
	dbImage := cfg.Database
	if !strings.Contains(dbImage, ":") {
		dbImage = "mysql:" + dbImage
	}

	// Host UID/GID for www-data remapping — always 33 (www-data convention)
	// so the entrypoint does NOT remap www-data to root's UID.
	hostUID := "33"
	hostGID := "33"

	// PHP environment
	phpEnv := map[string]string{
		"HOST_UID":                    hostUID,
		"HOST_GID":                    hostGID,
		"PREV_IS_PREVIEW":            "true",
		"PREV_PROJECT_NAME":          job.ProjectSlug,
		"PREV_PREVIEW_NAME":          job.PreviewName,
		"PREV_MR_IID":                "", // set below if applicable
		"PREV_BRANCH":                job.Branch,
		"PREV_TARGET_BRANCH":         job.TargetBranch,
		"PREV_COMMIT_SHA":            job.CommitSHA,
		"PREV_URL":                   url,
		"DRUSH_OPTIONS_URI":          url,
		"PREV_DOMAIN":                domain,
		"PREV_DB_HOST":               prefix + "-db",
		"PREV_DB_NAME":               "drupal",
		"PREV_DB_USER":               "drupal",
		"PREV_DB_PASSWORD":           "drupal",
		"PREV_FILE_PUBLIC_PATH":      "sites/default/files",
		"PREV_FILE_PRIVATE_PATH":     "sites/default/files/private",
		"PREV_FILE_TEMP_PATH":        "/tmp",
		"PREV_FILE_TRANSLATIONS_PATH": "sites/default/files/translations",
		"DOCUMENT_ROOT":              fmt.Sprintf("/var/www/html/%s", cfg.Docroot),
	}

	if job.ProxyURL != "" {
		phpEnv["PREV_HTTP_PROXY"] = job.ProxyURL
		phpEnv["PREV_HTTPS_PROXY"] = job.ProxyURL
	}

	if cfg.LitespeedCache {
		phpEnv["PREV_LITESPEED_CACHE"] = "1"
	}

	if isServiceEnabled(cfg.Redis) || isServiceEnabled(cfg.Valkey) {
		phpEnv["PREV_REDIS_HOST"] = prefix + "-redis"
	}

	if isServiceEnabled(cfg.Solr) {
		phpEnv["PREV_SOLR_HOST"] = prefix + "-solr"
		phpEnv["PREV_SOLR_CORE"] = "drupal"
	}

	// Domain aliases
	if len(cfg.DomainAliases) > 0 {
		aliasDomains := make([]string, len(cfg.DomainAliases))
		for i, a := range cfg.DomainAliases {
			aliasDomains[i] = fmt.Sprintf("%s--%s", a, domain)
		}
		phpEnv["PREV_DOMAIN_ALIASES"] = strings.Join(aliasDomains, ",")
	}

	// Merge preview.yml env vars
	for k, v := range cfg.Env {
		phpEnv[k] = v
	}

	// Merge extra env vars from server (project + preview level)
	for k, v := range job.EnvVars {
		phpEnv[k] = v
	}

	// Build compose structure
	compose := map[string]interface{}{
		"name": prefix,
		"services": map[string]interface{}{
			"php": map[string]interface{}{
				"image":          registryImage(job.DockerRegistry, fmt.Sprintf("preview-drupal:php%s", cfg.PHPVersion)),
				"container_name": prefix + "-php",
				"volumes":        []string{"./:/var/www/html"},
				"environment":    phpEnv,
				"ports":          []string{"80:80", "2222:2222"},
				"restart":        "unless-stopped",
			},
			"db": map[string]interface{}{
				"image":          dbImage,
				"container_name": prefix + "-db",
				"command":        "--innodb-flush-log-at-trx-commit=0",
				"environment": map[string]string{
					"MYSQL_ROOT_PASSWORD": "root",
					"MYSQL_DATABASE":      "drupal",
					"MYSQL_USER":          "drupal",
					"MYSQL_PASSWORD":      "drupal",
				},
				"volumes": []string{"db_data:/var/lib/mysql"},
				"restart": "unless-stopped",
			},
		},
		"volumes": map[string]interface{}{
			"db_data": map[string]string{"name": prefix + "_db_data"},
		},
	}

	services := compose["services"].(map[string]interface{})
	volumes := compose["volumes"].(map[string]interface{})

	// Optional: Valkey (takes priority over Redis)
	if isServiceEnabled(cfg.Valkey) {
		ver := serviceVersion(cfg.Valkey, "8")
		services["redis"] = map[string]interface{}{
			"image":          fmt.Sprintf("valkey/valkey:%s-alpine", ver),
			"container_name": prefix + "-redis",
			"restart":        "unless-stopped",
		}
	} else if isServiceEnabled(cfg.Redis) {
		ver := serviceVersion(cfg.Redis, "7")
		services["redis"] = map[string]interface{}{
			"image":          fmt.Sprintf("redis:%s-alpine", ver),
			"container_name": prefix + "-redis",
			"restart":        "unless-stopped",
		}
	}

	// Optional: Solr
	if isServiceEnabled(cfg.Solr) {
		ver := serviceVersion(cfg.Solr, "9")
		solrVolumes := []string{"solr_data:/var/solr"}
		solrService := map[string]interface{}{
			"image":          fmt.Sprintf("solr:%s", ver),
			"container_name": prefix + "-solr",
			"restart":        "unless-stopped",
			"environment": map[string]string{
				"SOLR_JAVA_MEM": "-Xms128m -Xmx256m",
			},
		}

		if cfg.SolrConfigset != "" {
			solrVolumes = append(solrVolumes, fmt.Sprintf("./%s:/opt/solr-conf:ro", cfg.SolrConfigset))
			solrService["entrypoint"] = []string{
				"bash", "-c",
				"mkdir -p /var/solr/data/drupal/conf /var/solr/data/drupal/data && " +
					"cp /opt/solr-conf/* /var/solr/data/drupal/conf/ && " +
					"echo name=drupal > /var/solr/data/drupal/core.properties && " +
					"chown -R solr:solr /var/solr/data/drupal && " +
					"exec solr-foreground",
			}
		} else {
			solrService["command"] = "solr-precreate drupal"
		}

		solrService["volumes"] = solrVolumes
		services["solr"] = solrService
		volumes["solr_data"] = map[string]string{"name": prefix + "_solr_data"}
	}

	// Terminal/deploy is handled by the native preview-agent on the VM (port 8022).
	// No Docker sidecar needed.

	// Expose services with known web UIs
	for svcName, port := range cfg.Expose {
		if svc, ok := services[svcName]; ok {
			svcMap := svc.(map[string]interface{})
			ports := []string{}
			if existing, ok := svcMap["ports"]; ok {
				ports = existing.([]string)
			}
			ports = append(ports, fmt.Sprintf("%d:%d", port, port))
			svcMap["ports"] = ports
		}
	}

	return compose
}

// WriteDockerCompose writes the compose dict as YAML to the given directory.
func WriteDockerCompose(dir string, compose map[string]interface{}) error {
	data, err := yaml.Marshal(compose)
	if err != nil {
		return fmt.Errorf("failed to marshal docker-compose.yml: %w", err)
	}
	return os.WriteFile(filepath.Join(dir, "docker-compose.yml"), data, 0644)
}

func registryImage(registry, image string) string {
	if registry != "" {
		return registry + "/" + image
	}
	return image
}
