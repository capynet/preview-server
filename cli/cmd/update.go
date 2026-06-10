package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var updateCmd = &cobra.Command{
	Use:   "update",
	Short: "Update the preview pulling code and preserving DB and files",
	Long: `Update the preview environment with the latest code from the current branch.

This syncs the code, runs composer install, and executes deploy scripts
without reimporting the database or files.

Auto-detects the project and preview from the current git branch.

Examples:
  druploy preview update`,
	Args: cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		r, err := resolvePreview()
		if err != nil {
			return err
		}
		announcePreview(r.Project, r.PreviewName, r.Branch)

		fmt.Fprintln(os.Stderr, "Updating...")
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
	previewCmd.AddCommand(updateCmd)
}
