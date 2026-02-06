package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

var setupCmd = &cobra.Command{
	Use:   "setup API_URL",
	Short: "Configure the API URL",
	Long:  "Save the API URL to ~/.preview-manager.json so you don't need --api-url every time.",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg := loadConfig()
		cfg.APIURL = args[0]
		if err := saveConfig(cfg); err != nil {
			return fmt.Errorf("failed to save config: %w", err)
		}
		fmt.Printf("API URL saved: %s\n", cfg.APIURL)
		fmt.Printf("Config file: %s\n", configPath())
		return nil
	},
}

func init() {
	rootCmd.AddCommand(setupCmd)
}
