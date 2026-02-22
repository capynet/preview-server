package cmd

import (
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

var drushCmd = &cobra.Command{
	Use:   "drush PROJECT/mr-ID [args...]",
	Short: "Run a drush command on a preview",
	Long:  "Run a drush command on a preview. Example: preview drush drupal-test/mr-5 cr",
	Args:  cobra.MinimumNArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}
		drushArgs := strings.Join(args[1:], " ")
		fmt.Fprintf(os.Stderr, "Running drush %s on %s/mr-%d...\n", drushArgs, project, mrID)
		result, err := apiClient.PostDrush(project, mrID, drushArgs)
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
