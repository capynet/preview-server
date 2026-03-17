package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rebuildCmd = &cobra.Command{
	Use:   "rebuild",
	Short: "Rebuild the preview from scratch (new VM, fresh deploy)",
	Long: `Rebuild the preview environment from scratch.

Auto-detects the project and preview from the current git branch.

Examples:
  preview rebuild`,
	Args: cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		r, err := resolvePreview()
		if err != nil {
			return err
		}

		fmt.Fprintf(os.Stderr, "Rebuilding %s/%s...\n", r.Project, r.PreviewName)
		result, err := apiClient.PostActionByName(r.Project, r.PreviewName, "rebuild?force_new=true")
		if err != nil {
			return err
		}
		printActionResult(result)
		if !result.Success {
			os.Exit(1)
		}
		return nil
	},
}

func init() {
	rootCmd.AddCommand(rebuildCmd)
}
