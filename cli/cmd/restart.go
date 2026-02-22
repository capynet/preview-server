package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var restartCmd = &cobra.Command{
	Use:   "restart PROJECT/mr-ID",
	Short: "Restart a preview (docker compose restart)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "Restarting %s/mr-%d...\n", project, mrID)
		result, err := apiClient.PostAction(project, mrID, "restart")
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
	rootCmd.AddCommand(restartCmd)
}
