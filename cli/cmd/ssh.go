package cmd

import (
	"bufio"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"github.com/spf13/cobra"
)

var sshCmd = &cobra.Command{
	Use:   "ssh [container] [project/preview-name]",
	Short: "SSH into a container on a preview environment",
	Long: `Open an interactive shell in a container running on a preview VM.

The default container is "php" (lands in /var/www/html).
Use "db" to connect to the database container.

If project/preview-name is not given, auto-detects from the current git branch.
On first use, you'll be asked to register your SSH key.

Examples:
  preview ssh                        # auto-detect, php container
  preview ssh db                     # auto-detect, db container
  preview ssh soudal/mr-1597         # explicit preview, php container
  preview ssh db soudal/mr-1597      # explicit preview, db container`,
	Args: cobra.MaximumNArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		container := "php"
		var explicitPreview string

		switch len(args) {
		case 0:
			// Auto-detect everything
		case 1:
			if strings.Contains(args[0], "/") {
				explicitPreview = args[0]
			} else {
				container = args[0]
			}
		case 2:
			container = args[0]
			explicitPreview = args[1]
		}

		if err := ensureSSHKeyRegistered(); err != nil {
			return err
		}

		var r *resolvedPreview
		var err error

		if explicitPreview != "" {
			r, err = resolveExplicitPreview(explicitPreview)
		} else {
			r, err = resolvePreview()
		}
		if err != nil {
			return err
		}

		return execSSH(r, container, nil)
	},
}

// execSSH connects to a container via SSH. If command is nil, opens interactive bash.
// Returns error only if SSH binary not found or syscall.Exec fails.
func execSSH(r *resolvedPreview, container string, command []string) error {
	containerName := fmt.Sprintf("%s-%s-%s", r.PreviewName, r.Project, container)

	var proxyCmd string
	if len(command) > 0 {
		// Non-interactive: run command
		proxyCmd = fmt.Sprintf("%s %s /var/www/html %s", r.VmIP, containerName, strings.Join(command, " "))
	} else {
		// Interactive: open bash
		proxyCmd = fmt.Sprintf("%s %s", r.VmIP, containerName)
		if container == "php" {
			proxyCmd += " /var/www/html"
		}
	}

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
		fmt.Sprintf("preview-manager@%s", r.JumpHost),
		proxyCmd,
	}

	return syscall.Exec(sshBin, sshArgs, os.Environ())
}

// runSSHCommand runs an SSH command and returns the exit code (without replacing the process).
func runSSHCommand(r *resolvedPreview, container string, command []string) int {
	containerName := fmt.Sprintf("%s-%s-%s", r.PreviewName, r.Project, container)
	proxyCmd := fmt.Sprintf("%s %s /var/www/html %s", r.VmIP, containerName, strings.Join(command, " "))

	sshBin, err := findSSHBinary()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		return 1
	}

	cmd := exec.Command(sshBin,
		"-t",
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=/dev/null",
		"-o", "LogLevel=ERROR",
		fmt.Sprintf("preview-manager@%s", r.JumpHost),
		proxyCmd,
	)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			return exitErr.ExitCode()
		}
		return 1
	}
	return 0
}

// ensureSSHKeyRegistered checks if the user has a registered SSH key.
// If not, it finds local keys, asks which one to use, and registers it.
func ensureSSHKeyRegistered() error {
	existingKeys, err := apiClient.ListSSHKeys()
	if err == nil && len(existingKeys) > 0 {
		return nil
	}

	fmt.Fprintln(os.Stderr, "\nNo SSH key registered. Let's set one up.")

	sshDir := filepath.Join(os.Getenv("HOME"), ".ssh")
	keys, err := findLocalSSHKeys(sshDir)
	if err != nil || len(keys) == 0 {
		return fmt.Errorf("no SSH public keys found in ~/.ssh/\nGenerate one with: ssh-keygen -t ed25519")
	}

	var selectedKey string

	if len(keys) == 1 {
		k := keys[0]
		fmt.Fprintf(os.Stderr, "Found SSH key: %s (%s)\n", filepath.Base(k.path), k.comment)
		fmt.Fprint(os.Stderr, "Register this key? [Y/n] ")
		reader := bufio.NewReader(os.Stdin)
		input, _ := reader.ReadString('\n')
		input = strings.TrimSpace(strings.ToLower(input))
		if input != "" && input != "y" && input != "yes" {
			return fmt.Errorf("SSH key registration cancelled")
		}
		selectedKey = k.content
	} else {
		fmt.Fprintln(os.Stderr, "Found SSH keys:")
		for i, k := range keys {
			fmt.Fprintf(os.Stderr, "  %d) %s — %s\n", i+1, filepath.Base(k.path), k.comment)
		}
		fmt.Fprint(os.Stderr, "\nSelect key to register: ")
		reader := bufio.NewReader(os.Stdin)
		input, _ := reader.ReadString('\n')
		input = strings.TrimSpace(input)
		idx, err := strconv.Atoi(input)
		if err != nil || idx < 1 || idx > len(keys) {
			return fmt.Errorf("invalid selection: %q", input)
		}
		selectedKey = keys[idx-1].content
	}

	result, err := apiClient.RegisterSSHKey(selectedKey)
	if err != nil {
		return fmt.Errorf("failed to register SSH key: %w", err)
	}

	fmt.Fprintf(os.Stderr, "SSH key registered: %s (%s)\n\n", result.Name, result.Fingerprint)
	return nil
}

type localSSHKey struct {
	path    string
	content string
	comment string
}

func findLocalSSHKeys(sshDir string) ([]localSSHKey, error) {
	entries, err := os.ReadDir(sshDir)
	if err != nil {
		return nil, err
	}

	var keys []localSSHKey
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".pub") {
			continue
		}

		path := filepath.Join(sshDir, entry.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}

		content := strings.TrimSpace(string(data))
		if !strings.HasPrefix(content, "ssh-") && !strings.HasPrefix(content, "ecdsa-") {
			continue
		}

		parts := strings.Fields(content)
		comment := entry.Name()
		if len(parts) >= 3 {
			comment = strings.Join(parts[2:], " ")
		}

		keys = append(keys, localSSHKey{
			path:    path,
			content: content,
			comment: comment,
		})
	}

	return keys, nil
}

func resolveSSHJumpHost(apiURL string) (string, error) {
	parsed, err := url.Parse(apiURL)
	if err != nil {
		return "", fmt.Errorf("invalid API URL %q: %w", apiURL, err)
	}

	hostname := parsed.Hostname()
	if hostname == "" {
		return "", fmt.Errorf("no hostname in API URL %q", apiURL)
	}

	if ip := net.ParseIP(hostname); ip != nil {
		return hostname, nil
	}

	ips, err := net.LookupHost(hostname)
	if err != nil {
		return "", fmt.Errorf("could not resolve %q: %w", hostname, err)
	}
	if len(ips) == 0 {
		return "", fmt.Errorf("no IPs found for %q", hostname)
	}

	return ips[0], nil
}

func findSSHBinary() (string, error) {
	paths := strings.Split(os.Getenv("PATH"), ":")
	for _, dir := range paths {
		candidate := dir + "/ssh"
		if _, err := os.Stat(candidate); err == nil {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("ssh binary not found in PATH")
}

func init() {
	rootCmd.AddCommand(sshCmd)
}
