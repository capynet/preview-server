package client

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

type Client struct {
	BaseURL    string
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

func New(baseURL string) *Client {
	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		HTTPClient: &http.Client{},
	}
}

func (c *Client) ListPreviews(includeStatus bool) (*PreviewListResult, error) {
	statusParam := "true"
	if !includeStatus {
		statusParam = "false"
	}
	url := fmt.Sprintf("%s/api/previews?status=%s", c.BaseURL, statusParam)

	resp, err := c.HTTPClient.Get(url)
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

	resp, err := c.HTTPClient.Post(url, "application/json", nil)
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
	resp, err := c.HTTPClient.Post(url, "application/json", strings.NewReader(payload))
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

func (c *Client) DownloadStream(project string, mrID int, kind string, w io.Writer) error {
	url := fmt.Sprintf("%s/api/previews/%s/mr-%d/%s/download", c.BaseURL, project, mrID, kind)

	resp, err := c.HTTPClient.Get(url)
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
