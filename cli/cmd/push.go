package cmd

import (
	"bufio"
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

func generateAndUploadDB(slug string) error {
	fmt.Fprintln(os.Stderr, "Generating database dump via ddev drush sql-dump...")

	// Create a pipe: drush sql-dump | gzip -> upload
	drush := exec.Command("ddev", "drush", "sql-dump")
	drush.Stderr = os.Stderr

	drushOut, err := drush.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create pipe: %w", err)
	}

	gzipCmd := exec.Command("gzip", "-c")
	gzipCmd.Stdin = drushOut
	gzipCmd.Stderr = os.Stderr

	gzipOut, err := gzipCmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create gzip pipe: %w", err)
	}

	if err := drush.Start(); err != nil {
		return fmt.Errorf("failed to start drush: %w", err)
	}
	if err := gzipCmd.Start(); err != nil {
		return fmt.Errorf("failed to start gzip: %w", err)
	}

	fmt.Fprintln(os.Stderr, "Uploading database dump...")

	filename := fmt.Sprintf("%s-base.sql.gz", slug)
	if err := apiClient.UploadBaseFileChunked(slug, "db", gzipOut, filename); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}

	if err := gzipCmd.Wait(); err != nil {
		return fmt.Errorf("gzip failed: %w", err)
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

func generateAndUploadFiles(slug string) error {
	// Determine the files directory
	filesDir := "web/sites/default/files"
	if _, err := os.Stat(filesDir); os.IsNotExist(err) {
		return fmt.Errorf("files directory %q not found — are you in the project root?", filesDir)
	}

	// Build tar args
	tarArgs := []string{"czf", "-", "--exclude=./css", "--exclude=./js", "--exclude=./php"}

	// If --strip-heavy-files is set, use find to generate file list excluding large files
	var findCmd *exec.Cmd
	if stripHeavyFiles != "" {
		maxBytes, err := parseSizeMB(stripHeavyFiles)
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "Packaging %s (excluding files > %s)...\n", filesDir, stripHeavyFiles)

		// Use find to list files under the size limit, then pass to tar via --files-from
		// find outputs paths relative to filesDir
		findCmd = exec.Command("find", ".", "-type", "f", "-size", fmt.Sprintf("+%dc", maxBytes),
			"-not", "-path", "./css/*", "-not", "-path", "./js/*", "-not", "-path", "./php/*")
		findCmd.Dir = filesDir
		findOut, err := findCmd.Output()
		if err != nil {
			return fmt.Errorf("find failed: %w", err)
		}

		// Build --exclude for each heavy file
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
	} else {
		fmt.Fprintf(os.Stderr, "Packaging %s...\n", filesDir)
	}

	tarArgs = append(tarArgs, "-C", filesDir, ".")
	tarCmd := exec.Command("tar", tarArgs...)
	tarCmd.Stderr = os.Stderr

	tarOut, err := tarCmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create pipe: %w", err)
	}

	if err := tarCmd.Start(); err != nil {
		return fmt.Errorf("failed to start tar: %w", err)
	}

	fmt.Fprintln(os.Stderr, "Uploading files archive...")

	filename := fmt.Sprintf("%s-files.tar.gz", slug)
	if err := apiClient.UploadBaseFileChunked(slug, "files", tarOut, filename); err != nil {
		return fmt.Errorf("upload failed: %w", err)
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
