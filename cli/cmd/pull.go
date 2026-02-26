package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var pullOutputFile string

var pullCmd = &cobra.Command{
	Use:   "pull",
	Short: "Pull files from a preview environment",
	Long: `Download database dumps or files archives from a preview environment.

If PROJECT/PREVIEW-NAME is given, downloads from that specific preview.
If no argument is given, auto-detects the project from git remote and
finds a preview matching the current git branch.`,
}

// resolvePullTarget resolves the project and preview name from args or auto-detection.
func resolvePullTarget(args []string) (project, previewName string, err error) {
	if len(args) == 1 {
		return parsePreviewName(args[0])
	}

	// Auto-detect project from git remote
	project, err = detectProjectSlug()
	if err != nil {
		return "", "", err
	}

	// Auto-detect branch
	branch, err := detectGitBranch()
	if err != nil {
		return "", "", err
	}
	fmt.Fprintf(os.Stderr, "Detected branch: %s\n", branch)

	// Find preview matching this branch
	preview, err := findPreviewByBranch(project, branch)
	if err != nil {
		return "", "", err
	}
	fmt.Fprintf(os.Stderr, "Found preview: %s (branch: %s)\n", preview.Name, preview.Branch)

	return project, preview.Name, nil
}

var pullDBCmd = &cobra.Command{
	Use:   "db [PROJECT/PREVIEW-NAME]",
	Short: "Download database dump from a preview",
	Long: `Download a database dump from a preview environment.

If PROJECT/PREVIEW-NAME is given, downloads from that specific preview.
If no argument is given, auto-detects from git remote and current branch.`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, previewName, err := resolvePullTarget(args)
		if err != nil {
			return err
		}

		output := pullOutputFile
		if output == "" {
			output = fmt.Sprintf("%s-%s.sql.gz", project, previewName)
		}

		fmt.Fprintf(os.Stderr, "Downloading database from %s/%s to %s...\n", project, previewName, output)

		f, err := os.Create(output)
		if err != nil {
			return fmt.Errorf("cannot create file: %w", err)
		}
		defer f.Close()

		if err := apiClient.DownloadStream(project, previewName, "db", f); err != nil {
			os.Remove(output)
			return err
		}

		fmt.Fprintf(os.Stderr, "Saved to %s\n", output)
		return nil
	},
}

var pullFilesCmd = &cobra.Command{
	Use:   "files [PROJECT/PREVIEW-NAME]",
	Short: "Download files archive from a preview",
	Long: `Download a files archive from a preview environment.

If PROJECT/PREVIEW-NAME is given, downloads from that specific preview.
If no argument is given, auto-detects from git remote and current branch.`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, previewName, err := resolvePullTarget(args)
		if err != nil {
			return err
		}

		output := pullOutputFile
		if output == "" {
			output = fmt.Sprintf("%s-%s-files.tar.gz", project, previewName)
		}

		fmt.Fprintf(os.Stderr, "Downloading files from %s/%s to %s...\n", project, previewName, output)

		f, err := os.Create(output)
		if err != nil {
			return fmt.Errorf("cannot create file: %w", err)
		}
		defer f.Close()

		if err := apiClient.DownloadStream(project, previewName, "files", f); err != nil {
			os.Remove(output)
			return err
		}

		fmt.Fprintf(os.Stderr, "Saved to %s\n", output)
		return nil
	},
}

func init() {
	pullDBCmd.Flags().StringVarP(&pullOutputFile, "output", "o", "", "Output file path")
	pullFilesCmd.Flags().StringVarP(&pullOutputFile, "output", "o", "", "Output file path")
	pullCmd.AddCommand(pullDBCmd)
	pullCmd.AddCommand(pullFilesCmd)
	rootCmd.AddCommand(pullCmd)
}
