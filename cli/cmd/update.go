package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var updateCmd = &cobra.Command{
	Use:   "update [PROJECT/PREVIEW-NAME]",
	Short: "Update the preview pulling code and preserving DB and files",
	Long: `Update the preview environment with the latest code from the current branch.

This syncs the code, runs composer install, and executes deploy scripts
without reimporting the database or files.

If PROJECT/PREVIEW-NAME is given, updates that specific preview.
Otherwise, auto-detects from git remote and current branch.

Examples:
  druploy preview update
  druploy preview update soudal/mr-1584`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		var project, previewName string

		if len(args) == 1 {
			var err error
			project, previewName, err = parsePreviewName(args[0])
			if err != nil {
				return err
			}
			if err := resolveOrgForProject(project); err != nil {
				return err
			}
			announcePreview(project, previewName, "")
		} else {
			r, err := resolvePreview()
			if err != nil {
				return err
			}
			project = r.Project
			previewName = r.PreviewName
			announcePreview(project, previewName, r.Branch)
		}

		fmt.Fprintln(os.Stderr, "Updating...")
		result, err := apiClient.PostActionByName(project, previewName, "rebuild")
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
