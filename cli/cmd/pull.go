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
	Long:  "Download database dumps or files archives from a preview environment.",
}

var pullDBCmd = &cobra.Command{
	Use:   "db PROJECT/mr-ID",
	Short: "Download database dump from a preview",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}

		output := pullOutputFile
		if output == "" {
			output = fmt.Sprintf("%s-mr-%d.sql.gz", project, mrID)
		}

		fmt.Fprintf(os.Stderr, "Downloading database from %s/mr-%d to %s...\n", project, mrID, output)

		f, err := os.Create(output)
		if err != nil {
			return fmt.Errorf("cannot create file: %w", err)
		}
		defer f.Close()

		if err := apiClient.DownloadStream(project, mrID, "db", f); err != nil {
			os.Remove(output)
			return err
		}

		fmt.Fprintf(os.Stderr, "Saved to %s\n", output)
		return nil
	},
}

var pullFilesCmd = &cobra.Command{
	Use:   "files PROJECT/mr-ID",
	Short: "Download files archive from a preview",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}

		output := pullOutputFile
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
	pullDBCmd.Flags().StringVarP(&pullOutputFile, "output", "o", "", "Output file path")
	pullFilesCmd.Flags().StringVarP(&pullOutputFile, "output", "o", "", "Output file path")
	pullCmd.AddCommand(pullDBCmd)
	pullCmd.AddCommand(pullFilesCmd)
	rootCmd.AddCommand(pullCmd)
}
