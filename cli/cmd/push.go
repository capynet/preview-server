package cmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// progressReader wraps a reader and prints progress to stderr as the stream is
// consumed. When total > 0 it shows a percentage; the byte count is the raw
// (uncompressed) dump, so it can be compared against the size estimate. The
// SSH pipe's backpressure makes the count track the remote import closely.
type progressReader struct {
	r        io.Reader
	total    int64  // estimated total bytes; 0 = unknown
	verb     string // progress label; defaults to "Imported"
	read     int64
	start    time.Time
	lastShow time.Time
}

func (p *progressReader) label() string {
	if p.verb != "" {
		return p.verb
	}
	return "Imported"
}

func (p *progressReader) Read(b []byte) (int, error) {
	if p.start.IsZero() {
		p.start = time.Now()
	}
	n, err := p.r.Read(b)
	p.read += int64(n)
	if now := time.Now(); now.Sub(p.lastShow) > 500*time.Millisecond {
		p.lastShow = now
		elapsed := now.Sub(p.start).Seconds()
		var speed int64
		if elapsed > 0 {
			speed = int64(float64(p.read) / elapsed)
		}
		if p.total > 0 {
			pct := p.read * 100 / p.total
			if pct > 99 {
				pct = 99 // estimate; never claim 100% until done
			}
			fmt.Fprintf(os.Stderr, "\r  %s: %s / ~%s (%d%%, %s/s)        ",
				p.label(), formatBytesShort(p.read), formatBytesShort(p.total), pct, formatBytesShort(speed))
		} else {
			fmt.Fprintf(os.Stderr, "\r  %s: %s (%s/s)        ", p.label(), formatBytesShort(p.read), formatBytesShort(speed))
		}
	}
	return n, err
}

func (p *progressReader) finish() {
	if p.read > 0 {
		fmt.Fprintf(os.Stderr, "\r  %s: %s total.        \n", p.label(), formatBytesShort(p.read))
	}
}

var stripHeavyFiles string
var noImageStyles bool
var autoYes bool

// newPushCmd builds the "push" command tree (push db / push files),
// mounted under "druploy project push".
func newPushCmd() *cobra.Command {
	pushCmd := &cobra.Command{
		Use:   "push",
		Short: "Push base DB or files used to seed every new preview",
		Long: `Upload the base database or files archive from your local project.

These are project-level resources: every new preview of the project is
seeded from them.`,
	}

	pushDBCmd := &cobra.Command{
		Use:   "db [file.sql.gz]",
		Short: "Export and upload the base database",
		Long: `Export the database using mariadb-dump/mysqldump (via ddev) and upload it as the base
database for previews. Cache tables (cache_*) are excluded automatically.

If a file path is given, upload that file instead of generating a dump.
The project is detected automatically from the git remote in the current directory.`,
		Args: cobra.MaximumNArgs(1),
		RunE: runPushDB,
	}

	pushFilesCmd := &cobra.Command{
		Use:   "files [file.tar.gz]",
		Short: "Package and upload the base files",
		Long: `Package the Drupal files directory and upload it as the base files archive
for previews.

If a file path is given, upload that file instead of packaging.
The project is detected automatically from the git remote in the current directory.`,
		Args: cobra.MaximumNArgs(1),
		RunE: runPushFiles,
	}

	pushCmd.PersistentFlags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	pushFilesCmd.Flags().StringVar(&stripHeavyFiles, "strip-heavy-files", "", "Exclude files larger than this size, e.g. --strip-heavy-files 10mb")
	pushFilesCmd.Flags().BoolVar(&noImageStyles, "no-image-styles", false, "Exclude Drupal image styles (styles/ directory) — they regenerate on demand")

	pushCmd.AddCommand(pushDBCmd)
	pushCmd.AddCommand(pushFilesCmd)
	return pushCmd
}

func runPushDB(cmd *cobra.Command, args []string) error {
	slug, err := detectProjectSlug()
	if err != nil {
		return err
	}

	if err := resolveOrgForProject(slug); err != nil {
		return err
	}
	announceProject(slug, true)

	// Check current status on the server
	status, err := apiClient.GetBaseFilesStatus(slug)
	if err != nil {
		return fmt.Errorf("failed to check base files status: %w", err)
	}

	if status.DB != nil && status.DB.Exists {
		fmt.Fprintf(os.Stderr, "A base database already exists for project %q (%d bytes).\n", slug, status.DB.SizeBytes)
	} else {
		fmt.Fprintf(os.Stderr, "No base database exists yet for project %q.\n", slug)
	}

	action := "overwrite the existing"
	if status.DB == nil || !status.DB.Exists {
		action = "upload a new"
	}
	if !confirm(fmt.Sprintf("Do you want to %s base database for %q? Every new preview will be seeded from it.", action, slug)) {
		fmt.Fprintln(os.Stderr, "Aborted.")
		return nil
	}

	// If a file was provided, upload it directly
	if len(args) == 1 {
		return uploadExistingFile(slug, "db", args[0])
	}

	// Generate dump with ddev drush sql-dump
	return generateAndUploadDB(slug)
}

func runPushFiles(cmd *cobra.Command, args []string) error {
	slug, err := detectProjectSlug()
	if err != nil {
		return err
	}

	if err := resolveOrgForProject(slug); err != nil {
		return err
	}
	announceProject(slug, true)

	status, err := apiClient.GetBaseFilesStatus(slug)
	if err != nil {
		return fmt.Errorf("failed to check base files status: %w", err)
	}

	if status.Files != nil && status.Files.Exists {
		fmt.Fprintf(os.Stderr, "A base files archive already exists for project %q (%d bytes).\n", slug, status.Files.SizeBytes)
	} else {
		fmt.Fprintf(os.Stderr, "No base files archive exists yet for project %q.\n", slug)
	}

	action := "overwrite the existing"
	if status.Files == nil || !status.Files.Exists {
		action = "upload a new"
	}
	if !confirm(fmt.Sprintf("Do you want to %s base files archive for %q? Every new preview will be seeded from it.", action, slug)) {
		fmt.Fprintln(os.Stderr, "Aborted.")
		return nil
	}

	if len(args) == 1 {
		return uploadExistingFile(slug, "files", args[0])
	}

	return generateAndUploadFiles(slug)
}

// detectProjectSlug reads the git remote "origin" URL in the current directory
// and extracts the last path segment as the project slug.
// e.g. git@gitlab.com:preview-tests/drupal-test.git -> "drupal-test"
// e.g. https://gitlab.com/preview-tests/drupal-test -> "drupal-test"
func detectProjectSlug() (string, error) {
	out, err := exec.Command("git", "remote", "get-url", "origin").Output()
	if err != nil {
		return "", fmt.Errorf("could not detect git remote: %w\nMake sure you are in a git repository with an 'origin' remote", err)
	}

	remote := strings.TrimSpace(string(out))

	// Remove .git suffix
	remote = strings.TrimSuffix(remote, ".git")

	// Extract last path segment
	parts := strings.Split(remote, "/")
	slug := parts[len(parts)-1]

	// Also handle SSH git@... format with colons
	if idx := strings.LastIndex(slug, ":"); idx >= 0 {
		slug = slug[idx+1:]
	}

	if slug == "" {
		return "", fmt.Errorf("could not determine project slug from remote %q", remote)
	}

	return slug, nil
}

func confirm(prompt string) bool {
	if autoYes {
		return true
	}
	fmt.Fprintf(os.Stderr, "%s [Y/n] ", prompt)
	scanner := bufio.NewScanner(os.Stdin)
	if scanner.Scan() {
		answer := strings.TrimSpace(strings.ToLower(scanner.Text()))
		return answer != "n" && answer != "no"
	}
	return true
}

func uploadExistingFile(slug, kind, filePath string) error {
	f, err := os.Open(filePath)
	if err != nil {
		return fmt.Errorf("cannot open file: %w", err)
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return fmt.Errorf("cannot stat file: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Uploading %s (%d bytes)...\n", filePath, info.Size())

	if err := apiClient.UploadBaseFileChunked(slug, kind, f, filepath.Base(filePath), 0); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Done! Base %s for %q updated.\n", kind, slug)
	return nil
}

func ensureDdevRunning() error {
	// Check if ddev is already running by checking container status
	cmd := exec.Command("ddev", "describe", "-j")
	out, err := cmd.Output()
	if err == nil && strings.Contains(string(out), `"running"`) {
		return nil
	}

	// Start ddev, sending all output to stderr so it doesn't pollute pipes
	fmt.Fprintln(os.Stderr, "Starting ddev...")
	start := exec.Command("ddev", "start")
	start.Stdout = os.Stderr
	start.Stderr = os.Stderr
	if err := start.Run(); err != nil {
		return fmt.Errorf("failed to start ddev: %w", err)
	}
	return nil
}

// getDrupalDBCredentials returns the database credentials from Drupal's configuration
// by running ddev drush sql:conf --format=json.
type dbCredentials struct {
	Host     string
	Database string
	Username string
	Password string
}

func getDrupalDBCredentials() (*dbCredentials, error) {
	out, err := exec.Command("ddev", "drush", "sql:conf", "--format=json").Output()
	if err != nil {
		return nil, fmt.Errorf("failed to run ddev drush sql:conf: %w", err)
	}

	var conf map[string]interface{}
	if err := json.Unmarshal(out, &conf); err != nil {
		return nil, fmt.Errorf("failed to parse drush sql:conf output: %w", err)
	}

	getString := func(key string) string {
		if v, ok := conf[key].(string); ok {
			return v
		}
		return ""
	}

	creds := &dbCredentials{
		Host:     getString("host"),
		Database: getString("database"),
		Username: getString("username"),
		Password: getString("password"),
	}

	if creds.Database == "" {
		return nil, fmt.Errorf("drush sql:conf did not return a database name")
	}
	if creds.Username == "" {
		creds.Username = "root"
	}

	return creds, nil
}

// ddevApproot returns the absolute path to the ddev project root (as ddev itself
// resolves it, regardless of which subdirectory the CLI was invoked from).
func ddevApproot() (string, error) {
	out, err := exec.Command("ddev", "describe", "-j").Output()
	if err != nil {
		return "", fmt.Errorf("failed to run ddev describe: %w", err)
	}

	var result struct {
		Raw struct {
			Approot string `json:"approot"`
		} `json:"raw"`
	}
	if err := json.Unmarshal(out, &result); err != nil {
		return "", fmt.Errorf("failed to parse ddev describe output: %w", err)
	}
	if result.Raw.Approot == "" {
		return "", fmt.Errorf("ddev describe did not return an approot")
	}
	return result.Raw.Approot, nil
}

// getDrupalFilesDir uses ddev drush status to detect the public files directory.
// Returns the absolute path to the files directory on the host (e.g.
// "/home/user/project/docroot/sites/default/files") and the path relative to the
// Drupal root (e.g. "sites/default/files"). The absolute path is anchored to the
// ddev project root, not the CLI's current working directory, so this works when
// invoked from any subdirectory of the project.
func getDrupalFilesDir() (string, string, error) {
	out, err := exec.Command("ddev", "drush", "status", "--format=json").Output()
	if err != nil {
		return "", "", fmt.Errorf("failed to run ddev drush status: %w", err)
	}

	// Strip any non-JSON output (PHP warnings, deprecation notices, etc.)
	// by finding the first '{' in the output.
	raw := string(out)
	idx := strings.Index(raw, "{")
	if idx < 0 {
		return "", "", fmt.Errorf("drush status returned no JSON (output: %s)", raw[:min(len(raw), 200)])
	}
	jsonBytes := []byte(raw[idx:])

	var status map[string]interface{}
	if err := json.Unmarshal(jsonBytes, &status); err != nil {
		return "", "", fmt.Errorf("failed to parse drush status: %w", err)
	}

	// "root" is the Drupal root inside the container, e.g. "/var/www/html/docroot"
	// "files" is relative to root, e.g. "sites/default/files"
	root, _ := status["root"].(string)
	files, _ := status["files"].(string)

	if files == "" {
		return "", "", fmt.Errorf("drush status did not return a files path")
	}

	// Extract the docroot relative to /var/www/html (DDEV mount point)
	// e.g. "/var/www/html/docroot" -> "docroot", "/var/www/html" -> ""
	docroot := ""
	ddevMount := "/var/www/html"
	if root != "" && strings.HasPrefix(root, ddevMount) {
		docroot = strings.TrimPrefix(root, ddevMount)
		docroot = strings.TrimPrefix(docroot, "/")
	}

	approot, err := ddevApproot()
	if err != nil {
		return "", "", err
	}

	// Build the absolute local path: approot + docroot + files
	filesDir := filepath.Join(approot, docroot, files)

	return filesDir, files, nil
}

func generateAndUploadDB(slug string) error {
	fmt.Fprintln(os.Stderr, "Generating database dump...")

	// Ensure ddev is running before piping stdout, so startup messages
	// don't get mixed into the SQL dump
	if err := ensureDdevRunning(); err != nil {
		return err
	}

	compressedOut, compressorName, wait, err := startLocalDBDump(0, true)
	if err != nil {
		return err
	}

	fmt.Fprintf(os.Stderr, "Uploading database dump (compressor: %s -6)...\n", compressorName)

	filename := fmt.Sprintf("%s-base.sql.gz", slug)
	if err := apiClient.UploadBaseFileChunked(slug, "db", compressedOut, filename, 0); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}

	if err := wait(); err != nil {
		return err
	}

	fmt.Fprintf(os.Stderr, "Done! Base database for %q updated.\n", slug)
	return nil
}

// getLocalDBSizeEstimate returns the approximate uncompressed size (in bytes)
// of the data the dump will contain: data_length of non-cache tables. Cache
// tables are dumped structure-only, so their data is excluded. Returns 0 if
// the size can't be determined (progress then falls back to bytes-only).
func getLocalDBSizeEstimate() int64 {
	creds, err := getDrupalDBCredentials()
	if err != nil {
		return 0
	}
	shell := fmt.Sprintf(
		"MYSQL_PWD=root mysql -uroot -N -D %s -e \"SELECT COALESCE(SUM(data_length),0) FROM information_schema.tables WHERE table_schema='%s' AND table_name NOT LIKE 'cache_%%'\"",
		creds.Database, creds.Database,
	)
	out, err := exec.Command("ddev", "exec", "-s", "db", "bash", "-c", shell).Output()
	if err != nil {
		return 0
	}
	n, _ := strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
	return n
}

// startLocalDBDump starts the ddev dump + gzip pipeline and returns the
// compressed stream, the compressor name, and a wait func that must be called
// after the stream has been fully consumed.
//
// If progressTotal > 0, live progress is printed against the dump stream with
// progressTotal as the estimated total. Pass 0 to disable progress printing.
//
// If compress is true the stream is gzip/pigz-compressed (for upload+storage);
// if false the raw SQL stream is returned (for a direct remote import, where
// transfer size is never the bottleneck).
func startLocalDBDump(progressTotal int64, compress bool) (io.Reader, string, func() error, error) {
	// Get database credentials from Drupal's configuration
	creds, err := getDrupalDBCredentials()
	if err != nil {
		return nil, "", nil, fmt.Errorf("could not detect database credentials: %w", err)
	}

	// In DDEV, the db container always has root/root access.
	// Use root to avoid issues with empty passwords from drush sql:conf.
	dbUser := "root"
	dbPass := "root"

	// Query cache_* tables so we can dump their structure without data.
	// This avoids dumping large cache data that gets rebuilt by drush cr,
	// while keeping the CREATE TABLE statements so MySQL doesn't end up
	// with orphaned tablespaces on import.
	listCmd := exec.Command("ddev", "exec", "-s", "db",
		"bash", "-c", fmt.Sprintf("MYSQL_PWD=%s mysql -u%s -N -D %s -e \"SHOW TABLES LIKE 'cache_%%'\"",
			dbPass, dbUser, creds.Database))
	cacheTablesOut, err := listCmd.Output()
	if err != nil {
		fmt.Fprintln(os.Stderr, "Warning: could not list cache tables, proceeding without exclusions")
	}

	var cacheTables []string
	if cacheTablesOut != nil {
		for _, t := range strings.Split(strings.TrimSpace(string(cacheTablesOut)), "\n") {
			t = strings.TrimSpace(t)
			if t != "" {
				cacheTables = append(cacheTables, t)
			}
		}
	}

	if len(cacheTables) > 0 {
		fmt.Fprintf(os.Stderr, "Dumping %d cache tables as structure-only (no data)\n", len(cacheTables))
	}

	// Two dump passes concatenated into a single stream:
	// 1. Full dump minus cache tables (structure + data)
	// 2. Cache tables structure only (--no-data)
	// Use mariadb-dump if available (avoids deprecation warning), else mysqldump
	mysqlAuth := fmt.Sprintf("-u%s", dbUser)
	mysqlEnv := fmt.Sprintf("MYSQL_PWD=%s", dbPass)
	baseFlags := fmt.Sprintf("--single-transaction --quick --no-tablespaces")
	dumpBin := "$(command -v mariadb-dump || command -v mysqldump)"

	var ignoreArgs string
	for _, t := range cacheTables {
		ignoreArgs += fmt.Sprintf(" --ignore-table=%s.%s", creds.Database, t)
	}

	shellCmd := fmt.Sprintf(
		"%s %s %s %s%s %s",
		mysqlEnv, dumpBin, mysqlAuth, baseFlags, ignoreArgs, creds.Database,
	)
	if len(cacheTables) > 0 {
		shellCmd += fmt.Sprintf(
			" && %s %s %s %s --no-data %s %s",
			mysqlEnv, dumpBin, mysqlAuth, baseFlags, creds.Database, strings.Join(cacheTables, " "),
		)
	}

	dumper := exec.Command("ddev", "exec", "-s", "db", "bash", "-c", shellCmd)
	dumper.Stderr = os.Stderr

	dumperOut, err := dumper.StdoutPipe()
	if err != nil {
		return nil, "", nil, fmt.Errorf("failed to create pipe: %w", err)
	}

	// Count progress on the dump stream. Used by the uncompressed path
	// (preview push db), where sent bytes match the size estimate directly.
	var progress *progressReader
	var dumpStream io.Reader = dumperOut
	if progressTotal > 0 {
		progress = &progressReader{r: dumperOut, total: progressTotal}
		dumpStream = progress
	}

	// Uncompressed path: stream raw SQL, no compressor stage. Used when the
	// dump is piped straight into a remote import over SSH — compression would
	// only burn CPU (the transfer size is never the bottleneck) and would make
	// the progress count compressed bytes, which can't be compared to the size
	// estimate.
	if !compress {
		if err := dumper.Start(); err != nil {
			return nil, "", nil, fmt.Errorf("failed to start database dump: %w", err)
		}
		wait := func() error {
			if err := dumper.Wait(); err != nil {
				return fmt.Errorf("database dump failed: %w", err)
			}
			if progress != nil {
				progress.finish()
			}
			return nil
		}
		return dumpStream, "uncompressed", wait, nil
	}

	// Compressed path: pipe through pigz (or gzip). Used by project push db,
	// which uploads the dump to be stored and re-downloaded per preview.
	var compressor *exec.Cmd
	compressorName := "gzip"
	if hasPigz() {
		compressorName = "pigz"
		compressor = exec.Command("pigz", "-6", "-c")
	} else {
		compressor = exec.Command("gzip", "-6", "-c")
	}
	compressor.Stdin = dumpStream
	compressor.Stderr = os.Stderr

	compressedOut, err := compressor.StdoutPipe()
	if err != nil {
		return nil, "", nil, fmt.Errorf("failed to create %s pipe: %w", compressorName, err)
	}

	if err := dumper.Start(); err != nil {
		return nil, "", nil, fmt.Errorf("failed to start database dump: %w", err)
	}
	if err := compressor.Start(); err != nil {
		return nil, "", nil, fmt.Errorf("failed to start %s: %w", compressorName, err)
	}

	wait := func() error {
		if err := compressor.Wait(); err != nil {
			return fmt.Errorf("%s failed: %w", compressorName, err)
		}
		if err := dumper.Wait(); err != nil {
			return fmt.Errorf("database dump failed: %w", err)
		}
		if progress != nil {
			progress.finish()
		}
		return nil
	}

	return compressedOut, compressorName, wait, nil
}

// parseSizeMB parses a size string like "10mb", "5MB", "10" into bytes.
// Accepts formats: "10mb", "10MB", "10" (assumed MB).
func parseSizeMB(s string) (int64, error) {
	s = strings.TrimSpace(strings.ToLower(s))
	s = strings.TrimSuffix(s, "mb")
	s = strings.TrimSpace(s)
	mb, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid size %q: expected format like '10mb' or '10'", s)
	}
	return int64(mb * 1024 * 1024), nil
}

// hasPigz checks if pigz is available in PATH.
func hasPigz() bool {
	_, err := exec.LookPath("pigz")
	return err == nil
}

// dirSize returns the total size in bytes of a directory using du -sb.
func dirSize(path string) (int64, error) {
	out, err := exec.Command("du", "-sb", path).Output()
	if err != nil {
		return 0, err
	}
	fields := strings.Fields(string(out))
	if len(fields) == 0 {
		return 0, fmt.Errorf("unexpected du output")
	}
	return strconv.ParseInt(fields[0], 10, 64)
}

// formatBytesShort formats bytes as a human-readable string (e.g. "1.2 GB").
func formatBytesShort(b int64) string {
	switch {
	case b >= 1024*1024*1024:
		return fmt.Sprintf("%.1f GB", float64(b)/(1024*1024*1024))
	case b >= 1024*1024:
		return fmt.Sprintf("%.1f MB", float64(b)/(1024*1024))
	case b >= 1024:
		return fmt.Sprintf("%.1f KB", float64(b)/1024)
	default:
		return fmt.Sprintf("%d B", b)
	}
}

func generateAndUploadFiles(slug string) error {
	// Ensure ddev is running so we can query drush
	if err := ensureDdevRunning(); err != nil {
		return err
	}

	// Detect files directory via drush status
	filesDir, _, err := getDrupalFilesDir()
	if err != nil {
		return fmt.Errorf("could not detect files directory: %w", err)
	}
	if _, err := os.Stat(filesDir); os.IsNotExist(err) {
		return fmt.Errorf("files directory %q not found", filesDir)
	}

	// Calculate source size
	sourceSize, _ := dirSize(filesDir)
	if sourceSize > 0 {
		fmt.Fprintf(os.Stderr, "Source: %s (%s)\n", filesDir, formatBytesShort(sourceSize))
	}

	// Determine compressor: pigz if available, else gzip
	// Level 6 = good compression/speed balance (gzip default is 6, but being explicit)
	usePigz := hasPigz()
	compressorName := "gzip"
	var compressorCmd *exec.Cmd
	if usePigz {
		compressorName = "pigz"
		compressorCmd = exec.Command("pigz", "-6", "-c")
	} else {
		compressorCmd = exec.Command("gzip", "-6", "-c")
		// Show hint for large packages (>500MB uncompressed)
		if sourceSize > 500*1024*1024 {
			fmt.Fprintln(os.Stderr, "HINT: Install pigz to speed up compression using multiple cores: sudo apt install pigz")
		}
	}

	// Always-excluded dirs
	excludeDirs := []string{"css", "js", "php"}
	if noImageStyles {
		excludeDirs = append(excludeDirs, "styles")
		if stylesSize, err := dirSize(filepath.Join(filesDir, "styles")); err == nil && stylesSize > 0 {
			fmt.Fprintf(os.Stderr, "Excluding image styles (%s) — Drupal will regenerate them on demand\n", formatBytesShort(stylesSize))
		}
	}

	// Subtract excluded dirs from sourceSize
	for _, excl := range excludeDirs {
		if exclSize, err := dirSize(filepath.Join(filesDir, excl)); err == nil {
			sourceSize -= exclSize
		}
	}

	// Build tar args (no compression — piped to external compressor)
	tarArgs := []string{"cf", "-"}
	for _, excl := range excludeDirs {
		tarArgs = append(tarArgs, "--exclude=./"+excl)
	}

	// If --strip-heavy-files is set, write excludes to a temp file to avoid
	// "argument list too long" when there are thousands of large files.
	var excludeFile *os.File
	if stripHeavyFiles != "" {
		maxBytes, err := parseSizeMB(stripHeavyFiles)
		if err != nil {
			return err
		}

		findArgs := []string{".", "-type", "f", "-size", fmt.Sprintf("+%dc", maxBytes)}
		for _, excl := range excludeDirs {
			findArgs = append(findArgs, "-not", "-path", "./"+excl+"/*")
		}
		findCmd := exec.Command("find", findArgs...)
		findCmd.Dir = filesDir
		findOut, err := findCmd.Output()
		if err != nil {
			return fmt.Errorf("find failed: %w", err)
		}

		heavyFiles := strings.Split(strings.TrimSpace(string(findOut)), "\n")
		skipped := 0
		var strippedBytes int64
		excludeFile, err = os.CreateTemp("", "druploy-exclude-*.txt")
		if err != nil {
			return fmt.Errorf("failed to create exclude file: %w", err)
		}
		defer os.Remove(excludeFile.Name())
		for _, f := range heavyFiles {
			f = strings.TrimSpace(f)
			if f == "" {
				continue
			}
			fmt.Fprintln(excludeFile, f)
			skipped++
			if info, err := os.Stat(filepath.Join(filesDir, f)); err == nil {
				strippedBytes += info.Size()
			}
		}
		excludeFile.Close()
		if skipped > 0 {
			tarArgs = append(tarArgs, "--exclude-from="+excludeFile.Name())
			sourceSize -= strippedBytes
			fmt.Fprintf(os.Stderr, "Skipping %d files larger than %s (%s excluded)\n", skipped, stripHeavyFiles, formatBytesShort(strippedBytes))
		}
	}

	if sourceSize < 0 {
		sourceSize = 0
	}

	fmt.Fprintf(os.Stderr, "Packaging %s (compressor: %s -6)...\n", filesDir, compressorName)

	tarArgs = append(tarArgs, "-C", filesDir, ".")
	tarCmd := exec.Command("tar", tarArgs...)
	tarCmd.Stderr = os.Stderr

	// Pipe: tar -> compressor -> upload
	tarOut, err := tarCmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create tar pipe: %w", err)
	}

	compressorCmd.Stdin = tarOut
	compressorCmd.Stderr = os.Stderr

	compressedOut, err := compressorCmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create compressor pipe: %w", err)
	}

	if err := tarCmd.Start(); err != nil {
		return fmt.Errorf("failed to start tar: %w", err)
	}
	if err := compressorCmd.Start(); err != nil {
		return fmt.Errorf("failed to start %s: %w", compressorName, err)
	}

	fmt.Fprintln(os.Stderr, "Uploading files archive...")

	filename := fmt.Sprintf("%s-files.tar.gz", slug)
	if err := apiClient.UploadBaseFileChunked(slug, "files", compressedOut, filename, sourceSize); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}

	if err := compressorCmd.Wait(); err != nil {
		return fmt.Errorf("%s failed: %w", compressorName, err)
	}
	if err := tarCmd.Wait(); err != nil {
		return fmt.Errorf("tar failed: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Done! Base files for %q updated.\n", slug)
	return nil
}

func init() {
	projectCmd.AddCommand(newPushCmd())
}
