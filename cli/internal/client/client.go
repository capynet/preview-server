package client

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

// ErrNotAuthenticated is returned when the server rejects the token.
var ErrNotAuthenticated = fmt.Errorf("authentication failed")

type Client struct {
	BaseURL    string
	Token      string
	Org        string
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

func New(baseURL, token, org string) *Client {
	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		Token:      token,
		Org:        org,
		HTTPClient: &http.Client{},
	}
}

// orgProjectPrefix returns the URL prefix for org-scoped project endpoints.
// e.g. "https://api.example.com/api/orgs/myorg/projects/myproject"
func (c *Client) orgProjectPrefix(project string) string {
	return fmt.Sprintf("%s/api/orgs/%s/projects/%s", c.BaseURL, c.Org, project)
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
	url := fmt.Sprintf("%s/previews/mr-%d/%s", c.orgProjectPrefix(project), mrID, action)

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
	return c.PostDrushByName(project, fmt.Sprintf("mr-%d", mrID), args)
}

func (c *Client) PostDrushByName(project string, previewName string, args string) (*ActionResult, error) {
	url := fmt.Sprintf("%s/previews/%s/drush", c.orgProjectPrefix(project), previewName)

	payload := fmt.Sprintf(`{"args": %q}`, args)
	resp, err := c.doRequest("POST", url, strings.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 404 {
		return nil, fmt.Errorf("preview %s/%s not found", project, previewName)
	}

	var result ActionResult
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("decode error: %w", err)
	}
	return &result, nil
}

type BaseFileInfo struct {
	Exists     bool   `json:"exists"`
	SizeBytes  int64  `json:"size_bytes"`
	ModifiedAt string `json:"modified_at"`
}

type BaseFilesStatus struct {
	DB    *BaseFileInfo `json:"db"`
	Files *BaseFileInfo `json:"files"`
}

func (c *Client) GetBaseFilesStatus(slug string) (*BaseFilesStatus, error) {
	url := fmt.Sprintf("%s/base-files", c.orgProjectPrefix(slug))

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

// UploadBaseFileChunked copies the reader to a temp file, then uploads
// directly to S3 via presigned URLs (single PUT for <=5GB, multipart for >5GB).
// uncompressedSize is the original size before compression (0 if unknown).
func (c *Client) UploadBaseFileChunked(slug, kind string, reader io.Reader, filename string, uncompressedSize int64) error {
	// 1. Copy stream to temp file to know size.
	tmpFile, err := os.CreateTemp(".", ".preview-upload-*")
	if err != nil {
		return fmt.Errorf("failed to create temp file: %w", err)
	}
	tmpPath := tmpFile.Name()
	defer os.Remove(tmpPath)

	// Clean up temp file on Ctrl+C / kill
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		os.Remove(tmpPath)
		fmt.Fprintln(os.Stderr, "\nUpload cancelled, temp file cleaned up.")
		os.Exit(1)
	}()
	defer signal.Stop(sigCh)

	bw := &bufferProgressWriter{}
	written, err := io.Copy(tmpFile, io.TeeReader(reader, bw))
	if err != nil {
		tmpFile.Close()
		return fmt.Errorf("failed to buffer upload: %w", err)
	}
	tmpFile.Close()
	fmt.Fprintf(os.Stderr, "\rBuffered %s to temp file.              \n", formatBytes(written))

	// 2. Request presigned URL(s) from API
	presignBody, _ := json.Marshal(map[string]interface{}{"total_size": written})
	resp, err := c.doRequest("POST",
		fmt.Sprintf("%s/base-files/%s/upload/presign", c.orgProjectPrefix(slug), kind),
		bytes.NewReader(presignBody))
	if err != nil {
		return fmt.Errorf("presign request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("presign HTTP %d: %s", resp.StatusCode, string(body))
	}
	var presignResult struct {
		Mode         string   `json:"mode"`
		PresignedURL string   `json:"presigned_url"`
		UploadID     string   `json:"upload_id"`
		PartSize     int64    `json:"part_size"`
		PartURLs     []string `json:"part_urls"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&presignResult); err != nil {
		return fmt.Errorf("presign decode error: %w", err)
	}
	resp.Body.Close()

	fmt.Fprintf(os.Stderr, "Uploading %s to S3...\n", formatBytes(written))

	// 3. Upload to S3
	var completeParts []map[string]interface{} // for multipart

	if presignResult.Mode == "single" {
		if err := c.uploadSingleToS3(tmpPath, presignResult.PresignedURL, written); err != nil {
			return err
		}
	} else {
		parts, err := c.uploadMultipartToS3(tmpPath, presignResult.PartURLs, presignResult.PartSize, written)
		if err != nil {
			return err
		}
		completeParts = parts
	}

	// 4. Confirm upload with API
	fmt.Fprintf(os.Stderr, "Confirming upload...\n")
	confirmPayload := map[string]interface{}{
		"uncompressed_size": uncompressedSize,
	}
	if presignResult.UploadID != "" {
		confirmPayload["upload_id"] = presignResult.UploadID
		confirmPayload["parts"] = completeParts
	}
	confirmBody, _ := json.Marshal(confirmPayload)
	confirmResp, err := c.doRequest("POST",
		fmt.Sprintf("%s/base-files/%s/upload/complete", c.orgProjectPrefix(slug), kind),
		bytes.NewReader(confirmBody))
	if err != nil {
		return fmt.Errorf("confirm request failed: %w", err)
	}
	defer confirmResp.Body.Close()
	if confirmResp.StatusCode != 200 {
		body, _ := io.ReadAll(confirmResp.Body)
		return fmt.Errorf("confirm HTTP %d: %s", confirmResp.StatusCode, string(body))
	}

	return nil
}

func (c *Client) uploadSingleToS3(filePath, presignedURL string, totalSize int64) error {
	f, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer f.Close()

	pr := &progressReader{reader: f, total: totalSize, label: "Uploading"}
	req, err := http.NewRequest("PUT", presignedURL, pr)
	if err != nil {
		return err
	}
	req.ContentLength = totalSize
	req.Header.Set("Content-Type", "application/octet-stream")

	s3Resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("S3 upload failed: %w", err)
	}
	defer s3Resp.Body.Close()
	fmt.Fprintln(os.Stderr)

	if s3Resp.StatusCode < 200 || s3Resp.StatusCode >= 300 {
		body, _ := io.ReadAll(s3Resp.Body)
		return fmt.Errorf("S3 upload HTTP %d: %s", s3Resp.StatusCode, string(body))
	}
	return nil
}

func (c *Client) uploadMultipartToS3(filePath string, partURLs []string, partSize, totalSize int64) ([]map[string]interface{}, error) {
	f, err := os.Open(filePath)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var totalUploaded int64
	parts := make([]map[string]interface{}, 0, len(partURLs))

	const maxRetries = 3

	for i, url := range partURLs {
		// Calculate this part's size
		remaining := totalSize - totalUploaded
		thisPartSize := partSize
		if remaining < thisPartSize {
			thisPartSize = remaining
		}

		partOffset := totalUploaded
		var etag string
		var lastErr error

		for attempt := 0; attempt <= maxRetries; attempt++ {
			if attempt > 0 {
				fmt.Fprintf(os.Stderr, "\nRetrying part %d/%d (attempt %d/%d)...\n", i+1, len(partURLs), attempt+1, maxRetries+1)
				time.Sleep(time.Duration(attempt) * 2 * time.Second)
				// Seek back to the start of this part
				if _, err := f.Seek(partOffset, io.SeekStart); err != nil {
					return nil, fmt.Errorf("part %d seek failed: %w", i+1, err)
				}
			}

			partReader := io.LimitReader(f, thisPartSize)
			pr := &progressReader{
				reader:  partReader,
				total:   totalSize,
				read:    partOffset,
				label:   fmt.Sprintf("Part %d/%d", i+1, len(partURLs)),
				lastPct: -1,
			}

			req, err := http.NewRequest("PUT", url, pr)
			if err != nil {
				return nil, fmt.Errorf("part %d request: %w", i+1, err)
			}
			req.ContentLength = thisPartSize
			req.Header.Set("Content-Type", "application/octet-stream")

			s3Resp, err := c.HTTPClient.Do(req)
			if err != nil {
				lastErr = fmt.Errorf("part %d upload failed: %w", i+1, err)
				continue
			}

			if s3Resp.StatusCode < 200 || s3Resp.StatusCode >= 300 {
				body, _ := io.ReadAll(s3Resp.Body)
				s3Resp.Body.Close()
				lastErr = fmt.Errorf("part %d S3 HTTP %d: %s", i+1, s3Resp.StatusCode, string(body))
				continue
			}

			etag = s3Resp.Header.Get("ETag")
			s3Resp.Body.Close()
			lastErr = nil
			break
		}

		if lastErr != nil {
			return nil, lastErr
		}

		parts = append(parts, map[string]interface{}{
			"ETag":       etag,
			"PartNumber": i + 1,
		})

		totalUploaded += thisPartSize
	}
	fmt.Fprintln(os.Stderr)

	return parts, nil
}

// progressReader wraps a reader and prints a progress bar to stderr.
// For multipart uploads, set `read` to the initial offset (bytes already uploaded).
type progressReader struct {
	reader  io.Reader
	total   int64
	read    int64
	label   string
	lastPct int
}

func (pr *progressReader) Read(p []byte) (int, error) {
	n, err := pr.reader.Read(p)
	pr.read += int64(n)
	pct := int(float64(pr.read) / float64(pr.total) * 100)
	if pct != pr.lastPct || err == io.EOF {
		pr.lastPct = pct
		bar := progressBar(float64(pct), 30)
		fmt.Fprintf(os.Stderr, "\r%s... %s / %s (%d%%) %s",
			pr.label, formatBytes(pr.read), formatBytes(pr.total), pct, bar)
	}
	return n, err
}

// bufferProgressWriter shows bytes written during buffering (unknown total).
type bufferProgressWriter struct {
	written int64
	lastLog int64
}

func (bw *bufferProgressWriter) Write(p []byte) (int, error) {
	bw.written += int64(len(p))
	if bw.written-bw.lastLog >= 1024*1024 {
		bw.lastLog = bw.written
		frames := []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}
		frame := frames[(bw.written/(1024*1024))%int64(len(frames))]
		fmt.Fprintf(os.Stderr, "\r%s Packaging... %s", frame, formatBytes(bw.written))
	}
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

func (c *Client) DownloadStream(project string, previewName string, kind string, w io.Writer) error {
	url := fmt.Sprintf("%s/previews/%s/%s/download", c.orgProjectPrefix(project), previewName, kind)

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
