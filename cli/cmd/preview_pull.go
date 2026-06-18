package cmd

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
)

var previewPullCmd = &cobra.Command{
	Use:   "pull",
	Short: "Pull data from a preview to your local machine",
	Long: `Pull data from a single preview environment down to your local machine.

The inverse of "druploy preview push": these commands read from the target
preview without modifying it.`,
}

var previewPullDBCmd = &cobra.Command{
	Use:   "db [PROJECT/PREVIEW-NAME] [FILE]",
	Short: "Replace your local database with the preview's one (or download it to a file)",
	Long: `Dump the preview's database (cache tables structure-only) and import it
straight into your local ddev database, or save it to a local file.

Without FILE, your local database is dropped completely and the preview's
dump is streamed straight into it, followed by a local drush cr. Asks for
confirmation first (skip with -y).

With FILE, the dump is saved there instead and your local database is not
touched. Relative or absolute paths are accepted (the file must end in .sql
or .gz); if it ends in .gz the dump is compressed on the fly.

The preview is never modified.

Examples:
  druploy preview pull db
  druploy preview pull db -y
  druploy preview pull db dumps/staging.sql.gz
  druploy preview pull db my-site/mr-1597 foo.sql`,
	Args: cobra.MaximumNArgs(2),
	RunE: runPreviewPullDB,
}

// classifyPullArgs splits the positional args into the optional target
// preview and the optional output file. An arg ending in .sql or .gz is the
// output file; anything else is the PROJECT/PREVIEW-NAME target.
func classifyPullArgs(args []string) (targetArgs []string, outFile string, err error) {
	for _, a := range args {
		lower := strings.ToLower(a)
		if strings.HasSuffix(lower, ".sql") || strings.HasSuffix(lower, ".gz") {
			if outFile != "" {
				return nil, "", fmt.Errorf("two output files given (%q and %q)", outFile, a)
			}
			outFile = a
			continue
		}
		if len(targetArgs) > 0 {
			return nil, "", fmt.Errorf("two previews given (%q and %q) — the output file must end in .sql or .gz", targetArgs[0], a)
		}
		targetArgs = append(targetArgs, a)
	}
	return targetArgs, outFile, nil
}

// remoteDBCredentials holds the connection info of the preview's database,
// parsed from the mysql command line that drush sql:connect prints.
type remoteDBCredentials struct {
	User     string
	Password string
	Host     string
	Port     string
	Database string
}

// shQuote single-quotes s for safe interpolation into a shell command.
func shQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// sshCapture runs a command on the preview's PHP container and returns its
// stdout.
func sshCapture(sshBin string, r *resolvedPreview, remoteCmd string) (string, error) {
	c := exec.Command(sshBin,
		"-p", "2222",
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=/dev/null",
		"-o", "LogLevel=ERROR",
		fmt.Sprintf("preview@%s", r.VmIP),
		remoteCmd,
	)
	c.Stderr = os.Stderr
	out, err := c.Output()
	return string(out), err
}

// getRemoteDBCredentials asks drush on the preview for its sql:connect
// command line and parses the mysql flags out of it.
func getRemoteDBCredentials(sshBin string, r *resolvedPreview) (*remoteDBCredentials, error) {
	out, err := sshCapture(sshBin, r, "cd /var/www/html && vendor/bin/drush sql:connect 2>/dev/null")
	if err != nil {
		return nil, fmt.Errorf("could not get database credentials from the preview: %w", err)
	}
	return parseSQLConnect(out)
}

// parseSQLConnect parses a mysql command line as printed by drush
// sql:connect, e.g.:
//
//	mysql --user=drupal --password=drupal --database=drupal --host=db --port=3306 -A
//
// Both --opt=value and attached short forms (-udrupal) are handled.
func parseSQLConnect(out string) (*remoteDBCredentials, error) {
	creds := &remoteDBCredentials{}
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Fields(strings.TrimSpace(line))
		if len(fields) == 0 || !strings.HasSuffix(fields[0], "mysql") {
			continue
		}
		for _, f := range fields[1:] {
			switch {
			case strings.HasPrefix(f, "--user="):
				creds.User = strings.TrimPrefix(f, "--user=")
			case strings.HasPrefix(f, "--password="):
				creds.Password = strings.TrimPrefix(f, "--password=")
			case strings.HasPrefix(f, "--host="):
				creds.Host = strings.TrimPrefix(f, "--host=")
			case strings.HasPrefix(f, "--port="):
				creds.Port = strings.TrimPrefix(f, "--port=")
			case strings.HasPrefix(f, "--database="):
				creds.Database = strings.TrimPrefix(f, "--database=")
			case strings.HasPrefix(f, "-u") && len(f) > 2:
				creds.User = f[2:]
			case strings.HasPrefix(f, "-p") && len(f) > 2:
				creds.Password = f[2:]
			case strings.HasPrefix(f, "-h") && len(f) > 2:
				creds.Host = f[2:]
			case strings.HasPrefix(f, "-P") && len(f) > 2:
				creds.Port = f[2:]
			case strings.HasPrefix(f, "-"):
				// other flags (-A, etc.) — ignore
			case creds.Database == "":
				creds.Database = f
			}
		}
		break
	}
	if creds.Database == "" || creds.User == "" {
		return nil, fmt.Errorf("could not parse drush sql:connect output: %q", strings.TrimSpace(out))
	}
	return creds, nil
}

// mysqlConnFlags renders the connection flags shared by the mysql client and
// mysqldump invocations on the preview.
func (c *remoteDBCredentials) mysqlConnFlags() string {
	flags := fmt.Sprintf("-u%s", c.User)
	if c.Host != "" {
		flags += fmt.Sprintf(" -h%s", c.Host)
	}
	if c.Port != "" {
		flags += fmt.Sprintf(" -P%s", c.Port)
	}
	return flags
}

// getRemoteDBInfo returns the size estimate of the data the dump will
// contain (non-cache tables) and the list of cache_* tables, in a single
// remote round-trip. On failure it returns 0 and no tables, which degrades
// to bytes-only progress and a dump that includes cache data.
func getRemoteDBInfo(sshBin string, r *resolvedPreview, creds *remoteDBCredentials) (int64, []string) {
	query := fmt.Sprintf(
		"SELECT COALESCE(SUM(data_length),0) FROM information_schema.tables WHERE table_schema='%s' AND table_name NOT LIKE 'cache_%%'; SHOW TABLES LIKE 'cache_%%'",
		creds.Database,
	)
	remoteCmd := fmt.Sprintf(
		"cd /var/www/html && MYSQL_PWD=%s mysql %s -N -D %s -e \"%s\"",
		shQuote(creds.Password), creds.mysqlConnFlags(), creds.Database, query,
	)
	out, err := sshCapture(sshBin, r, remoteCmd)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Warning: could not inspect the remote database, proceeding without size estimate or cache exclusions")
		return 0, nil
	}

	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) == 0 {
		return 0, nil
	}
	estimate, _ := strconv.ParseInt(strings.TrimSpace(lines[0]), 10, 64)
	var cacheTables []string
	for _, t := range lines[1:] {
		t = strings.TrimSpace(t)
		if t != "" {
			cacheTables = append(cacheTables, t)
		}
	}
	return estimate, cacheTables
}

// startRemoteDBDump starts the mysqldump on the preview and returns the raw
// SQL stream plus a wait func that must be called after the stream has been
// fully consumed.
//
// We bypass `drush sql:dump` deliberately: its output would flow through
// PHP's process handling, with the same kind of heavy per-stream overhead
// that made `drush sql:cli` ~5x slower on stdin imports. Instead the
// credentials parsed from sql:connect drive a direct mysqldump whose stdout
// streams straight through the SSH pipe. Same two-pass strategy as the local
// dump: full dump minus cache tables, then cache tables structure-only.
func startRemoteDBDump(sshBin string, r *resolvedPreview, creds *remoteDBCredentials, cacheTables []string) (io.Reader, func() error, error) {
	baseFlags := "--single-transaction --quick --no-tablespaces"
	var ignoreArgs string
	for _, t := range cacheTables {
		ignoreArgs += fmt.Sprintf(" --ignore-table=%s.%s", creds.Database, t)
	}

	dumpCmd := fmt.Sprintf("\"$DBIN\" %s %s%s %s",
		creds.mysqlConnFlags(), baseFlags, ignoreArgs, creds.Database)
	if len(cacheTables) > 0 {
		dumpCmd += fmt.Sprintf(" && \"$DBIN\" %s %s --no-data %s %s",
			creds.mysqlConnFlags(), baseFlags, creds.Database, strings.Join(cacheTables, " "))
	}
	remoteCmd := fmt.Sprintf(
		"cd /var/www/html && export MYSQL_PWD=%s && DBIN=\"$(command -v mariadb-dump || command -v mysqldump)\" && %s",
		shQuote(creds.Password), dumpCmd,
	)

	c := exec.Command(sshBin,
		"-p", "2222",
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=/dev/null",
		"-o", "LogLevel=ERROR",
		// Keep the session alive across slow, quiet stretches of a big dump
		// so an idle TCP path doesn't drop the connection mid-transfer.
		"-o", "ServerAliveInterval=30",
		"-o", "ServerAliveCountMax=20",
		fmt.Sprintf("preview@%s", r.VmIP),
		remoteCmd,
	)
	c.Stderr = os.Stderr
	out, err := c.StdoutPipe()
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create pipe: %w", err)
	}
	if err := c.Start(); err != nil {
		return nil, nil, fmt.Errorf("failed to start remote dump: %w", err)
	}
	wait := func() error {
		if err := c.Wait(); err != nil {
			return fmt.Errorf("remote database dump failed: %w", err)
		}
		return nil
	}
	return out, wait, nil
}

func runPreviewPullDB(cmd *cobra.Command, args []string) error {
	sshBin, err := findSSHBinary()
	if err != nil {
		return err
	}

	targetArgs, outFile, err := classifyPullArgs(args)
	if err != nil {
		return err
	}

	r, _, err := resolvePreviewPushTarget(targetArgs)
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

	if outFile != "" {
		return pullDBToFile(sshBin, r, outFile)
	}
	return pullDBImport(sshBin, r)
}

// pullDBToFile downloads the preview's database to a local .sql (or .sql.gz)
// file.
func pullDBToFile(sshBin string, r *resolvedPreview, outFile string) error {
	if _, err := os.Stat(outFile); err == nil {
		if !confirm(fmt.Sprintf("File %q already exists. Overwrite it?", outFile)) {
			fmt.Fprintln(os.Stderr, "Aborted.")
			return nil
		}
	}
	compress := strings.HasSuffix(strings.ToLower(outFile), ".gz")

	creds, err := getRemoteDBCredentials(sshBin, r)
	if err != nil {
		return err
	}
	estimate, cacheTables := getRemoteDBInfo(sshBin, r, creds)
	if len(cacheTables) > 0 {
		fmt.Fprintf(os.Stderr, "Dumping %d cache tables as structure-only (no data)\n", len(cacheTables))
	}

	dump, wait, err := startRemoteDBDump(sshBin, r, creds, cacheTables)
	if err != nil {
		return err
	}
	progress := &progressReader{r: dump, total: estimate, verb: "Downloaded"}

	fmt.Fprintf(os.Stderr, "Downloading database of %s/%s to %s:\n", r.Project, r.PreviewName, outFile)

	// Write to a .part file and rename on success, so an interrupted
	// download never leaves a truncated dump under the final name.
	partFile := outFile + ".part"
	f, err := os.Create(partFile)
	if err != nil {
		return fmt.Errorf("cannot create %s: %w", partFile, err)
	}
	cleanup := func() {
		f.Close()
		os.Remove(partFile)
	}

	if compress {
		compressor := exec.Command("gzip", "-6", "-c")
		if hasPigz() {
			compressor = exec.Command("pigz", "-6", "-c")
		}
		compressor.Stdin = progress
		compressor.Stdout = f
		compressor.Stderr = os.Stderr
		if err := compressor.Run(); err != nil {
			cleanup()
			return fmt.Errorf("compression failed: %w", err)
		}
	} else {
		if _, err := io.Copy(f, progress); err != nil {
			cleanup()
			return fmt.Errorf("download failed: %w", err)
		}
	}
	if err := wait(); err != nil {
		cleanup()
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(partFile)
		return fmt.Errorf("failed to write %s: %w", partFile, err)
	}
	progress.finish()
	if err := os.Rename(partFile, outFile); err != nil {
		os.Remove(partFile)
		return fmt.Errorf("failed to move dump into place: %w", err)
	}

	if info, err := os.Stat(outFile); err == nil {
		fmt.Fprintf(os.Stderr, "Done! Saved %s (%s).\n", outFile, formatBytesShort(info.Size()))
	} else {
		fmt.Fprintf(os.Stderr, "Done! Saved %s.\n", outFile)
	}
	return nil
}

// pullDBImport streams the preview's database straight into the local ddev
// database, replacing it completely.
func pullDBImport(sshBin string, r *resolvedPreview) error {
	if err := ensureDdevRunning(); err != nil {
		return err
	}
	localCreds, err := getDrupalDBCredentials()
	if err != nil {
		return fmt.Errorf("could not detect local database credentials: %w", err)
	}

	if !confirm(fmt.Sprintf("REPLACE your local database (%s) with the database of preview %s/%s? Your current local database will be dropped completely first.", localCreds.Database, r.Project, r.PreviewName)) {
		fmt.Fprintln(os.Stderr, "Aborted.")
		return nil
	}

	creds, err := getRemoteDBCredentials(sshBin, r)
	if err != nil {
		return err
	}
	estimate, cacheTables := getRemoteDBInfo(sshBin, r, creds)
	if len(cacheTables) > 0 {
		fmt.Fprintf(os.Stderr, "Dumping %d cache tables as structure-only (no data)\n", len(cacheTables))
	}

	dump, wait, err := startRemoteDBDump(sshBin, r, creds, cacheTables)
	if err != nil {
		return err
	}
	progress := &progressReader{r: dump, total: estimate}

	fmt.Fprintf(os.Stderr, "Importing %s/%s into your local database. Large databases take a few minutes:\n", r.Project, r.PreviewName)

	// Drop and recreate the local database before importing, so no tables
	// from the previous content survive, then stream the dump into mysql.
	// Both run inside one bash -c so stdin is consumed only by the import.
	shell := fmt.Sprintf(
		"MYSQL_PWD=root mysql -uroot -e 'DROP DATABASE IF EXISTS %s; CREATE DATABASE %s' && MYSQL_PWD=root mysql -uroot -D %s",
		localCreds.Database, localCreds.Database, localCreds.Database,
	)
	importer := exec.Command("ddev", "exec", "-s", "db", "bash", "-c", shell)
	importer.Stdin = progress
	importer.Stdout = os.Stderr
	importer.Stderr = os.Stderr
	if err := importer.Run(); err != nil {
		return fmt.Errorf("local database import failed: %w", err)
	}
	if err := wait(); err != nil {
		return err
	}
	progress.finish()

	fmt.Fprintln(os.Stderr, "Rebuilding caches (drush cr)...")
	cr := exec.Command("ddev", "drush", "cr")
	cr.Stdout = os.Stderr
	cr.Stderr = os.Stderr
	if err := cr.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "Warning: drush cr failed — the database was imported, run it manually if needed.")
	}

	fmt.Fprintf(os.Stderr, "Done! Local database replaced with %s/%s.\n", r.Project, r.PreviewName)
	return nil
}

var previewPullReplace bool

var previewPullFilesCmd = &cobra.Command{
	Use:   "files [PROJECT/PREVIEW-NAME]",
	Short: "Rsync the preview's Drupal files dir down to your local one",
	Long: `Sync the preview's Drupal files directory down to your local files dir via
rsync over SSH.

By default the sync is additive: the preview's files are added or updated
locally, nothing is deleted. With --replace, your local files dir is wiped
completely first, so it ends up containing exactly what the preview has.

The same exclusions as "druploy preview push files" apply: css/, js/ and
php/ are always excluded; styles/ with --no-image-styles; big files with
--strip-heavy-files.

The preview is never modified. Asks for confirmation (skip with -y).

Examples:
  druploy preview pull files
  druploy preview pull files --no-image-styles --strip-heavy-files 5mb
  druploy preview pull files --replace my-site/mr-1597`,
	Args: cobra.MaximumNArgs(1),
	RunE: runPreviewPullFiles,
}

func runPreviewPullFiles(cmd *cobra.Command, args []string) error {
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

	r, _, err := resolvePreviewPushTarget(args)
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

	// Make sure SSH access works (key registered + injected into the preview)
	if err := ensureSSHKeyRegistered(); err != nil {
		return err
	}
	if err := ensureSSHKeyOnPreview(r); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: could not inject SSH key: %v\n", err)
	}

	msg := fmt.Sprintf("Sync the files of preview %s/%s into your local files dir (%s)? Local files are added or updated, nothing is deleted.", r.Project, r.PreviewName, localDir)
	if previewPullReplace {
		msg = fmt.Sprintf("WIPE your local files dir (%s) completely and download the content of preview %s/%s? Nothing currently in it will be kept.", localDir, r.Project, r.PreviewName)
	}
	if !confirm(msg) {
		fmt.Fprintln(os.Stderr, "Aborted.")
		return nil
	}

	if previewPullReplace {
		// Paranoia guard: never wipe a path that doesn't look like a Drupal
		// files dir.
		if localDir == "" || !strings.Contains(localDir, "files") {
			return fmt.Errorf("refusing to wipe suspicious local files path %q", localDir)
		}
		fmt.Fprintf(os.Stderr, "Wiping %s locally...\n", localDir)
		entries, err := os.ReadDir(localDir)
		if err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("could not read local files dir: %w", err)
		}
		for _, e := range entries {
			if err := os.RemoveAll(filepath.Join(localDir, e.Name())); err != nil {
				return fmt.Errorf("could not wipe local files dir: %w", err)
			}
		}
	}
	if err := os.MkdirAll(localDir, 0775); err != nil {
		return fmt.Errorf("could not create local files dir: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Syncing preview: %s → local: %s...\n", remoteDir, localDir)
	sshCmd := "ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
	rsyncArgs := []string{"-rlptz", "-e", sshCmd}
	rsyncArgs = append(rsyncArgs, previewRsyncFilterArgs()...)
	rsyncArgs = append(rsyncArgs, "--human-readable", "--info=progress2",
		fmt.Sprintf("preview@%s:%s/", r.VmIP, strings.TrimSuffix(remoteDir, "/")),
		strings.TrimSuffix(localDir, "/")+"/",
	)
	c := exec.Command(rsyncBin, rsyncArgs...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Run(); err != nil {
		return fmt.Errorf("sync failed: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Done! Files of %s/%s synced to %s.\n", r.Project, r.PreviewName, localDir)
	return nil
}

func init() {
	previewPullDBCmd.Flags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	previewPullFilesCmd.Flags().BoolVar(&previewPullReplace, "replace", false, "Wipe your local files dir completely before downloading")
	previewPullFilesCmd.Flags().BoolVar(&noImageStyles, "no-image-styles", false, "Exclude Drupal image styles (styles/ directory) — they regenerate on demand")
	previewPullFilesCmd.Flags().StringVar(&stripHeavyFiles, "strip-heavy-files", "", "Exclude files larger than this size, e.g. --strip-heavy-files 10mb")
	previewPullFilesCmd.Flags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	previewPullCmd.AddCommand(previewPullDBCmd)
	previewPullCmd.AddCommand(previewPullFilesCmd)
	previewCmd.AddCommand(previewPullCmd)
}
