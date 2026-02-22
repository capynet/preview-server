package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var stopCmd = &cobra.Command{
	Use:   "stop PROJECT/mr-ID",
	Short: "Stop a preview (docker compose stop)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "Stopping %s/mr-%d...\n", project, mrID)
		result, err := apiClient.PostAction(project, mrID, "stop")
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
	rootCmd.AddCommand(stopCmd)
}
