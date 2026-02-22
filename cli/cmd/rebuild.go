package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rebuildCmd = &cobra.Command{
	Use:   "rebuild PROJECT/mr-ID",
	Short: "Trigger a GitLab pipeline rebuild",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "Triggering rebuild for %s/mr-%d...\n", project, mrID)
		result, err := apiClient.PostAction(project, mrID, "rebuild")
		if err != nil {
			return err
		}
		printActionResult(result)
		if result.PipelineURL != "" {
			fmt.Fprintf(os.Stderr, "Pipeline: %s\n", result.PipelineURL)
		}
		if !result.Success {
			os.Exit(1)
		}
		return nil
	},
}

func init() {
	rootCmd.AddCommand(rebuildCmd)
}
