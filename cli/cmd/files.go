package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var filesOutputFile string

var filesCmd = &cobra.Command{
	Use:   "files",
	Short: "Files operations",
}

var filesDownloadCmd = &cobra.Command{
	Use:   "download PROJECT/mr-ID",
	Short: "Download Drupal files directory",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}

		output := filesOutputFile
		if output == "" {
			output = fmt.Sprintf("%s-mr-%d-files.tar.gz", project, mrID)
		}

		fmt.Fprintf(os.Stderr, "Downloading files from %s/mr-%d to %s...\n", project, mrID, output)

		f, err := os.Create(output)
		if err != nil {
			return fmt.Errorf("cannot create file: %w", err)
		}
		defer f.Close()

		if err := apiClient.DownloadStream(project, mrID, "files", f); err != nil {
			os.Remove(output)
			return err
		}

		fmt.Fprintf(os.Stderr, "Saved to %s\n", output)
		return nil
	},
}

func init() {
	filesDownloadCmd.Flags().StringVarP(&filesOutputFile, "output", "o", "", "Output file path")
	filesCmd.AddCommand(filesDownloadCmd)
	rootCmd.AddCommand(filesCmd)
}
