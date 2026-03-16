package cmd

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// resolvedPreview holds cached preview connection info.
type resolvedPreview struct {
	OrgSlug       string `json:"org_slug"`
	Project       string `json:"project"`
	PreviewName   string `json:"preview_name"`
	VmIP          string `json:"vm_ip"`
	JumpHost      string `json:"jump_host"`
	Status        string `json:"status"`
	Branch        string `json:"branch"`
}

// cacheKey returns a unique key based on the git repo root + branch.
func cacheKey() (string, error) {
	root, err := exec.Command("git", "rev-parse", "--show-toplevel").Output()
	if err != nil {
		return "", err
	}
	branch, err := detectGitBranch()
	if err != nil {
		return "", err
	}
	h := sha256.Sum256([]byte(strings.TrimSpace(string(root)) + ":" + branch))
	return fmt.Sprintf("%x", h[:8]), nil
}

func cachePath() (string, error) {
	key, err := cacheKey()
	if err != nil {
		return "", err
	}
	return filepath.Join(os.TempDir(), fmt.Sprintf("preview-resolve-%s.json", key)), nil
}

// loadCachedResolve loads cached preview info. Returns nil if not found.
func loadCachedResolve() *resolvedPreview {
	path, err := cachePath()
	if err != nil {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var r resolvedPreview
	if err := json.Unmarshal(data, &r); err != nil {
		return nil
	}
	return &r
}

// saveCachedResolve saves preview info to cache.
func saveCachedResolve(r *resolvedPreview) {
	path, err := cachePath()
	if err != nil {
		return
	}
	data, _ := json.Marshal(r)
	os.WriteFile(path, data, 0600)
}

// clearCachedResolve removes the cache for the current repo+branch.
func clearCachedResolve() {
	path, err := cachePath()
	if err != nil {
		return
	}
	os.Remove(path)
}

// resolvePreviewFull does the full resolution: project detection, org, branch, preview lookup, VM IP.
// Returns a resolvedPreview or error. Shows no output on success.
func resolvePreviewFull() (*resolvedPreview, error) {
	slug, err := detectProjectSlug()
	if err != nil {
		return nil, err
	}

	if err := resolveOrgForProject(slug); err != nil {
		return nil, err
	}

	branch, err := detectGitBranch()
	if err != nil {
		return nil, err
	}

	preview, err := findPreviewByBranch(slug, branch)
	if err != nil {
		return nil, fmt.Errorf("no preview found for branch %q in project %q", branch, slug)
	}

	detail, err := apiClient.GetPreview(slug, preview.Name)
	if err != nil {
		return nil, fmt.Errorf("failed to get preview details: %w", err)
	}

	if err := checkPreviewReady(detail, slug, preview.Name); err != nil {
		return nil, err
	}

	cfg := loadConfig()
	jumpHost, err := resolveSSHJumpHost(cfg.APIURL)
	if err != nil {
		return nil, fmt.Errorf("could not determine SSH jump host: %w", err)
	}

	r := &resolvedPreview{
		OrgSlug:     apiClient.Org,
		Project:     slug,
		PreviewName: preview.Name,
		VmIP:        detail.VmIP,
		JumpHost:    jumpHost,
		Status:      detail.Status,
		Branch:      branch,
	}

	saveCachedResolve(r)
	return r, nil
}

// resolveExplicitPreview resolves a "project/preview-name" string directly (no git detection).
func resolveExplicitPreview(arg string) (*resolvedPreview, error) {
	project, previewName, err := parsePreviewName(arg)
	if err != nil {
		return nil, err
	}

	if err := resolveOrgForProject(project); err != nil {
		return nil, err
	}

	detail, err := apiClient.GetPreview(project, previewName)
	if err != nil {
		return nil, fmt.Errorf("failed to get preview details: %w", err)
	}

	if err := checkPreviewReady(detail, project, previewName); err != nil {
		return nil, err
	}

	cfg := loadConfig()
	jumpHost, err := resolveSSHJumpHost(cfg.APIURL)
	if err != nil {
		return nil, fmt.Errorf("could not determine SSH jump host: %w", err)
	}

	return &resolvedPreview{
		OrgSlug:     apiClient.Org,
		Project:     project,
		PreviewName: previewName,
		VmIP:        detail.VmIP,
		JumpHost:    jumpHost,
		Status:      detail.Status,
		Branch:      detail.Branch,
	}, nil
}

// resolvePreview returns cached info or does a full resolve.
// If the cache exists, returns immediately with no output.
func resolvePreview() (*resolvedPreview, error) {
	if cached := loadCachedResolve(); cached != nil {
		// Set org on apiClient for any subsequent API calls
		if apiClient != nil && apiClient.Org == "" {
			apiClient.Org = cached.OrgSlug
		}
		return cached, nil
	}
	return resolvePreviewFull()
}

// resolvePreviewWithRetry tries cached, on command failure clears cache and re-resolves.
// Returns (resolved, needsRetry, error).
// Usage pattern:
//
//	r, err := resolvePreview()
//	if err != nil { return err }
//	exitCode := runCommand(r)
//	if exitCode != 0 {
//	    r, err = retryResolve()
//	    if err != nil { return err }
//	    exitCode = runCommand(r)
//	}
func retryResolve() (*resolvedPreview, error) {
	clearCachedResolve()
	fmt.Fprintf(os.Stderr, "Refreshing preview info...\n")
	return resolvePreviewFull()
}
