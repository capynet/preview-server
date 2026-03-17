package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// PreviewConfig holds the parsed preview.yml configuration.
type PreviewConfig struct {
	PHPVersion     string            `yaml:"php_version"`
	Database       string            `yaml:"database"`
	Docroot        string            `yaml:"docroot"`
	Redis          interface{}       `yaml:"-"` // parsed manually: false | true | "7"
	Solr           interface{}       `yaml:"-"` // parsed manually: false | true | "9"
	Valkey         interface{}       `yaml:"-"` // parsed manually: false | true | "8"
	Env            map[string]string `yaml:"env"`
	Deploy         PhaseScripts      `yaml:"-"`
	PostDeploy     PhaseScripts      `yaml:"-"`
	DomainAliases  []string          `yaml:"domain_aliases"`
	SolrConfigset  string            `yaml:"solr_configset"`
	LitespeedCache bool              `yaml:"litespeed_cache"`
	Expose         map[string]int    `yaml:"-"` // auto-computed
}

// PhaseScripts holds deploy/post-deploy script paths per phase.
type PhaseScripts struct {
	New    string
	Update string
}

// ServiceConfig tracks which optional services are enabled and their versions.
type ServiceConfig struct {
	Redis  interface{} // false | true | "7"
	Solr   interface{} // false | true | "9"
	Valkey interface{} // false | true | "8"
}

// Defaults
var defaultConfig = PreviewConfig{
	PHPVersion:     "8.3",
	Database:       "mysql:8.0",
	Docroot:        "web",
	Env:            map[string]string{},
	DomainAliases:  []string{},
	LitespeedCache: false,
	Expose:         map[string]int{},
}

// ParsePreviewYML reads and parses preview.yml from the given directory.
func ParsePreviewYML(repoPath string) (*PreviewConfig, error) {
	ymlPath := filepath.Join(repoPath, "preview.yml")
	data, err := os.ReadFile(ymlPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, fmt.Errorf("no preview.yml found in the repository — a preview.yml file is required to create previews")
		}
		return nil, fmt.Errorf("failed to read preview.yml: %w", err)
	}

	var raw map[string]interface{}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("failed to parse preview.yml: %w", err)
	}

	cfg := defaultConfig
	cfg.Env = make(map[string]string)
	cfg.DomainAliases = []string{}
	cfg.Expose = make(map[string]int)
	cfg.Redis = false
	cfg.Solr = false
	cfg.Valkey = false

	// PHP version
	if v, ok := raw["php_version"]; ok {
		cfg.PHPVersion = fmt.Sprintf("%v", v)
	}

	// Database
	if v, ok := raw["database"]; ok {
		dbVal := fmt.Sprintf("%v", v)
		engine := dbVal
		if i := strings.Index(dbVal, ":"); i >= 0 {
			engine = dbVal[:i]
		}
		if engine != "mysql" && engine != "mariadb" {
			return nil, fmt.Errorf("unsupported database engine %q — supported: mysql, mariadb", engine)
		}
		cfg.Database = dbVal
	}

	// Docroot
	if v, ok := raw["docroot"]; ok {
		cfg.Docroot = fmt.Sprintf("%v", v)
	}

	// Services: redis, solr, valkey
	for _, svc := range []string{"redis", "solr", "valkey"} {
		if v, ok := raw[svc]; ok {
			switch val := v.(type) {
			case bool:
				if svc == "redis" {
					cfg.Redis = val
				} else if svc == "solr" {
					cfg.Solr = val
				} else {
					cfg.Valkey = val
				}
			case string:
				if svc == "redis" {
					cfg.Redis = val
				} else if svc == "solr" {
					cfg.Solr = val
				} else {
					cfg.Valkey = val
				}
			case int:
				s := fmt.Sprintf("%d", val)
				if svc == "redis" {
					cfg.Redis = s
				} else if svc == "solr" {
					cfg.Solr = s
				} else {
					cfg.Valkey = s
				}
			case nil:
				// disabled
			}
		}
	}

	// Valkey and redis are mutually exclusive — valkey wins
	if isServiceEnabled(cfg.Valkey) {
		cfg.Redis = false
	}

	// Env vars
	if v, ok := raw["env"]; ok {
		if envMap, ok := v.(map[string]interface{}); ok {
			for k, val := range envMap {
				cfg.Env[k] = fmt.Sprintf("%v", val)
			}
		}
	}

	// Deploy scripts
	cfg.Deploy = parsePhaseScripts(raw, "deploy")

	// Post-deploy scripts (accept both "post_deploy" and "post-deploy")
	cfg.PostDeploy = parsePhaseScripts(raw, "post_deploy")
	if cfg.PostDeploy.New == "" && cfg.PostDeploy.Update == "" {
		cfg.PostDeploy = parsePhaseScripts(raw, "post-deploy")
	}

	// Domain aliases
	if v, ok := raw["domain_aliases"]; ok {
		if aliases, ok := v.([]interface{}); ok {
			for _, a := range aliases {
				s := strings.TrimSpace(fmt.Sprintf("%v", a))
				if s != "" {
					cfg.DomainAliases = append(cfg.DomainAliases, s)
				}
			}
		}
	}

	// Solr configset
	if v, ok := raw["solr_configset"]; ok && v != nil {
		cfg.SolrConfigset = fmt.Sprintf("%v", v)
	}

	// LiteSpeed cache
	if v, ok := raw["litespeed_cache"]; ok {
		if b, ok := v.(bool); ok {
			cfg.LitespeedCache = b
		}
	}

	// Auto-expose services with known web UIs
	autoExpose := map[string]int{"solr": 8983}
	for svc, port := range autoExpose {
		if svc == "solr" && isServiceEnabled(cfg.Solr) {
			cfg.Expose[svc] = port
		}
	}

	// Auto-detect docroot if not set in preview.yml
	if _, ok := raw["docroot"]; !ok {
		cfg.Docroot = detectDocroot(repoPath)
	}

	return &cfg, nil
}

func parsePhaseScripts(raw map[string]interface{}, key string) PhaseScripts {
	ps := PhaseScripts{}
	v, ok := raw[key]
	if !ok {
		return ps
	}

	// deploy: false — disable all
	if v == false || v == nil {
		return ps
	}

	if m, ok := v.(map[string]interface{}); ok {
		if val, ok := m["new"]; ok && val != nil && val != false {
			ps.New = fmt.Sprintf("%v", val)
		}
		if val, ok := m["update"]; ok && val != nil && val != false {
			ps.Update = fmt.Sprintf("%v", val)
		}
	}

	return ps
}

func isServiceEnabled(v interface{}) bool {
	switch val := v.(type) {
	case bool:
		return val
	case string:
		return val != "" && val != "false"
	default:
		return false
	}
}

func serviceVersion(v interface{}, defaultVer string) string {
	switch val := v.(type) {
	case string:
		if val != "" && val != "true" {
			return val
		}
	}
	return defaultVer
}

func detectDocroot(repoPath string) string {
	for _, candidate := range []string{"web", "docroot"} {
		if info, err := os.Stat(filepath.Join(repoPath, candidate)); err == nil && info.IsDir() {
			return candidate
		}
	}
	return "web"
}
