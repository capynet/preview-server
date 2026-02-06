package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/preview-manager/cli/internal/client"
	"github.com/spf13/cobra"
)

var apiClient *client.Client

var rootCmd = &cobra.Command{
	Use:   "preview",
	Short: "Preview Manager CLI",
	Long:  "CLI tool to manage Drupal preview environments.\n\nRun 'preview login' to authenticate.",
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		// Commands that don't require auth
		name := cmd.Name()
		if name == "setup" || name == "login" || name == "logout" || name == "help" || name == "completion" {
			return
		}

		cfg := loadConfig()
		if cfg.APIURL == "" {
			fmt.Fprintln(os.Stderr, "API URL not configured. Run 'preview login' or 'preview setup <API_URL>' first.")
			os.Exit(1)
		}
		if cfg.Token == "" {
			fmt.Fprintln(os.Stderr, "Not logged in. Run 'preview login' first.")
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

func configPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".preview-manager.json")
}

type config struct {
	APIURL string `json:"api_url"`
	Token  string `json:"token,omitempty"`
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
