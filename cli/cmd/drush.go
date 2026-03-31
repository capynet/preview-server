package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var drushCmd = &cobra.Command{
	Use:   "drush [args...]",
	Short: "Run a drush command on a preview",
	Long: `Run a drush command on a preview via direct SSH.

The project and preview are auto-detected from the current git branch.

Examples:
  druploy drush cr
  druploy drush status
  druploy drush sql-dump > dump.sql
  druploy drush updb -y`,
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

		// Ensure SSH key is on the preview VM
		if err := ensureSSHKeyOnPreview(r); err != nil {
			fmt.Fprintf(os.Stderr, "Warning: could not inject SSH key: %v\n", err)
		}

		drushArgs := append([]string{"vendor/bin/drush"}, args...)
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
				// Re-inject SSH key for new VM
				if err := ensureSSHKeyOnPreview(r2); err != nil {
					fmt.Fprintf(os.Stderr, "Warning: could not inject SSH key: %v\n", err)
				}
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
