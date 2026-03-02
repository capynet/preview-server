package cmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
)

var stripHeavyFiles string
var autoYes bool

var pushCmd = &cobra.Command{
	Use:   "push",
	Short: "Push base files to the preview server",
	Long:  "Upload base database or files from your local project to the preview server.",
}

var pushDBCmd = &cobra.Command{
	Use:   "db [file.sql.gz]",
	Short: "Export and upload the base database",
	Long: `Export the database using ddev drush sql-dump and upload it as the base
database for previews.

If a file path is given, upload that file instead of generating a dump.
The project is detected automatically from the git remote in the current directory.`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		slug, err := detectProjectSlug()
		if err != nil {
			return err
		}

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
		if !confirm(fmt.Sprintf("Do you want to %s base database for %q?", action, slug)) {
			fmt.Fprintln(os.Stderr, "Aborted.")
			return nil
		}

		// If a file was provided, upload it directly
		if len(args) == 1 {
			return uploadExistingFile(slug, "db", args[0])
		}

		// Generate dump with ddev drush sql-dump
		return generateAndUploadDB(slug)
	},
}

var pushFilesCmd = &cobra.Command{
	Use:   "files [file.tar.gz]",
	Short: "Package and upload the base files",
	Long: `Package the Drupal files directory and upload it as the base files archive
for previews.

If a file path is given, upload that file instead of packaging.
The project is detected automatically from the git remote in the current directory.`,
	Args: cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		slug, err := detectProjectSlug()
		if err != nil {
			return err
		}

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
		if !confirm(fmt.Sprintf("Do you want to %s base files archive for %q?", action, slug)) {
			fmt.Fprintln(os.Stderr, "Aborted.")
			return nil
		}

		if len(args) == 1 {
			return uploadExistingFile(slug, "files", args[0])
		}

		return generateAndUploadFiles(slug)
	},
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

	fmt.Fprintf(os.Stderr, "Detected project: %s\n", slug)
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

	if err := apiClient.UploadBaseFileChunked(slug, kind, f, filepath.Base(filePath)); err != nil {
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

// getDrupalFilesDir uses ddev drush status to detect the public files directory.
// Returns a path relative to the project root (e.g. "docroot/sites/default/files").
func getDrupalFilesDir() (string, error) {
	out, err := exec.Command("ddev", "drush", "status", "--format=json").Output()
	if err != nil {
		return "", fmt.Errorf("failed to run ddev drush status: %w", err)
	}

	var status map[string]interface{}
	if err := json.Unmarshal(out, &status); err != nil {
		return "", fmt.Errorf("failed to parse drush status: %w", err)
	}

	// "root" is the Drupal root inside the container, e.g. "/var/www/html/docroot"
	// "files" is relative to root, e.g. "sites/default/files"
	root, _ := status["root"].(string)
	files, _ := status["files"].(string)

	if files == "" {
		return "", fmt.Errorf("drush status did not return a files path")
	}

	// Extract the docroot relative to /var/www/html (DDEV mount point)
	// e.g. "/var/www/html/docroot" -> "docroot", "/var/www/html" -> ""
	docroot := ""
	ddevMount := "/var/www/html"
	if root != "" && strings.HasPrefix(root, ddevMount) {
		docroot = strings.TrimPrefix(root, ddevMount)
		docroot = strings.TrimPrefix(docroot, "/")
	}

	// Build the local path: docroot + files
	var filesDir string
	if docroot != "" {
		filesDir = filepath.Join(docroot, files)
	} else {
		filesDir = files
	}

	return filesDir, nil
}

func generateAndUploadDB(slug string) error {
	fmt.Fprintln(os.Stderr, "Generating database dump via ddev drush sql-dump...")

	// Ensure ddev is running before piping stdout, so startup messages
	// don't get mixed into the SQL dump
	if err := ensureDdevRunning(); err != nil {
		return err
	}

	// Create a pipe: drush sql-dump | pigz/gzip -> upload
	drush := exec.Command("ddev", "drush", "sql-dump")
	drush.Stderr = os.Stderr

	drushOut, err := drush.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create pipe: %w", err)
	}

	// Use pigz if available, else gzip. Level 6 for good balance.
	var compressor *exec.Cmd
	compressorName := "gzip"
	if hasPigz() {
		compressorName = "pigz"
		compressor = exec.Command("pigz", "-6", "-c")
	} else {
		compressor = exec.Command("gzip", "-6", "-c")
	}
	compressor.Stdin = drushOut
	compressor.Stderr = os.Stderr

	compressedOut, err := compressor.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create %s pipe: %w", compressorName, err)
	}

	if err := drush.Start(); err != nil {
		return fmt.Errorf("failed to start drush: %w", err)
	}
	if err := compressor.Start(); err != nil {
		return fmt.Errorf("failed to start %s: %w", compressorName, err)
	}

	fmt.Fprintf(os.Stderr, "Uploading database dump (compressor: %s -6)...\n", compressorName)

	filename := fmt.Sprintf("%s-base.sql.gz", slug)
	if err := apiClient.UploadBaseFileChunked(slug, "db", compressedOut, filename); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}

	if err := compressor.Wait(); err != nil {
		return fmt.Errorf("%s failed: %w", compressorName, err)
	}
	if err := drush.Wait(); err != nil {
		return fmt.Errorf("drush sql-dump failed: %w", err)
	}

	fmt.Fprintf(os.Stderr, "Done! Base database for %q updated.\n", slug)
	return nil
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
	filesDir, err := getDrupalFilesDir()
	if err != nil {
		return fmt.Errorf("could not detect files directory: %w", err)
	}
	if _, err := os.Stat(filesDir); os.IsNotExist(err) {
		return fmt.Errorf("files directory %q not found — are you in the project root?", filesDir)
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

	// Build tar args (no compression — piped to external compressor)
	tarArgs := []string{"cf", "-", "--exclude=./css", "--exclude=./js", "--exclude=./php"}

	// If --strip-heavy-files is set, exclude large files
	if stripHeavyFiles != "" {
		maxBytes, err := parseSizeMB(stripHeavyFiles)
		if err != nil {
			return err
		}

		findCmd := exec.Command("find", ".", "-type", "f", "-size", fmt.Sprintf("+%dc", maxBytes),
			"-not", "-path", "./css/*", "-not", "-path", "./js/*", "-not", "-path", "./php/*")
		findCmd.Dir = filesDir
		findOut, err := findCmd.Output()
		if err != nil {
			return fmt.Errorf("find failed: %w", err)
		}

		heavyFiles := strings.Split(strings.TrimSpace(string(findOut)), "\n")
		skipped := 0
		for _, f := range heavyFiles {
			f = strings.TrimSpace(f)
			if f == "" {
				continue
			}
			tarArgs = append(tarArgs, "--exclude="+f)
			skipped++
		}
		if skipped > 0 {
			fmt.Fprintf(os.Stderr, "Skipping %d files larger than %s\n", skipped, stripHeavyFiles)
		}
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
	if err := apiClient.UploadBaseFileChunked(slug, "files", compressedOut, filename); err != nil {
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
	pushCmd.PersistentFlags().BoolVarP(&autoYes, "yes", "y", false, "Skip confirmation prompts")
	pushFilesCmd.Flags().StringVar(&stripHeavyFiles, "strip-heavy-files", "", "Exclude files larger than this size, e.g. --strip-heavy-files 10mb")
	pushCmd.AddCommand(pushDBCmd)
	pushCmd.AddCommand(pushFilesCmd)
	rootCmd.AddCommand(pushCmd)
}
