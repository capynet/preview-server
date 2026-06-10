package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

var previewCmd = &cobra.Command{
	Use:   "preview",
	Short: "Operate on a preview environment (remote VM)",
	Long: `Commands that act on a single preview environment running on a remote VM.

The target preview is auto-detected from the current git remote and branch,
or can be passed explicitly as PROJECT/PREVIEW-NAME.`,
	GroupID: groupPreview,
}

// movedCommand returns a hidden stub for a command that moved to a new
// location. It fails with a pointer to the new command instead of cobra's
// generic "unknown command" error.
func movedCommand(old, new string) *cobra.Command {
	return &cobra.Command{
		Use:                old,
		Hidden:             true,
		DisableFlagParsing: true,
		SilenceUsage:       true,
		Annotations:        map[string]string{"moved": new},
		RunE: func(cmd *cobra.Command, args []string) error {
			return fmt.Errorf("'druploy %s' has moved to 'druploy %s'", old, new)
		},
	}
}

func init() {
	rootCmd.AddCommand(previewCmd)
	rootCmd.AddCommand(
		movedCommand("ssh", "preview ssh"),
		movedCommand("update", "preview update"),
		movedCommand("rebuild", "preview rebuild"),
		movedCommand("push", "project push"),
	)
}
