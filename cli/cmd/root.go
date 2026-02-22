package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/preview-manager/cli/internal/client"
	"github.com/spf13/cobra"
)

var apiClient *client.Client

// Version is set at build time via ldflags
var Version = "dev"

var rootCmd = &cobra.Command{
	Use:     "preview",
	Short:   "Preview Manager CLI",
	Long:    "CLI tool to manage Drupal preview environments.\n\nRun 'preview login' to authenticate.",
	Version: Version,
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		cfg := loadConfig()

		// Refresh version cache if stale (every 24h, max 1.5s)
		if cfg.APIURL != "" {
			refreshVersionCache(&cfg)
			printVersionWarning(cfg)
		}

		// Commands that don't require auth
		name := cmd.Name()
		if name == "setup" || name == "api" || name == "project" || name == "login" || name == "logout" || name == "help" || name == "completion" || name == "self-update" {
			return
		}

		if cfg.APIURL == "" {
			fmt.Fprintln(os.Stderr, "API URL not configured. Run 'preview login' or 'preview setup <API_URL>' first.")
			os.Exit(1)
		}
		if cfg.Token == "" {
			fmt.Fprintln(os.Stderr, "Not authenticated. Register this CLI by running:\n")
			fmt.Fprintln(os.Stderr, "  preview login\n")
			fmt.Fprintln(os.Stderr, "This will open a browser to authorize the CLI with your preview server.")
			os.Exit(1)
		}
		apiClient = client.New(cfg.APIURL, cfg.Token)
	},
}

func Execute() {
	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}

// printVersionWarning shows update notice from cached data (instant, no I/O).
func printVersionWarning(cfg config) {
	if cfg.LatestVersion != "" && cfg.LatestVersion != Version {
		yellow := "\033[33m"
		bold := "\033[1m"
		reset := "\033[0m"
		fmt.Fprintf(os.Stderr, "\n%s%sA new version of preview CLI is available (current: %s -> latest: %s)%s\n", yellow, bold, Version, cfg.LatestVersion, reset)
		fmt.Fprintf(os.Stderr, "%sRun 'preview self-update' to update.%s\n\n", yellow, reset)
	}
}

// refreshVersionCache fetches the latest version from the server and updates the cache file.
// Only makes a network call if the cache is older than 24h (max 1.5s timeout).
func refreshVersionCache(cfg *config) {
	if cfg.LastVersionCheck > 0 && time.Since(time.Unix(cfg.LastVersionCheck, 0)) < 24*time.Hour {
		return
	}

	httpClient := &http.Client{Timeout: 1500 * time.Millisecond}
	resp, err := httpClient.Get(strings.TrimSuffix(cfg.APIURL, "/") + "/api/cli/version")
	if err != nil {
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return
	}

	var result struct {
		Version string `json:"version"`
	}
	if err := json.Unmarshal(body, &result); err != nil || result.Version == "" {
		return
	}

	cfg.LatestVersion = result.Version
	cfg.LastVersionCheck = time.Now().Unix()
	saveConfig(*cfg)
}

func configPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".preview-manager.json")
}

type config struct {
	APIURL           string `json:"api_url"`
	Token            string `json:"token,omitempty"`
	LastVersionCheck int64  `json:"last_version_check,omitempty"`
	LatestVersion    string `json:"latest_version,omitempty"`
}

func loadConfig() config {
	var cfg config
	data, err := os.ReadFile(configPath())
	if err != nil {
		return cfg
	}
	json.Unmarshal(data, &cfg)
	return cfg
}

func saveConfig(cfg config) error {
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(configPath(), data, 0600)
}

func init() {
}

// parsePreviewArg parses "project/mr-ID" into (project, mrID).
func parsePreviewArg(arg string) (string, int, error) {
	// Accept both "project/mr-5" and "project/mr-5" formats
	arg = strings.TrimSuffix(arg, "/")

	parts := strings.SplitN(arg, "/", 2)
	if len(parts) != 2 {
		return "", 0, fmt.Errorf("expected format: project/mr-ID (e.g. drupal-test/mr-5)")
	}

	project := parts[0]
	mrPart := parts[1]

	// Strip "mr-" prefix if present
	mrPart = strings.TrimPrefix(mrPart, "mr-")

	mrID, err := strconv.Atoi(mrPart)
	if err != nil {
		return "", 0, fmt.Errorf("invalid MR ID %q: %w", parts[1], err)
	}

	return project, mrID, nil
}

// printActionResult prints an action result in a consistent format.
func printActionResult(result *client.ActionResult) {
	if result.Output != "" {
		fmt.Print(result.Output)
	}
	if !result.Success && result.Error != "" {
		fmt.Fprintf(os.Stderr, "Error: %s\n", result.Error)
	}
}
