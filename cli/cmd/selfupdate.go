package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"

	"github.com/spf13/cobra"
)

var selfUpdateCmd = &cobra.Command{
	Use:   "self-update",
	Short: "Update the CLI to the latest version",
	Args:  cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg := loadConfig()
		if cfg.APIURL == "" {
			cfg.APIURL = defaultAPIURL
		}

		// Check latest version
		fmt.Println("Checking for updates...")
		versionURL := fmt.Sprintf("%s/api/cli/version", cfg.APIURL)
		resp, err := http.Get(versionURL)
		if err != nil {
			return fmt.Errorf("failed to check version: %w", err)
		}
		defer resp.Body.Close()

		if resp.StatusCode != 200 {
			return fmt.Errorf("failed to check version (HTTP %d)", resp.StatusCode)
		}

		body, _ := io.ReadAll(resp.Body)
		var versionInfo struct {
			Version string `json:"version"`
		}
		if err := json.Unmarshal(body, &versionInfo); err != nil {
			return fmt.Errorf("failed to parse version: %w", err)
		}

		if versionInfo.Version == Version {
			fmt.Printf("Already up to date (v%s).\n", Version)
			return nil
		}

		fmt.Printf("Updating v%s -> v%s...\n", Version, versionInfo.Version)

		// Download install script and exec it — this replaces the current process
		installURL := fmt.Sprintf("%s/api/cli/install.sh", cfg.APIURL)
		scriptResp, err := http.Get(installURL)
		if err != nil {
			return fmt.Errorf("failed to download install script: %w", err)
		}
		defer scriptResp.Body.Close()

		if scriptResp.StatusCode != 200 {
			return fmt.Errorf("failed to download install script (HTTP %d)", scriptResp.StatusCode)
		}

		script, err := io.ReadAll(scriptResp.Body)
		if err != nil {
			return fmt.Errorf("failed to read install script: %w", err)
		}

		// Write script to temp file
		tmpFile, err := os.CreateTemp("", "preview-install-*.sh")
		if err != nil {
			return fmt.Errorf("failed to create temp file: %w", err)
		}
		tmpPath := tmpFile.Name()
		defer os.Remove(tmpPath)

		tmpFile.Write(script)
		tmpFile.Close()
		os.Chmod(tmpPath, 0755)

		// Execute the install script (it downloads and replaces the binary)
		sh := exec.Command("sh", tmpPath)
		sh.Stdout = os.Stdout
		sh.Stderr = os.Stderr
		if err := sh.Run(); err != nil {
			return fmt.Errorf("update failed: %w", err)
		}

		// Update cache
		cfg.LatestVersion = versionInfo.Version
		cfg.LastVersionCheck = 0
		saveConfig(cfg)

		return nil
	},
}

func init() {
	rootCmd.AddCommand(selfUpdateCmd)
}
