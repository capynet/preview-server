package client

import (
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"strings"
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
