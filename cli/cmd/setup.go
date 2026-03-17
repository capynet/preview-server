package cmd

import (
	"github.com/spf13/cobra"
)

var setupCmd = &cobra.Command{
	Use:   "setup",
	Short: "Setup commands for project configuration",
	Long:  "Scaffold a Drupal project for preview environments.",
}

func init() {
	rootCmd.AddCommand(setupCmd)
}
