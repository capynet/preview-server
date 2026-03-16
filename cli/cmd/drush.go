package cmd

import (
	"os"

	"github.com/spf13/cobra"
)

var drushCmd = &cobra.Command{
	Use:   "drush [args...]",
	Short: "Run a drush command on a preview",
	Long: `Run a drush command on a preview via direct SSH.

The project and preview are auto-detected from the current git branch.

Examples:
  preview drush cr
  preview drush status
  preview drush sql-dump > dump.sql
  preview drush updb -y`,
	Args:               cobra.MinimumNArgs(1),
	DisableFlagParsing: true,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := ensureSSHKeyRegistered(); err != nil {
			return err
		}

		// Try with cached resolve
		r, err := resolvePreview()
		if err != nil {
			return err
		}

		drushArgs := append([]string{"drush"}, args...)
		exitCode := runSSHCommand(r, "php", drushArgs)

		if exitCode != 0 {
			// Command failed — maybe cache is stale (VM changed, preview recreated)
			r2, err := retryResolve()
			if err != nil {
				// Re-resolution failed — report the original error
				os.Exit(exitCode)
			}

			// If the resolved info changed, retry the command
			if r2.VmIP != r.VmIP || r2.PreviewName != r.PreviewName {
				exitCode = runSSHCommand(r2, "php", drushArgs)
			}
		}

		if exitCode != 0 {
			os.Exit(exitCode)
		}
		return nil
	},
}

func init() {
	rootCmd.AddCommand(drushCmd)
}
