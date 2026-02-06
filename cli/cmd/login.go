package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"

	"github.com/spf13/cobra"
)

var loginNoBrowser bool

var loginCmd = &cobra.Command{
	Use:   "login PROJECT/mr-ID",
	Short: "Get a one-time login link (drush uli)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		project, mrID, err := parsePreviewArg(args[0])
		if err != nil {
			return err
		}
		result, err := apiClient.PostAction(project, mrID, "drush-uli")
		if err != nil {
			return err
		}
		if !result.Success {
			printActionResult(result)
			os.Exit(1)
		}

		url := strings.TrimSpace(result.Output)
		fmt.Println(url)

		if !loginNoBrowser && url != "" {
			openBrowser(url)
		}
		return nil
	},
}

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "linux":
		cmd = exec.Command("xdg-open", url)
	default:
		return
	}
	_ = cmd.Start()
}

func init() {
	loginCmd.Flags().BoolVar(&loginNoBrowser, "no-browser", false, "Don't open the URL in a browser")
	rootCmd.AddCommand(loginCmd)
}
