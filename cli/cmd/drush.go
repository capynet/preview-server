package cmd

import (
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

var drushCmd = &cobra.Command{
	Use:   "drush [PROJECT/PREVIEW-NAME] [args...]",
	Short: "Run a drush command on a preview",
	Long: `Run a drush command on a preview.

If PROJECT/PREVIEW-NAME is given, runs drush on that specific preview.
If no preview is specified, auto-detects the project from git remote
and finds a preview matching the current git branch.

Examples:
  preview drush drupal-test/mr-5 cr
  preview drush drupal-test/branch-develop status
  preview drush cr                  # auto-detect from current branch`,
	Args: cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		var project, previewName string

		// Try to parse first arg as PROJECT/PREVIEW-NAME
		if strings.Contains(args[0], "/") {
			p, name, err := parsePreviewName(args[0])
			if err != nil {
				return err
			}
			project = p
			previewName = name
			args = args[1:]
		} else {
			// Auto-detect: all args are drush args
			slug, err := detectProjectSlug()
			if err != nil {
				return err
			}
			branch, err := detectGitBranch()
			if err != nil {
				return err
			}
			fmt.Fprintf(os.Stderr, "Detected project: %s, branch: %s\n", slug, branch)

			preview, err := findPreviewByBranch(slug, branch)
			if err != nil {
				return err
			}
			project = slug
			previewName = preview.Name
			fmt.Fprintf(os.Stderr, "Found preview: %s/%s\n", project, previewName)
		}

		if len(args) == 0 {
			return fmt.Errorf("no drush arguments provided")
		}

		drushArgs := strings.Join(args, " ")
		fmt.Fprintf(os.Stderr, "Running drush %s on %s/%s...\n", drushArgs, project, previewName)
		result, err := apiClient.PostDrushByName(project, previewName, drushArgs)
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
	rootCmd.AddCommand(drushCmd)
}
