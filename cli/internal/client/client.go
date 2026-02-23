package client

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"strings"
	"time"
)

// ErrNotAuthenticated is returned when the server rejects the token.
var ErrNotAuthenticated = fmt.Errorf("authentication failed")

type Client struct {
	BaseURL    string
	Token      string
	HTTPClient *http.Client
}

type ActionResult struct {
	Success     bool   `json:"success"`
	Output      string `json:"output"`
	Error       string `json:"error"`
	PipelineID  int    `json:"pipeline_id,omitempty"`
	PipelineURL string `json:"pipeline_url,omitempty"`
}

type PreviewListResult struct {
	Previews []Preview `json:"previews"`
	Total    int       `json:"total"`
}

type Preview struct {
	Name           string  `json:"name"`
	Project        string  `json:"project"`
	MrID           int     `json:"mr_id"`
	Status         string  `json:"status"`
	URL            string  `json:"url"`
	Branch         string  `json:"branch"`
	CommitSHA      string  `json:"commit_sha"`
	LastDeployedAt *string `json:"last_deployed_at"`
	BasicAuthUser  *string `json:"basic_auth_user"`
	BasicAuthPass  *string `json:"basic_auth_pass"`
}

func New(baseURL, token string) *Client {
	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		Token:      token,
		HTTPClient: &http.Client{},
	}
}

func (c *Client) doRequest(method, url string, body io.Reader) (*http.Response, error) {
	req, err := http.NewRequest(method, url, body)
	if err != nil {
		return nil, err
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	if method == "POST" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == 401 {
		resp.Body.Close()
		fmt.Fprintln(os.Stderr, "Authentication failed. Your token may be expired or revoked.")
		fmt.Fprintln(os.Stderr, "Re-authenticate by running:\n")
		fmt.Fprintln(os.Stderr, "  preview login\n")
		os.Exit(1)
	}
	return resp, nil
}

func (c *Client) ListPreviews(includeStatus bool) (*PreviewListResult, error) {
	statusParam := "true"
	if !includeStatus {
		statusParam = "false"
	}
	url := fmt.Sprintf("%s/api/previews?status=%s", c.BaseURL, statusParam)

	resp, err := c.doRequest("GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	var result PreviewListResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode error: %w", err)
	}
	return &result, nil
}

func (c *Client) PostAction(project string, mrID int, action string) (*ActionResult, error) {
	url := fmt.Sprintf("%s/api/previews/%s/mr-%d/%s", c.BaseURL, project, mrID, action)

	resp, err := c.doRequest("POST", url, nil)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 404 {
		return nil, fmt.Errorf("preview %s/mr-%d not found", project, mrID)
	}

	var result ActionResult
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("decode error: %w", err)
	}
	return &result, nil
}

func (c *Client) PostDrush(project string, mrID int, args string) (*ActionResult, error) {
	url := fmt.Sprintf("%s/api/previews/%s/mr-%d/drush", c.BaseURL, project, mrID)

	payload := fmt.Sprintf(`{"args": %q}`, args)
	resp, err := c.doRequest("POST", url, strings.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 404 {
		return nil, fmt.Errorf("preview %s/mr-%d not found", project, mrID)
	}

	var result ActionResult
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("decode error: %w", err)
	}
	return &result, nil
}

type BaseFileInfo struct {
	Exists    bool   `json:"exists"`
	SizeBytes int64  `json:"size_bytes"`
	ModifiedAt string `json:"modified_at"`
}

type BaseFilesStatus struct {
	DB    *BaseFileInfo `json:"db"`
	Files *BaseFileInfo `json:"files"`
}

func (c *Client) GetBaseFilesStatus(slug string) (*BaseFilesStatus, error) {
	url := fmt.Sprintf("%s/api/projects/%s/base-files", c.BaseURL, slug)

	resp, err := c.doRequest("GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	var result BaseFilesStatus
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode error: %w", err)
	}
	return &result, nil
}

func (c *Client) UploadBaseFile(slug, kind string, reader io.Reader, filename string) error {
	url := fmt.Sprintf("%s/api/projects/%s/base-files/%s", c.BaseURL, slug, kind)

	pr, pw := io.Pipe()
	writer := multipart.NewWriter(pw)

	go func() {
		part, err := writer.CreateFormFile("file", filename)
		if err != nil {
			pw.CloseWithError(err)
			return
		}
		if _, err := io.Copy(part, reader); err != nil {
			pw.CloseWithError(err)
			return
		}
		writer.Close()
		pw.Close()
	}()

	req, err := http.NewRequest("POST", url, pr)
	if err != nil {
		return err
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 401 {
		fmt.Fprintln(os.Stderr, "Authentication failed. Your token may be expired or revoked.")
		fmt.Fprintln(os.Stderr, "Re-authenticate by running:\n")
		fmt.Fprintln(os.Stderr, "  preview login\n")
		os.Exit(1)
	}
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return nil
}

const chunkSize = 50 * 1024 * 1024 // 50MB

// UploadBaseFileChunked copies the reader to a temp file, then uploads using
// single request (if <50MB) or chunked upload (if >=50MB) with a progress bar.
func (c *Client) UploadBaseFileChunked(slug, kind string, reader io.Reader, filename string) error {
	// 1. Copy stream to temp file to know size and allow chunking
	tmpFile, err := os.CreateTemp("", "preview-upload-*")
	if err != nil {
		return fmt.Errorf("failed to create temp file: %w", err)
	}
	tmpPath := tmpFile.Name()
	defer os.Remove(tmpPath)

	fmt.Fprintf(os.Stderr, "Buffering to temp file...\r")
	written, err := io.Copy(tmpFile, reader)
	if err != nil {
		tmpFile.Close()
		return fmt.Errorf("failed to buffer upload: %w", err)
	}
	tmpFile.Close()
	fmt.Fprintf(os.Stderr, "Buffered %s to temp file.  \n", formatBytes(written))

	// 2. Decide: single or chunked
	if written < chunkSize {
		return c.uploadSingleWithProgress(slug, kind, tmpPath, filename, written)
	}
	return c.uploadChunked(slug, kind, tmpPath, filename, written)
}

func (c *Client) uploadSingleWithProgress(slug, kind, filePath, filename string, totalSize int64) error {
	f, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer f.Close()

	pr, pw := io.Pipe()
	writer := multipart.NewWriter(pw)

	go func() {
		part, err := writer.CreateFormFile("file", filename)
		if err != nil {
			pw.CloseWithError(err)
			return
		}
		progressReader := &progressWriter{total: totalSize, label: "Uploading"}
		if _, err := io.Copy(part, io.TeeReader(f, progressReader)); err != nil {
			pw.CloseWithError(err)
			return
		}
		fmt.Fprintln(os.Stderr)
		writer.Close()
		pw.Close()
	}()

	req, err := http.NewRequest("POST", fmt.Sprintf("%s/api/projects/%s/base-files/%s", c.BaseURL, slug, kind), pr)
	if err != nil {
		return err
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 401 {
		fmt.Fprintln(os.Stderr, "Authentication failed. Re-authenticate by running:\n\n  preview login\n")
		os.Exit(1)
	}
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return nil
}

func (c *Client) uploadChunked(slug, kind, filePath, filename string, totalSize int64) error {
	totalChunks := int((totalSize + chunkSize - 1) / chunkSize)

	// Init
	initBody, _ := json.Marshal(map[string]interface{}{
		"total_chunks": totalChunks,
		"total_size":   totalSize,
	})
	resp, err := c.doRequest("POST",
		fmt.Sprintf("%s/api/projects/%s/base-files/%s/upload/init", c.BaseURL, slug, kind),
		bytes.NewReader(initBody))
	if err != nil {
		return fmt.Errorf("chunked init failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("chunked init HTTP %d: %s", resp.StatusCode, string(body))
	}
	var initResult struct {
		UploadID string `json:"upload_id"`
	}
	json.NewDecoder(resp.Body).Decode(&initResult)
	resp.Body.Close()

	fmt.Fprintf(os.Stderr, "Uploading %s in %d chunks of %s...\n", formatBytes(totalSize), totalChunks, formatBytes(chunkSize))

	// Upload chunks
	f, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer f.Close()

	var totalSent int64
	buf := make([]byte, chunkSize)

	for i := 0; i < totalChunks; i++ {
		n, err := io.ReadFull(f, buf)
		if err != nil && err != io.ErrUnexpectedEOF && err != io.EOF {
			return fmt.Errorf("read chunk %d: %w", i, err)
		}
		chunkData := buf[:n]

		// Retry logic per chunk
		var uploadErr error
		for attempt := 0; attempt < 3; attempt++ {
			if attempt > 0 {
				wait := time.Duration(1<<uint(attempt)) * 2 * time.Second
				fmt.Fprintf(os.Stderr, "  Retrying chunk %d/%d in %v...\n", i+1, totalChunks, wait)
				time.Sleep(wait)
			}

			uploadErr = c.uploadOneChunk(slug, kind, initResult.UploadID, i, chunkData)
			if uploadErr == nil {
				break
			}
		}
		if uploadErr != nil {
			return fmt.Errorf("chunk %d failed after 3 attempts: %w", i, uploadErr)
		}

		totalSent += int64(n)
		pct := float64(totalSent) / float64(totalSize) * 100
		bar := progressBar(pct, 30)
		fmt.Fprintf(os.Stderr, "\r  %s / %s (%.0f%%) %s", formatBytes(totalSent), formatBytes(totalSize), pct, bar)
	}
	fmt.Fprintln(os.Stderr)

	// Complete
	fmt.Fprintf(os.Stderr, "Finalizing upload...\n")
	completeBody, _ := json.Marshal(map[string]string{"upload_id": initResult.UploadID})
	resp2, err := c.doRequest("POST",
		fmt.Sprintf("%s/api/projects/%s/base-files/%s/upload/complete", c.BaseURL, slug, kind),
		bytes.NewReader(completeBody))
	if err != nil {
		return fmt.Errorf("chunked complete failed: %w", err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != 200 {
		body, _ := io.ReadAll(resp2.Body)
		return fmt.Errorf("chunked complete HTTP %d: %s", resp2.StatusCode, string(body))
	}

	return nil
}

func (c *Client) uploadOneChunk(slug, kind, uploadID string, index int, data []byte) error {
	pr, pw := io.Pipe()
	writer := multipart.NewWriter(pw)

	go func() {
		writer.WriteField("upload_id", uploadID)
		writer.WriteField("chunk_index", fmt.Sprintf("%d", index))
		part, err := writer.CreateFormFile("file", fmt.Sprintf("chunk_%d", index))
		if err != nil {
			pw.CloseWithError(err)
			return
		}
		part.Write(data)
		writer.Close()
		pw.Close()
	}()

	req, err := http.NewRequest("POST",
		fmt.Sprintf("%s/api/projects/%s/base-files/%s/upload/chunk", c.BaseURL, slug, kind),
		pr)
	if err != nil {
		return err
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return nil
}

// progressWriter counts bytes written and prints a progress bar to stderr.
type progressWriter struct {
	total   int64
	written int64
	label   string
}

func (pw *progressWriter) Write(p []byte) (int, error) {
	pw.written += int64(len(p))
	pct := float64(pw.written) / float64(pw.total) * 100
	bar := progressBar(pct, 30)
	fmt.Fprintf(os.Stderr, "\r%s... %s / %s (%.0f%%) %s",
		pw.label, formatBytes(pw.written), formatBytes(pw.total), pct, bar)
	return len(p), nil
}

func progressBar(pct float64, width int) string {
	filled := int(pct / 100 * float64(width))
	if filled > width {
		filled = width
	}
	return "[" + strings.Repeat("█", filled) + strings.Repeat("░", width-filled) + "]"
}

func formatBytes(b int64) string {
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

func (c *Client) DownloadStream(project string, mrID int, kind string, w io.Writer) error {
	url := fmt.Sprintf("%s/api/previews/%s/mr-%d/%s/download", c.BaseURL, project, mrID, kind)

	resp, err := c.doRequest("GET", url, nil)
	if err != nil {
		return fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	_, err = io.Copy(w, resp.Body)
	return err
}
