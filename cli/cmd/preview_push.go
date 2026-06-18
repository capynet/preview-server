package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
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

var previewPushDBCmd = &cobra.Command{
	Use:   "db [PROJECT/PREVIEW-NAME]",
	Short: "Replace the preview's database with your local one",
	Long: `Dump the local database (via ddev, cache tables structure-only) and import
it into the target preview, replacing its current database completely.

Only the target preview is touched — the project's base database is not
modified. After the import, caches are rebuilt with drush cr.

Examples:
  druploy preview push db
  druploy preview push db my-site/mr-1597`,
	Args: cobra.MaximumNArgs(1),
	RunE: runPreviewPushDB,
}

func runPreviewPushDB(cmd *cobra.Command, args []string) error {
	sshBin, err := findSSHBinary()
	if err != nil {
		return err
	}

	r, _, err := resolvePreviewPushTarget(args)
	if err != nil {
		return err
	}

	// Make sure SSH access works (key registered + injected into the preview)
	if err := ensureSSHKeyRegistered(); err != nil {
		return err
	}
	if err := ensureSSHKeyOnPreview(r); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: could not inject SSH key: %v\n", err)
	}

	if !confirm(fmt.Sprintf("REPLACE the database of preview %s/%s with your local database? Its current database will be lost.", r.Project, r.PreviewName)) {
		fmt.Fprintln(os.Stderr, "Aborted.")
		return nil
	}

	// Ensure ddev is running before piping stdout, so startup messages
	// don't get mixed into the SQL dump
	if err := ensureDdevRunning(); err != nil {
		return err
	}

	fmt.Fprintln(os.Stderr, "Generating database dump...")
	estimate := getLocalDBSizeEstimate()
	dump, _, wait, err := startLocalDBDump(estimate, false)
	if err != nil {
		return err
	}

	fmt.Fprintf(os.Stderr, "Importing into %s/%s. Large databases take a few minutes:\n", r.Project, r.PreviewName)

	// Raw SQL straight into the mysql client (no compression — transfer size is
	// never the bottleneck). We bypass `drush sql:cli` and feed the client
	// returned by `drush sql:connect` directly: sql:cli adds ~5x overhead on a
	// large stdin stream (the "slow stdin" warning), while sql:connect just
	// prints the mysql command, so the dump streams straight into mysql at the
	// server's real import speed. drush cr runs separately afterwards.
	remoteCmd := "cd /var/www/html && vendor/bin/drush sql:drop -y >/dev/null 2>&1 && eval \"$(vendor/bin/drush sql:connect)\" 2>/dev/null"
	c := exec.Command(sshBin,
		"-p", "2222",
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=/dev/null",
		"-o", "LogLevel=ERROR",
		// Keep the session alive across slow, quiet stretches of a big import
		// so an idle TCP path doesn't drop the connection mid-transfer.
		"-o", "ServerAliveInterval=30",
		"-o", "ServerAliveCountMax=20",
		fmt.Sprintf("preview@%s", r.VmIP),
		remoteCmd,
	)
	c.Stdin = dump
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Run(); err != nil {
		return fmt.Errorf("database import failed: %w", err)
	}
	if err := wait(); err != nil {
		return err
	}

	fmt.Fprintln(os.Stderr, "Rebuilding caches (drush cr)...")
	if code := runSSHCommand(r, "php", []string{"vendor/bin/drush", "cr"}); code != 0 {
		fmt.Fprintln(os.Stderr, "Warning: drush cr failed — the database was imported, run it manually if needed.")
	}

	fmt.Fprintf(os.Stderr, "Done! Database of %s/%s replaced.\n", r.Project, r.PreviewName)
	return nil
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

// resolvePreviewPushTarget resolves the target preview from an optional
// explicit PROJECT/PREVIEW-NAME arg and announces it. The bool reports
// whether the target was explicit.
func resolvePreviewPushTarget(args []string) (*resolvedPreview, bool, error) {
	if len(args) == 1 {
		r, err := resolveExplicitPreview(args[0])
		if err != nil {
			return nil, false, err
		}
		announcePreview(r.Project, r.PreviewName, "")
		return r, true, nil
	}
	r, err := resolvePreview()
	if err != nil {
		return nil, false, err
	}
	announcePreview(r.Project, r.PreviewName, r.Branch)
	return r, false, nil
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
		fmt.Fprintf(os.Stderr, "Dry run — computing payload from %s...\n", localDir)
		payload, err := previewPushPayloadReport(rsyncBin, localDir)
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "\nNothing was sent (dry run). A real push would transfer %s.\n", formatBytesShort(payload))
		return nil
	}

	// Resolve target preview
	r, explicit, err := resolvePreviewPushTarget(args)
	if err != nil {
		return err
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

	// Show the payload summary before asking for confirmation
	if _, err := previewPushPayloadReport(rsyncBin, localDir); err != nil {
		return err
	}
	fmt.Fprintln(os.Stderr)

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

		fmt.Fprintf(os.Stderr, "Syncing local: %s → preview: %s...\n", localDir, remoteDir)
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

// previewPushPayloadReport computes the payload size locally by dry-running
// rsync against an empty directory with the same filters applied, and prints
// a compact summary. Returns the payload size in bytes.
func previewPushPayloadReport(rsyncBin, localDir string) (int64, error) {
	tmp, err := os.MkdirTemp("", "druploy-dryrun-")
	if err != nil {
		return 0, fmt.Errorf("failed to create temp dir: %w", err)
	}
	defer os.RemoveAll(tmp)

	args := append([]string{"--dry-run"}, previewRsyncFilterArgs()...)
	args = append(args, "-rlpt", "--stats",
		strings.TrimSuffix(localDir, "/")+"/", tmp+"/")

	c := exec.Command(rsyncBin, args...)
	c.Stderr = os.Stderr
	out, err := c.Output()
	if err != nil {
		return 0, fmt.Errorf("rsync dry run failed: %w", err)
	}

	payload := parseRsyncStat(string(out), "Total transferred file size:")
	fileCount := parseRsyncStat(string(out), "Number of regular files transferred:")
	original, _ := dirSize(localDir)

	fmt.Fprintln(os.Stderr)
	if original > 0 {
		fmt.Fprintf(os.Stderr, "  Files dir (unfiltered):  %s\n", formatBytesShort(original))
	}
	fmt.Fprintf(os.Stderr, "  Payload after filters:   %s (%s files)\n", formatBytesShort(payload), formatCount(fileCount))
	if original > payload {
		saved := original - payload
		fmt.Fprintf(os.Stderr, "  Excluded by filters:     %s (%d%%)\n", formatBytesShort(saved), saved*100/original)
	}
	return payload, nil
}

// parseRsyncStat extracts an integer value from a line of rsync --stats output.
func parseRsyncStat(out, prefix string) int64 {
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, prefix) {
			continue
		}
		v := strings.TrimSpace(strings.TrimPrefix(line, prefix))
		v = strings.ReplaceAll(strings.Fields(v)[0], ",", "")
		n, err := strconv.ParseInt(v, 10, 64)
		if err == nil {
			return n
		}
	}
	return 0
}

// formatCount renders an integer with thousands separators (e.g. 82,464).
func formatCount(n int64) string {
	s := strconv.FormatInt(n, 10)
	for i := len(s) - 3; i > 0; i -= 3 {
		s = s[:i] + "," + s[i:]
	}
	return s
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
	args = append(args, "--human-readable", "--info=progress2",
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
	previewPushDBCmd.Flags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	previewPushCmd.AddCommand(previewPushDBCmd)
	previewPushCmd.AddCommand(previewPushFilesCmd)
	previewCmd.AddCommand(previewPushCmd)
}
