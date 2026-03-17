package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var updateCmd = &cobra.Command{
	Use:   "update",
	Short: "Update the preview with the latest code (no DB reimport)",
	Long: `Update the preview environment with the latest code from the current branch.

This syncs the code, runs composer install, and executes deploy scripts
without reimporting the database or files.

Auto-detects the project and preview from the current git branch.

Examples:
  preview update`,
	Args: cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		r, err := resolvePreview()
		if err != nil {
			return err
		}

		fmt.Fprintf(os.Stderr, "Updating %s/%s...\n", r.Project, r.PreviewName)
		result, err := apiClient.PostActionByName(r.Project, r.PreviewName, "rebuild")
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
	rootCmd.AddCommand(updateCmd)
}
