package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// GitClone clones or updates a repository.
// Uses proxyURL for HTTP_PROXY if set.
func GitClone(repoPath, cloneURL, branch, commitSHA, proxyURL string, log func(string)) error {
	gitDir := filepath.Join(repoPath, ".git")

	env := os.Environ()
	// Ensure HOME is set (systemd services may not have it)
	// so git can find ~/.gitconfig for safe.directory
	hasHome := false
	for _, e := range env {
		if strings.HasPrefix(e, "HOME=") {
			hasHome = true
			break
		}
	}
	if !hasHome {
		env = append(env, "HOME=/root")
	}
	if proxyURL != "" {
		env = append(env, "HTTP_PROXY="+proxyURL, "HTTPS_PROXY="+proxyURL)
	}

	if info, err := os.Stat(gitDir); err == nil && info.IsDir() {
		// Incremental update
		log("Fetching latest changes...\n")

		// Mark directory as safe (agent runs as root, repo owned by www-data)
		safeCmd := exec.Command("git", "config", "--global", "--add", "safe.directory", repoPath)
		safeCmd.Env = env
		safeCmd.Run()

		// Update remote URL (token may have changed)
		if err := runGit(repoPath, env, "remote", "set-url", "origin", cloneURL); err != nil {
			// Fallback: fresh clone
			log("Remote update failed, doing fresh clone...\n")
			os.RemoveAll(repoPath)
			return freshClone(repoPath, cloneURL, branch, env, log)
		}

		if err := runGit(repoPath, env, "fetch", "--depth", "1", "origin", branch); err != nil {
			log("Fetch failed, doing fresh clone...\n")
			os.RemoveAll(repoPath)
			return freshClone(repoPath, cloneURL, branch, env, log)
		}

		if err := runGit(repoPath, env, "reset", "--hard", "FETCH_HEAD"); err != nil {
			return fmt.Errorf("git reset failed: %w", err)
		}

		log("Repository updated.\n")
	} else {
		// Fresh clone
		return freshClone(repoPath, cloneURL, branch, env, log)
	}

	return nil
}

func freshClone(repoPath, cloneURL, branch string, env []string, log func(string)) error {
	log("Cloning repository...\n")

	os.RemoveAll(repoPath)
	os.MkdirAll(repoPath, 0755)

	cmd := exec.Command("git", "clone", "--depth", "1", "--branch", branch, cloneURL, repoPath)
	cmd.Env = env
	output, err := cmd.CombinedOutput()
	if err != nil {
		// Sanitize output to not leak tokens
		sanitized := sanitizeGitOutput(string(output))
		return fmt.Errorf("git clone failed: %s", sanitized)
	}

	log("Repository cloned.\n")
	return nil
}

func runGit(repoPath string, env []string, args ...string) error {
	cmd := exec.Command("git", append([]string{"-C", repoPath}, args...)...)
	cmd.Env = env
	output, err := cmd.CombinedOutput()
	if err != nil {
		sanitized := sanitizeGitOutput(string(output))
		return fmt.Errorf("git %s: %s", args[0], sanitized)
	}
	return nil
}

// sanitizeGitOutput removes OAuth tokens from git error messages.
func sanitizeGitOutput(output string) string {
	// Replace oauth2:TOKEN@ with oauth2:***@
	if i := strings.Index(output, "oauth2:"); i >= 0 {
		if j := strings.Index(output[i:], "@"); j >= 0 {
			output = output[:i] + "oauth2:***" + output[i+j:]
		}
	}
	return output
}
