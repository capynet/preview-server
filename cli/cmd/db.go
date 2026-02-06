package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var dbOutputFile string

var dbCmd = &cobra.Command{
	Use:   "db",
	Short: "Database operations",
}

var dbDownloadCmd = &cobra.Command{
	Use:   "download PROJECT/mr-ID",
	Short: "Download database dump",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}

		output := dbOutputFile
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

func init() {
	dbDownloadCmd.Flags().StringVarP(&dbOutputFile, "output", "o", "", "Output file path")
	dbCmd.AddCommand(dbDownloadCmd)
	rootCmd.AddCommand(dbCmd)
}
