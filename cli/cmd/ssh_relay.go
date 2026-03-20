package cmd

import (
	"fmt"
	"os"
	"strings"
	"syscall"

	"github.com/spf13/cobra"
)

var sshRelayVM string
var sshRelayContainer string

var sshRelayCmd = &cobra.Command{
	Use:    "ssh-relay",
	Short:  "SSH relay for drush aliases (internal use)",
	Hidden: true,
	// Drush calls: preview ssh-relay --vm=IP --container=NAME user@host "cd /path && command"
	// We get positional args: [user@host, "cd /path && command"]
	Args: cobra.ExactArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		if sshRelayVM == "" || sshRelayContainer == "" {
			return fmt.Errorf("--vm and --container flags are required")
		}

		userAtHost := args[0] // e.g. preview-manager@91.99.157.66
		remoteCmd := args[1]  // e.g. "cd /var/www/html && vendor/bin/drush status"

		// Strip "cd /path && " prefix that drush adds from the root setting
		if idx := strings.Index(remoteCmd, " && "); idx >= 0 && strings.HasPrefix(remoteCmd, "cd ") {
			remoteCmd = remoteCmd[idx+4:]
		}

		// Build the proxy command: "<vm_ip> <container> /var/www/html <command>"
		proxyCmd := fmt.Sprintf("%s %s /var/www/html %s", sshRelayVM, sshRelayContainer, remoteCmd)

		sshBin, err := findSSHBinary()
		if err != nil {
			return err
		}

		sshArgs := []string{
			"ssh",
			"-t",
			"-o", "StrictHostKeyChecking=no",
			"-o", "UserKnownHostsFile=/dev/null",
			"-o", "LogLevel=ERROR",
			userAtHost,
			proxyCmd,
		}

		return syscall.Exec(sshBin, sshArgs, os.Environ())
	},
}

func init() {
	sshRelayCmd.Flags().StringVar(&sshRelayVM, "vm", "", "VM IP address")
	sshRelayCmd.Flags().StringVar(&sshRelayContainer, "container", "", "Docker container name")
	rootCmd.AddCommand(sshRelayCmd)
}
