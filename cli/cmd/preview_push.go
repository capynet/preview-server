package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"
)

var previewPushReplace bool
var previewPushDryRun bool

var previewPushCmd = &cobra.Command{
	Use:   "push",
	Short: "Push local data directly to a preview",
	Long: `Push local data to a single preview environment.

Unlike "druploy project push" (which updates the shared base used to seed
every new preview), these commands only touch the target preview.`,
}

var previewPushFilesCmd = &cobra.Command{
	Use:   "files [PROJECT/PREVIEW-NAME]",
	Short: "Rsync the local Drupal files dir to the preview",
	Long: `Sync the local Drupal files directory to the preview via rsync over SSH.

Only the target preview is touched — the project's base files are not modified.
The same exclusions as "druploy project push files" apply: css/, js/ and php/
are always excluded; styles/ with --no-image-styles; big files with
--strip-heavy-files.

By default the sync is additive: local files are added or updated on the
preview, nothing is deleted. With --replace, the preview's files dir is wiped
completely first, so it ends up containing exactly what you send.

Use --dry-run to get a size report of the payload without connecting or
transferring anything — useful to tune the exclusion flags until the size
feels right.

Examples:
  druploy preview push files --dry-run --no-image-styles
  druploy preview push files --no-image-styles --strip-heavy-files 5mb
  druploy preview push files --replace my-site/mr-1597`,
	Args: cobra.MaximumNArgs(1),
	RunE: runPreviewPushFiles,
}

func runPreviewPushFiles(cmd *cobra.Command, args []string) error {
	rsyncBin, err := exec.LookPath("rsync")
	if err != nil {
		return fmt.Errorf("rsync not found in PATH — install it first (e.g. sudo apt install rsync)")
	}

	if stripHeavyFiles != "" {
		if _, err := parseSizeMB(stripHeavyFiles); err != nil {
			return err
		}
	}

	// Detect the local files dir via ddev drush
	if err := ensureDdevRunning(); err != nil {
		return err
	}
	localDir, relFiles, err := getDrupalFilesDir()
	if err != nil {
		return fmt.Errorf("could not detect files directory: %w", err)
	}
	if _, err := os.Stat(localDir); os.IsNotExist(err) {
		return fmt.Errorf("files directory %q not found — are you in the project root?", localDir)
	}

	// Dry run: purely local payload report, no connection needed
	if previewPushDryRun {
		return previewPushDryRunReport(rsyncBin, localDir)
	}

	// Resolve target preview
	var r *resolvedPreview
	explicit := len(args) == 1
	if explicit {
		r, err = resolveExplicitPreview(args[0])
		if err != nil {
			return err
		}
		announcePreview(r.Project, r.PreviewName, "")
	} else {
		r, err = resolvePreview()
		if err != nil {
			return err
		}
		announcePreview(r.Project, r.PreviewName, r.Branch)
	}

	// Remote files dir: /var/www/html[/docroot]/<relFiles>
	remoteRoot := "/var/www/html"
	if cfg, cfgErr := apiClient.GetPreviewConfig(r.Project, r.PreviewName); cfgErr == nil && cfg != nil {
		if cfg.Docroot != "" && cfg.Docroot != "." {
			remoteRoot += "/" + cfg.Docroot
		}
	} else if cfgErr != nil {
		fmt.Fprintf(os.Stderr, "Warning: could not fetch preview config (%v), assuming Drupal root at %s\n", cfgErr, remoteRoot)
	}
	remoteDir := remoteRoot + "/" + relFiles
	// Paranoia guard: --replace wipes this path, never let it escape the docroot
	if !strings.HasPrefix(remoteDir, "/var/www/html/") || !strings.Contains(remoteDir, "files") {
		return fmt.Errorf("refusing to operate on suspicious remote files path %q", remoteDir)
	}

	// Make sure SSH access works (key registered + injected into the preview)
	if err := ensureSSHKeyRegistered(); err != nil {
		return err
	}
	if err := ensureSSHKeyOnPreview(r); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: could not inject SSH key: %v\n", err)
	}

	msg := fmt.Sprintf("Sync local files to preview %s/%s?", r.Project, r.PreviewName)
	if previewPushReplace {
		msg = fmt.Sprintf("WIPE the files dir of preview %s/%s completely and upload your local content? Nothing currently on the preview will be kept.", r.Project, r.PreviewName)
	}
	if !confirm(msg) {
		fmt.Fprintln(os.Stderr, "Aborted.")
		return nil
	}

	runSync := func(r *resolvedPreview) int {
		if previewPushReplace {
			fmt.Fprintf(os.Stderr, "Wiping %s on the preview...\n", remoteDir)
			wipe := fmt.Sprintf("mkdir -p %q && find %q -mindepth 1 -delete", remoteDir, remoteDir)
			if code := runSSHCommand(r, "php", []string{wipe}); code != 0 {
				fmt.Fprintf(os.Stderr, "Error: failed to wipe remote files dir (exit %d)\n", code)
				return code
			}
		}

		fmt.Fprintf(os.Stderr, "Syncing %s → %s...\n", localDir, remoteDir)
		c := exec.Command(rsyncBin, buildPreviewRsyncArgs(localDir, remoteDir, r.VmIP)...)
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		if err := c.Run(); err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				return exitErr.ExitCode()
			}
			return 1
		}
		return 0
	}

	exitCode := runSync(r)
	if exitCode != 0 && !explicit {
		// Cached VM info may be stale — refresh and retry once
		r, err = retryResolve()
		if err != nil {
			return err
		}
		exitCode = runSync(r)
	}
	if exitCode != 0 {
		return fmt.Errorf("sync failed (exit code %d)", exitCode)
	}

	fmt.Fprintf(os.Stderr, "Done! Files synced to %s/%s.\n", r.Project, r.PreviewName)
	return nil
}

// previewPushDryRunReport computes the payload size locally by dry-running
// rsync against an empty directory with the same filters applied.
func previewPushDryRunReport(rsyncBin, localDir string) error {
	tmp, err := os.MkdirTemp("", "druploy-dryrun-")
	if err != nil {
		return fmt.Errorf("failed to create temp dir: %w", err)
	}
	defer os.RemoveAll(tmp)

	args := append([]string{"--dry-run"}, previewRsyncFilterArgs()...)
	args = append(args, "-rlpt", "--stats", "--human-readable",
		strings.TrimSuffix(localDir, "/")+"/", tmp+"/")

	fmt.Fprintf(os.Stderr, "Dry run — computing payload from %s with the current flags...\n\n", localDir)
	c := exec.Command(rsyncBin, args...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Run(); err != nil {
		return fmt.Errorf("rsync dry run failed: %w", err)
	}

	fmt.Fprintln(os.Stderr, "\nNothing was sent (local dry run). \"Total transferred file size\" is the payload a real push would send.")
	return nil
}

// previewRsyncFilterArgs returns the exclusion/size filters shared by the
// real sync and the dry-run report.
func previewRsyncFilterArgs() []string {
	args := []string{"--exclude=/css", "--exclude=/js", "--exclude=/php"}
	if noImageStyles {
		args = append(args, "--exclude=/styles")
	}
	if stripHeavyFiles != "" {
		if maxBytes, err := parseSizeMB(stripHeavyFiles); err == nil {
			args = append(args, fmt.Sprintf("--max-size=%d", maxBytes))
		}
	}
	return args
}

// buildPreviewRsyncArgs assembles the rsync invocation for the real sync.
func buildPreviewRsyncArgs(localDir, remoteDir, vmIP string) []string {
	sshCmd := "ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
	args := []string{"-rlptz", "-e", sshCmd}
	args = append(args, previewRsyncFilterArgs()...)
	args = append(args, "--stats", "--human-readable", "--info=progress2",
		strings.TrimSuffix(localDir, "/")+"/",
		fmt.Sprintf("preview@%s:%s/", vmIP, remoteDir),
	)
	return args
}

func init() {
	previewPushFilesCmd.Flags().BoolVar(&noImageStyles, "no-image-styles", false, "Exclude Drupal image styles (styles/ directory) — they regenerate on demand")
	previewPushFilesCmd.Flags().StringVar(&stripHeavyFiles, "strip-heavy-files", "", "Exclude files larger than this size, e.g. --strip-heavy-files 10mb")
	previewPushFilesCmd.Flags().BoolVar(&previewPushReplace, "replace", false, "Wipe the preview's files dir completely before sending")
	previewPushFilesCmd.Flags().BoolVar(&previewPushDryRun, "dry-run", false, "Local size report of the payload, without connecting or sending anything")
	previewPushFilesCmd.Flags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	previewPushCmd.AddCommand(previewPushFilesCmd)
	previewCmd.AddCommand(previewPushCmd)
}
