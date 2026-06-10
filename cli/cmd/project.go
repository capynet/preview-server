package cmd

import (
	"github.com/spf13/cobra"
)

var projectCmd = &cobra.Command{
	Use:   "project",
	Short: "Manage project-level resources shared by all previews",
	Long: `Commands that act on project-level resources — shared by every preview
of the project — such as the base database and files archive.`,
	GroupID: groupProject,
}

func init() {
	rootCmd.AddCommand(projectCmd)
}
