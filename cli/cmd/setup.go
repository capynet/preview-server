package cmd

import (
	"github.com/spf13/cobra"
)

var setupCmd = &cobra.Command{
	Use:   "setup",
	Short: "Scaffold a Drupal project for preview environments",
	Long: `Creates the necessary files for preview compatibility:

  1. Creates web/sites/default/settings.preview.php for custom overrides
  2. Creates preview.yml template in the project root
  3. Creates deploy script templates in scripts/preview/

The preview include snippet in settings.php and the internal settings
file (settings.preview.internal.php) are managed automatically by
the deployer — you don't need to touch them.

Run this command from the root of your Drupal project.
Use --override to overwrite existing files with the latest templates.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return runSetupProject()
	},
}

func init() {
	setupCmd.Flags().BoolVar(&overrideFlag, "override", false, "Overwrite existing files with the latest templates")
	rootCmd.AddCommand(setupCmd)
}
