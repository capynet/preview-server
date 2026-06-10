package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rebuildCmd = &cobra.Command{
	Use:   "rebuild [PROJECT/PREVIEW-NAME]",
	Short: "Rebuild the preview from scratch (new VM, fresh deploy)",
	Long: `Rebuild the preview environment from scratch.

If PROJECT/PREVIEW-NAME is given, rebuilds that specific preview.
Otherwise, auto-detects from git remote and current branch.

Examples:
  druploy preview rebuild
  druploy preview rebuild soudal/branch-master
  druploy preview rebuild soudal/mr-1584`,
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

		if !confirm("Rebuild destroys the current VM and redeploys from scratch (DB and files are reimported from the base). Continue?") {
			fmt.Fprintln(os.Stderr, "Aborted.")
			return nil
		}

		fmt.Fprintln(os.Stderr, "Rebuilding...")
		result, err := apiClient.PostActionByName(project, previewName, "rebuild?force_new=true")
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
	rebuildCmd.Flags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	previewCmd.AddCommand(rebuildCmd)
}
